"""Tests for python-minimal fixture."""

from src import add


def test_add_positive():
    """Positive numbers."""
    assert add(2, 3) == 5


def test_add_zero():
    """Zero inputs."""
    assert add(0, 0) == 0


def test_add_negative():
    """Opposite signs sum to zero."""
    assert add(-1, 1) == 0
