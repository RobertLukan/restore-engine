from __future__ import annotations

import pytest

from pve_client import allocate_sequential_free_vmids


def test_allocate_respects_in_use_and_order() -> None:
    used = {101, 102, 105}
    ids, cursor = allocate_sequential_free_vmids(used, 100, 3)
    assert ids == [100, 103, 104]
    assert 100 in used and 103 in used and 104 in used
    assert cursor == 105


def test_allocate_empty_count() -> None:
    used: set[int] = {200}
    ids, cursor = allocate_sequential_free_vmids(used, 200, 0)
    assert ids == []
    assert cursor == 200


def test_allocate_raises_when_scan_exhausted() -> None:
    """No free IDs in [v, upper] for the requested count -> RuntimeError."""
    used = set(range(100, 5101))
    with pytest.raises(RuntimeError, match="could not allocate"):
        allocate_sequential_free_vmids(used, 100, 2)
