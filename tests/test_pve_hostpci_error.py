from __future__ import annotations

from pve_client import _explain_pve_task_failure


def test_explain_hostpci_root_failure() -> None:
    msg = _explain_pve_task_failure("only root can set 'hostpci0' config for non-mapped devices")
    assert "PVE task failed:" in msg
    assert "PCI passthrough" in msg
    assert "root" in msg.lower()


def test_explain_other_failure_passthrough() -> None:
    msg = _explain_pve_task_failure("something else went wrong")
    assert msg == "PVE task failed: something else went wrong"
