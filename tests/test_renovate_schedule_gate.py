"""Production deduplication must never confuse dry runs or skipped backups."""
import copy
import datetime as dt
import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("gate", Path(__file__).resolve().parents[1] / "scripts/renovate_schedule_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
NOW = dt.datetime(2026, 11, 7, 22, tzinfo=dt.timezone.utc)


def iso(hours=0):
    return (NOW - dt.timedelta(hours=hours)).isoformat()


def run(**changes):
    value = dict(id=10, workflow_id=1, path=".github/workflows/renovate.yml", event="schedule",
                 head_branch="main", head_sha="a" * 40, status="completed", conclusion="success",
                 created_at=iso(4), updated_at=iso(2), run_attempt=1)
    value.update(changes)
    return value


def job(marked=False, producer="success", completed=None):
    steps = [dict(name=gate.PRODUCER_STEP, conclusion=producer, completed_at=completed or iso(2))]
    if marked:
        steps.append(dict(name=gate.PRODUCTION_MARKER, conclusion="success"))
    return dict(name="renovate", conclusion="success", steps=steps)


class API:
    def __init__(self, runs=(), jobs=()):
        self.runs = list(runs)
        self.jobs = list(jobs)
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path.endswith("/runs/20"):
            return run(id=20, created_at=iso(), status="in_progress", conclusion=None)
        if "/workflows/1/runs?" in path:
            return {"workflow_runs": copy.deepcopy(self.runs), "total_count": len(self.runs)}
        if "/attempts/" in path:
            return {"jobs": copy.deepcopy(self.jobs), "total_count": len(self.jobs)}
        raise AssertionError(path)


class ScheduleGateTests(unittest.TestCase):
    def decide(self, api, legacy=None):
        return gate.decide("schedule", "owner/repo", 20, "main", api, NOW, "09:23", legacy)

    def test_manual_dispatch_bypasses_api_for_dry_or_production(self):
        self.assertEqual(gate.decide("workflow_dispatch", "", "", "", None, NOW), (True, "manual-dispatch"))

    def test_no_history_runs(self):
        self.assertEqual(self.decide(API()), (True, "no-production-in-current-cycle"))

    def test_old_success_does_not_suppress(self):
        self.assertTrue(self.decide(API([run(updated_at=iso(21))], [job()]))[0])

    def test_fresh_scheduled_production_before_marker_suppresses(self):
        self.assertEqual(self.decide(API([run()], [job()])), (False, "production-in-current-cycle:10"))

    def test_manual_dry_run_without_marker_never_satisfies(self):
        self.assertTrue(self.decide(API([run(event="workflow_dispatch")], [job()]))[0])

    def test_marked_manual_production_satisfies(self):
        self.assertFalse(self.decide(API([run(event="workflow_dispatch")], [job(marked=True)]))[0])

    def test_reviewed_bridge_requires_exact_run_and_head(self):
        previous = run(event="workflow_dispatch")
        self.assertFalse(self.decide(API([previous], [job()]), (10, "a" * 40, NOW + dt.timedelta(hours=1)))[0])
        self.assertTrue(self.decide(API([previous], [job()]), (11, "a" * 40, NOW + dt.timedelta(hours=1)))[0])
        self.assertTrue(self.decide(API([previous], [job()]), (10, "b" * 40, NOW + dt.timedelta(hours=1)))[0])

    def test_expired_reviewed_bridge_never_satisfies(self):
        self.assertTrue(self.decide(API([run(event="workflow_dispatch")], [job()]),
                                   (10, "a" * 40, NOW))[0])

    def test_previous_day_delayed_production_does_not_skip_primary(self):
        now = dt.datetime(2026, 11, 8, 9, 23, tzinfo=dt.timezone.utc)
        api = API([run(updated_at=iso(6))], [job(completed=iso(6))])
        self.assertTrue(gate.decide("schedule", "owner/repo", 20, "main", api, now, "09:23")[0])

    def test_cycle_boundary_and_delayed_backup_across_midnight(self):
        at_primary = dt.datetime(2026, 11, 8, 9, 23, tzinfo=dt.timezone.utc)
        self.assertEqual(gate.cycle_start(at_primary, "09:23"), at_primary)
        before_primary = at_primary - dt.timedelta(seconds=1)
        self.assertEqual(gate.cycle_start(before_primary, "09:23"), at_primary - dt.timedelta(days=1))
        now = dt.datetime(2026, 11, 8, 1, tzinfo=dt.timezone.utc)
        self.assertFalse(gate.decide("schedule", "owner/repo", 20, "main", API([run()], [job()]), now, "09:23")[0])

    def test_private_cycle_has_independent_anchor(self):
        now = dt.datetime(2026, 11, 8, 10, tzinfo=dt.timezone.utc)
        self.assertEqual(gate.cycle_start(now, "10:43"), dt.datetime(2026, 11, 7, 10, 43, tzinfo=dt.timezone.utc))

    def test_malformed_primary_time_fails(self):
        for value in ["", "25:00", "10:99", "9:23"]:
            with self.subTest(value=value), self.assertRaises(gate.MetadataError):
                gate.cycle_start(NOW, value)

    def test_skipped_backup_never_satisfies(self):
        self.assertTrue(self.decide(API([run()], [job(producer="skipped")]))[0])

    def test_failed_engine_or_failed_run_never_satisfies(self):
        self.assertTrue(self.decide(API([run()], [job(producer="failure")]))[0])
        self.assertTrue(self.decide(API([run(conclusion="failure")], [job(marked=True)]))[0])

    def test_actual_completion_controls_freshness(self):
        self.assertTrue(self.decide(API([run(updated_at=iso())], [job(completed=iso(21))]))[0])

    def test_earlier_active_run_defers_but_later_pending_does_not(self):
        self.assertEqual(self.decide(API([run(status="in_progress", conclusion=None)])), (False, "earlier-run-still-active:10"))
        self.assertTrue(self.decide(API([run(status="queued", conclusion=None)]))[0])

    def test_current_run_and_other_branch_do_not_satisfy(self):
        self.assertTrue(self.decide(API([run(id=20), run(head_branch="feature")], [job()]))[0])

    def test_completed_future_timestamp_fails(self):
        with self.assertRaises(gate.MetadataError):
            self.decide(API([run()], [job(completed=iso(-1))]))

    def test_api_failure_is_not_a_permit(self):
        class Broken(API):
            def get(self, _):
                raise gate.MetadataError("unavailable")
        with self.assertRaises(gate.MetadataError):
            self.decide(Broken())

    def test_incomplete_metadata_is_not_a_permit(self):
        class Broken(API):
            def get(self, path):
                if "/workflows/" in path:
                    return {"workflow_runs": None}
                return super().get(path)
        with self.assertRaises(gate.MetadataError):
            self.decide(Broken())

    def test_bounded_pagination_fails_instead_of_truncating(self):
        api = API([run()] * 100)
        with self.assertRaises(gate.MetadataError):
            self.decide(api)
        self.assertEqual(len(api.calls), gate.MAX_PAGES + 1)

    def test_candidate_job_lookups_are_bounded(self):
        with self.assertRaises(gate.MetadataError):
            self.decide(API([run(id=i, event="workflow_dispatch") for i in range(30, 50)], [job()]))

    def test_attempt_specific_jobs_are_used(self):
        api = API([run(run_attempt=3)], [job()])
        self.decide(api)
        self.assertTrue(any("/attempts/3/jobs?" in p for p in api.calls))

    def test_wrong_workflow_or_context_fails(self):
        class Wrong(API):
            def get(self, path):
                value = super().get(path)
                if path.endswith("/runs/20"):
                    value["path"] = ".github/workflows/other.yml"
                return value
        with self.assertRaises(gate.MetadataError):
            self.decide(Wrong())

    def test_incomplete_job_steps_fail(self):
        with self.assertRaises(gate.MetadataError):
            self.decide(API([run()], [dict(name="renovate", conclusion="success", steps=None)]))

    def test_duplicate_named_steps_fail(self):
        value = job()
        value["steps"] *= 2
        with self.assertRaises(gate.MetadataError):
            self.decide(API([run()], [value]))


if __name__ == "__main__":
    unittest.main()
