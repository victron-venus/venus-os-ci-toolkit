"""Exercise real version declarations and frozen-plan binding without writes."""

import copy
import json
import sys
from pathlib import Path

import atheris

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
with atheris.instrument_imports():
    import version_plan


def test_one_input(data: bytes) -> None:
    """Mutate real selectors; accepted policies must retain exact plan binding."""
    selector = data[0] % 5 if data else 0
    text = data[1:].decode("utf-8", errors="replace")
    declaration = {"path": "SOURCE_VERSION", "format": "text", "value": "package"}
    config = {"schema": 1, "promotion": "final-build", "files": [declaration]}
    policy = {"versioning": config}
    if selector == 0:
        declaration["path"] = text
    elif selector == 1:
        declaration.update(format="json", field=text)
    elif selector == 2:
        declaration["value"] = text
    elif selector == 3:
        declaration.update(ecosystem=text)
    else:
        config["promotion"] = text
    original = copy.deepcopy(policy)
    try:
        version_plan.validate_policy(policy)
        plan = version_plan.create_plan("0.1.1", "rc", 1, "a" * 40, policy)
    except ValueError:
        assert policy == original
        return
    assert policy == original
    assert version_plan.validate_plan(plan, policy, "a" * 40) == plan
    assert plan == version_plan.create_plan("0.1.1", "rc", 1, "a" * 40, policy)
    assert version_plan.validate_plan(json.loads(json.dumps(plan)), policy) == plan
    changed = dict(plan, source_sha="b" * 40)
    try:
        version_plan.validate_plan(changed, policy, "a" * 40)
    except ValueError:
        pass
    else:
        raise AssertionError("A changed source SHA passed frozen-plan binding")


if __name__ == "__main__":
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
