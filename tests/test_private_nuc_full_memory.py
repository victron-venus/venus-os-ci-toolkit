"""Explicit full-memory NUC selection; offline contracts, not live CEL qualification."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "full_memory_helpers", Path(__file__).with_name("test_private_nuc_profiles.py")
)
h = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(h)
arc = h.arc
TARGET = h.TARGETS[0]


def selected_plan():
    return h.plan()._replace(
        low_share_ci_scopes=(TARGET[0],), full_memory_ci_scopes=(TARGET[0],)
    )


class FullMemoryTests(unittest.TestCase):
    def test_only_selected_memory_request_and_scoped_admission_change(self):
        before = h.render(selected_plan()._replace(full_memory_ci_scopes=()))
        after = h.render(selected_plan())
        key = arc.name(*TARGET) + ".json"
        self.assertEqual(
            {k for k in before if before[k] != after[k]}, {"foundations.json", key}
        )
        worker = copy.deepcopy(after[key])
        resources = worker["template"]["spec"]["containers"][0]["resources"]
        self.assertEqual(
            (resources["requests"]["memory"], resources["limits"]["memory"]),
            ("6Gi", "6Gi"),
        )
        self.assertEqual(resources["requests"]["cpu"], "100m")
        resources["requests"]["memory"] = "3Gi"
        self.assertEqual(worker, before[key])
        original = copy.deepcopy(before["foundations.json"])
        actual = copy.deepcopy(after["foundations.json"])
        pairs = [(a, b) for a, b in zip(original["items"], actual["items"]) if a != b]
        self.assertEqual(len(pairs), 1)
        a, b = pairs[0]
        self.assertEqual(b["metadata"]["name"], arc.NUC_RUNTIME + "-workers")
        changed = [
            i
            for i, (left, right) in enumerate(
                zip(a["spec"]["validations"], b["spec"]["validations"])
            )
            if left != right
        ]
        self.assertEqual(len(changed), 1)
        b["spec"]["validations"][changed[0]] = a["spec"]["validations"][changed[0]]
        self.assertEqual(original, actual)
        self.assertEqual(
            h.quota_charge(after, TARGET)["requests.memory"], h.quantity("6272Mi")
        )

    def test_exact_six_gib_admission_rejects_other_requests_and_borrowed_scope(self):
        docs = h.render(selected_plan())
        rule = next(
            r["expression"]
            for r in h.policy(docs, "-workers")["spec"]["validations"]
            if r["message"].startswith("NUC workers require one exact")
        )
        pod = copy.deepcopy(h.worker(docs, TARGET)["template"])
        pod["metadata"]["namespace"] = "arc-private-ci"
        context = {
            "object": pod,
            "oldObject": None,
            "request": {"namespace": "arc-private-ci", "operation": "CREATE"},
        }
        self.assertTrue(h.identity_expression(rule, context))
        for memory in ("3Gi", "5Gi", "7Gi"):
            changed = copy.deepcopy(context)
            changed["object"]["spec"]["containers"][0]["resources"]["requests"][
                "memory"
            ] = memory
            self.assertFalse(h.identity_expression(rule, changed))
        changed = copy.deepcopy(context)
        changed["object"]["metadata"]["labels"][arc.SCOPE_LABEL] = arc.name(
            h.TARGETS[1][0], "scope"
        )
        self.assertFalse(h.identity_expression(rule, changed))

    def test_invalid_or_conflicting_scopes_fail(self):
        for value in (
            None,
            [],
            (True,),
            ("bad scope",),
            (TARGET[0], TARGET[0]),
            ("missing/repo",),
            (h.TARGETS[1][0],),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                h.render(selected_plan()._replace(full_memory_ci_scopes=value))
        with self.assertRaises(ValueError):
            h.render(
                selected_plan()._replace(
                    light_ci_scopes=(TARGET[0],), low_share_ci_scopes=()
                )
            )
        with self.assertRaises(ValueError):
            h.render(
                selected_plan()._replace(
                    targets=tuple(t for t in h.plan().targets if t.scope != TARGET[0])
                )
            )

    def test_json_plan_roundtrip_and_no_arbitrary_memory_override(self):
        cfg = selected_plan()
        data = cfg._asdict()
        data["targets"] = [t._asdict() for t in cfg.targets]
        del data["bounded_heavy_ci_scopes"], data["bounded_heavy_image"]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "plan.json"
            p.write_text(json.dumps(data))
            self.assertEqual(arc.load_nuc_kvm_plan(p), cfg)
            for value in (None, True, "6Gi", [False]):
                p.write_text(json.dumps(dict(data, full_memory_ci_scopes=value)))
                with self.assertRaises(ValueError):
                    arc.load_nuc_kvm_plan(p)
            p.write_text(json.dumps(dict(data, memory_request="6Gi")))
            with self.assertRaises(ValueError):
                arc.load_nuc_kvm_plan(p)

    def test_absent_and_empty_keep_standard_heavy_and_automation_defaults(self):
        default = h.render(h.plan())
        empty = h.render(h.plan()._replace(full_memory_ci_scopes=()))
        self.assertEqual(default, empty)
        for target, wanted in h.RESOURCE_TUPLES.items():
            self.assertEqual(
                h.worker(default, target)["template"]["spec"]["containers"][0][
                    "resources"
                ],
                wanted,
            )


if __name__ == "__main__":
    unittest.main()
