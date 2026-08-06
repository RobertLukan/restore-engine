from __future__ import annotations

from sources import load_sources, source_by_id


def test_nested_servers_flatten_to_sources() -> None:
    cfg = {
        "pbs_servers": [
            {
                "id": "main",
                "host": "10.0.0.10",
                "port": 8007,
                "verify_ssl": False,
                "api_token_id": "root@pam!restore",
                "api_token_secret": "s3cret",
                "mounts": [
                    {"datastore": "main", "namespace": "", "pve_storage": "pbs-main"},
                    {"datastore": "main", "namespace": "team-a", "pve_storage": "pbs-main-teamA"},
                    {"datastore": "archive", "namespace": "", "pve_storage": "pbs-archive"},
                ],
            },
            {
                "id": "dr",
                "host": "10.0.0.30",
                "api_token_id": "root@pam!restore",
                "api_token_secret": "s3cret2",
                "mounts": [{"datastore": "backup", "namespace": "", "pve_storage": "pbs-dr"}],
            },
        ]
    }
    sources = load_sources(cfg)
    assert len(sources) == 4
    ids = {s.source_id for s in sources}
    assert ids == {"main/main/root", "main/main/team-a", "main/archive/root", "dr/backup/root"}

    teama = source_by_id(cfg, "main/main/team-a")
    assert teama is not None
    assert teama.namespace == "team-a"
    assert teama.pve_storage == "pbs-main-teamA"
    assert teama.host == "10.0.0.10"
    assert teama.api_token_secret == "s3cret"


def test_mounts_missing_pve_storage_are_skipped() -> None:
    cfg = {
        "pbs_servers": [
            {
                "id": "main",
                "host": "10.0.0.10",
                "mounts": [
                    {"datastore": "main", "namespace": "", "pve_storage": ""},  # skipped
                    {"datastore": "", "namespace": "", "pve_storage": "x"},  # skipped
                    {"datastore": "ok", "namespace": "", "pve_storage": "pbs-ok"},
                ],
            }
        ]
    }
    sources = load_sources(cfg)
    assert [s.source_id for s in sources] == ["main/ok/root"]


def test_legacy_single_pbs_is_converted() -> None:
    cfg = {
        "pbs": {
            "host": "10.0.0.10",
            "port": 8007,
            "verify_ssl": False,
            "api_token_id": "root@pam!restore",
            "api_token_secret": "legacy",
            "datastore": "main",
        },
        "proxmox": {"pbs_storage": "pbs-main"},
    }
    sources = load_sources(cfg)
    assert len(sources) == 1
    src = sources[0]
    assert src.datastore == "main"
    assert src.namespace == ""
    assert src.pve_storage == "pbs-main"
    assert src.api_token_secret == "legacy"


def test_new_schema_takes_precedence_over_legacy() -> None:
    cfg = {
        "pbs": {"host": "old", "datastore": "old", "api_token_secret": "x"},
        "proxmox": {"pbs_storage": "old-storage"},
        "pbs_servers": [
            {"id": "new", "host": "new", "mounts": [{"datastore": "d", "pve_storage": "s"}]}
        ],
    }
    sources = load_sources(cfg)
    assert len(sources) == 1
    assert sources[0].server_id == "new"
