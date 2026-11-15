"""RE-02: live restore must not be authorized by post-restore NIC isolation alone."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from main import _assert_power_on_isolation


def test_live_restore_rejected_with_unlink_only() -> None:
    with pytest.raises(HTTPException) as ei:
        _assert_power_on_isolation(
            power_on=False,
            qga_wait_sec=0,
            live_restore=True,
            network_mode="unlink",
            isolated=False,
            allow_non_isolated=False,
        )
    assert ei.value.status_code == 400
    assert "live_restore" in str(ei.value.detail).lower()


def test_live_restore_rejected_with_remap_only() -> None:
    with pytest.raises(HTTPException) as ei:
        _assert_power_on_isolation(
            power_on=False,
            qga_wait_sec=0,
            live_restore=True,
            network_mode="remap",
            isolated=False,
        )
    assert ei.value.status_code == 400


def test_live_restore_allowed_on_isolated_location() -> None:
    _assert_power_on_isolation(
        power_on=False,
        qga_wait_sec=0,
        live_restore=True,
        network_mode="none",
        isolated=True,
    )


def test_live_restore_allowed_with_allow_non_isolated() -> None:
    _assert_power_on_isolation(
        power_on=False,
        qga_wait_sec=0,
        live_restore=True,
        network_mode="unlink",
        isolated=False,
        allow_non_isolated=True,
    )


def test_powered_off_then_power_on_still_allows_unlink() -> None:
    """Ordinary restore: isolate after restore, then start — still supported."""
    _assert_power_on_isolation(
        power_on=True,
        qga_wait_sec=0,
        live_restore=False,
        network_mode="unlink",
        isolated=False,
    )
