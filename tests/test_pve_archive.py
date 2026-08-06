from __future__ import annotations

import pytest

from pve_client import archive_path


def test_archive_path_builds_pbs_volid() -> None:
    volid = archive_path("pbs-main", "vm/100/2026-05-01T01:00:00Z")
    assert volid == "pbs-main:backup/vm/100/2026-05-01T01:00:00Z"


def test_archive_path_strips_leading_slash() -> None:
    volid = archive_path("pbs-main", "/vm/100/2026-05-01T01:00:00Z")
    assert volid == "pbs-main:backup/vm/100/2026-05-01T01:00:00Z"


def test_archive_path_requires_pve_storage() -> None:
    with pytest.raises(ValueError, match="pve_storage"):
        archive_path("", "vm/100/2026-05-01T01:00:00Z")


def test_archive_path_requires_voltail() -> None:
    with pytest.raises(ValueError, match="voltail"):
        archive_path("pbs-main", "")
