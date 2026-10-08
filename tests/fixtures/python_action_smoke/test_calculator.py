"""Require both pytest plugins and exercise real coverage collection."""

import asyncio

import pytest

from calculator import add


def test_add():
    assert add(1, 2) == 3


@pytest.mark.asyncio
async def test_async_add():
    await asyncio.sleep(0)
    assert add(3, 4) == 7
