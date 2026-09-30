"""Offline receipt-byte checks for complete, bounded final-build diagnostics."""

# Tests intentionally exercise the public verifier with malformed saved evidence.
# pylint: disable=missing-class-docstring,missing-function-docstring,wrong-import-position

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_versioned as lifecycle


class ToolchainDiagnosticsTests(unittest.TestCase):
    def fixture(self, pairs):
        assets, declarations, receipts, files = [], [], [], {}
        for asset_id, (name, original, current) in enumerate(pairs, 1):
            raw = json.dumps({"toolchain": original}).encode()
            assets.append({"id": asset_id, "name": name})
            declarations.append({"name": name, "sha256": lifecycle.rc.digest(raw)})
            receipts.append({"name": name, "inputs": {"toolchain": current}})
            files[f"releases/assets/{asset_id}"] = raw
        github = Mock()
        github.binary.side_effect = files.__getitem__
        candidate = {"tag": "v1.2.3-rc.1", "build_receipts": declarations}
        return github, candidate, receipts, assets, files

    def verify(self, fixture):
        github, candidate, receipts, assets, _ = fixture
        with patch.object(
            lifecycle.rc, "release_snapshot", return_value=({}, {}, assets)
        ):
            return lifecycle.verify_final_toolchains(github, candidate, receipts)

    def failure(self, fixture):
        with self.assertRaises(lifecycle.rc.ReleaseError) as raised:
            self.verify(fixture)
        return str(raised.exception)

    def test_all_six_platforms_checked_and_multiple_drift_sorted(self):
        pairs = []
        for platform in ("windows", "linux", "ios", "mac-x64", "android", "mac-arm64"):
            original = {"python": "3.13", "ImageVersion": "20260927.309.1"}
            current = copy.deepcopy(original)
            if platform in {"android", "linux"}:
                current["ImageVersion"] = "20260920.303.1"
            pairs.append((f"release-inputs-{platform}.json", original, current))
        fixture = self.fixture(pairs)
        message = self.failure(fixture)
        self.assertEqual(fixture[0].binary.call_count, 6)
        self.assertIn("2 platform receipt(s), 2 field difference(s)", message)
        lines = [line for line in message.splitlines() if line.startswith("  ")]
        self.assertEqual(
            lines,
            [
                "  release-inputs-android.json: toolchain/ImageVersion: value changed",
                "  release-inputs-linux.json: toolchain/ImageVersion: value changed",
            ],
        )
        self.assertNotIn("202609", message)
        self.assertIn("a new RC alone does not guarantee matching inputs", message)
        self.assertEqual(message, self.failure(self.fixture(list(reversed(pairs)))))

    def test_nested_missing_type_list_and_escaped_key_paths(self):
        original = {
            "python": "3.13",
            "compiler": {"version": "old", "gone": None},
            "flags": ["old", "removed"],
            "typed": 7,
            "a/b~c": 1,
        }
        current = {
            "python": "3.13",
            "compiler": {"version": "new", "added": None},
            "flags": ["new"],
            "typed": "7",
            "a/b~c": 2,
        }
        message = self.failure(
            self.fixture([("release-inputs-linux.json", original, current)])
        )
        self.assertIn("1 platform receipt(s), 7 field difference(s)", message)
        for expected in (
            "toolchain/a~1b~0c: value changed",
            "toolchain/compiler/added: missing in accepted RC",
            "toolchain/compiler/gone: missing in final build",
            "toolchain/compiler/version: value changed",
            "toolchain/flags/0: value changed",
            "toolchain/flags/1: missing in final build",
            "toolchain/typed: type changed (int -> str)",
        ):
            self.assertIn(expected, message)

    def test_exact_existing_equality_remains_the_acceptance_predicate(self):
        fixture = self.fixture(
            [
                (
                    "release-inputs-linux.json",
                    {"python": "3.13", "extra": 1},
                    {"extra": True, "python": "3.13"},
                ),
                ("release-inputs-ios.json", {"python": "3.13"}, {"python": "3.13"}),
            ]
        )
        self.assertIsNone(self.verify(fixture))
        self.assertEqual(fixture[0].binary.call_count, 2)

    def test_malformed_authenticated_receipt_is_rejected_without_echoing_keys(self):
        hostile = "UNSAFE\n::error::secret"
        duplicate = json.dumps(hostile)
        for raw in (
            b"not json",
            b"[]",
            b"null",
            b'"text"',
            ("{" + duplicate + ":1," + duplicate + ":2}").encode(),
        ):
            with self.subTest(raw_type=raw[:1]):
                fixture = self.fixture([("release-inputs-linux.json", {}, {})])
                fixture[4]["releases/assets/1"] = raw
                fixture[1]["build_receipts"][0]["sha256"] = lifecycle.rc.digest(raw)
                message = self.failure(fixture)
                self.assertIn("Invalid", message)
                self.assertNotIn("UNSAFE", message)
                self.assertNotIn("secret", message)
                self.assertNotIn("differs", message)

    def test_malformed_final_receipt_object_is_rejected(self):
        fixture = self.fixture([("release-inputs-linux.json", {}, {})])
        fixture[2][0]["inputs"] = []
        self.assertEqual(self.failure(fixture), "Invalid toolchain receipt object")

    def test_later_integrity_failure_takes_precedence_over_earlier_drift(self):
        fixture = self.fixture(
            [
                ("release-inputs-android.json", {"python": "old"}, {"python": "new"}),
                ("release-inputs-linux.json", {"python": "old"}, {"python": "new"}),
            ]
        )
        fixture[4]["releases/assets/2"] = b"changed authenticated bytes"
        self.assertEqual(self.failure(fixture), "RC toolchain receipt changed")
        self.assertEqual(fixture[0].binary.call_count, 2)

    def test_inventory_missing_asset_and_invalid_id_still_reject(self):
        for defect, expected in (
            ("inventory", "Final platform receipt inventory differs from RC"),
            ("asset", "RC platform receipt is missing"),
            ("id", "Invalid receipt asset ID"),
        ):
            with self.subTest(defect=defect):
                fixture = self.fixture([("release-inputs-linux.json", {}, {})])
                if defect == "inventory":
                    fixture[1]["build_receipts"] = []
                elif defect == "asset":
                    fixture[3].clear()
                else:
                    fixture[3][0]["id"] = 0
                self.assertEqual(self.failure(fixture), expected)
                fixture[0].binary.assert_not_called()

    def test_arbitrary_values_and_unsafe_labels_are_not_logged(self):
        hostile = "SECRET\n::error::\x1b[31m\u202e"
        fixture = self.fixture(
            [
                (
                    hostile,
                    {hostile: hostile, "safe": hostile},
                    {hostile: "another secret", "safe": "another secret"},
                )
            ]
        )
        message = self.failure(fixture)
        for unsafe in ("SECRET", "::error::", "\x1b", "\u202e", "another secret"):
            self.assertNotIn(unsafe, message)
        self.assertIn("redacted-sha256-", message)
        self.assertTrue(message.isascii())

    def test_large_drift_is_counted_but_failure_output_is_bounded(self):
        original = {f"field-{i:03d}-" + "a" * 190: "old" for i in range(150)}
        current = {key: "new" for key in original}
        fixture = self.fixture(
            [
                ("release-inputs-a.json", original, current),
                ("release-inputs-z.json", {"python": "old"}, {"python": "new"}),
            ]
        )
        message = self.failure(fixture)
        self.assertIn("2 platform receipt(s), 151 field difference(s)", message)
        self.assertIn("51 further field differences omitted (log limit)", message)
        self.assertLess(len(message.encode()), 48_000)
        self.assertEqual(fixture[0].binary.call_count, 2)


if __name__ == "__main__":
    unittest.main()
