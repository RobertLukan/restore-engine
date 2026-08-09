"""Guest hostname vs PVE name matching (warning-only mismatch)."""

from __future__ import annotations

from pve_client import hostname_matches_pve_name


def test_hostname_exact_match() -> None:
    assert hostname_matches_pve_name("web01", "web01")
    assert hostname_matches_pve_name("Web01", "web01")


def test_hostname_fqdn_matches_short_pve_name() -> None:
    assert hostname_matches_pve_name("web01.example.com", "web01")
    assert hostname_matches_pve_name("web01", "web01.example.com")


def test_hostname_mismatch() -> None:
    assert not hostname_matches_pve_name("old-name", "test-clone2")
    assert not hostname_matches_pve_name("", "test-clone2")
    assert not hostname_matches_pve_name("web01", "")
