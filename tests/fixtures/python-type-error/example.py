"""Intentionally invalid annotation for the type-check contract test."""


def add(a: int, b: int) -> str:
    """Return an integer where the annotation requires a string."""
    return a + b
