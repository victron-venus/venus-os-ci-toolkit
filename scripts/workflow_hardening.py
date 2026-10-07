"""Keep generated runners hardened with the centrally maintained action pin."""

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def harden_step():
    """Use the same Renovate-managed release commit as checked-in workflows."""
    pins = json.loads((ROOT / ".github/action-pins.json").read_text())
    pin = next(item for item in pins if item["packageName"] == "step-security/harden-runner")
    return {
        "name": "Harden the runner (Audit all outbound calls)",
        "uses": "step-security/harden-runner@" + pin["digest"],
        "with": {"egress-policy": "audit"},
    }


def harden_jobs(workflow):
    """Protect executable host jobs without modifying callers or container jobs."""
    result = copy.deepcopy(workflow)
    for job in result.get("jobs", {}).values():
        steps = job.get("steps")
        if not steps or "container" in job:
            continue
        if any(step.get("uses", "").startswith("step-security/harden-runner@") for step in steps):
            continue
        steps.insert(0, harden_step())
    return result
