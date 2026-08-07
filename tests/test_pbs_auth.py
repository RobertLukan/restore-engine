"""PBS auth: API token preferred; username/password ticket fallback."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from pbs_client import _authenticated_get, probe_pbs_source
from sources import Source


def _source(**kwargs: Any) -> Source:
    base = dict(
        source_id="main/main/root",
        server_id="main",
        label="main · main",
        host="pbs.example",
        port=8007,
        verify_ssl=False,
        api_token_id="",
        api_token_secret="",
        user="",
        password="",
        datastore="main",
        namespace="",
        pve_storage="pbs-main",
    )
    base.update(kwargs)
    return Source(**base)


def test_token_auth_preferred_over_password() -> None:
    src = _source(
        api_token_id="root@pam!tok",
        api_token_secret="sec",
        user="u@pbs",
        password="pw",
    )
    client = MagicMock()
    client.get.return_value = MagicMock(status_code=200)
    _authenticated_get(client, src, "/version")
    client.get.assert_called_once()
    kwargs = client.get.call_args.kwargs
    assert kwargs["headers"]["Authorization"].startswith("PBSAPIToken=")
    client.post.assert_not_called()


def test_password_auth_fetches_ticket() -> None:
    src = _source(user="flaskapp@pbs", password="secret")
    client = MagicMock()
    ticket_resp = MagicMock(status_code=200)
    ticket_resp.json.return_value = {
        "data": {"ticket": "TICKET", "CSRFPreventionToken": "CSRF"}
    }
    version_resp = MagicMock(status_code=200)
    client.post.return_value = ticket_resp
    client.get.return_value = version_resp

    resp = _authenticated_get(client, src, "/version")
    assert resp.status_code == 200
    client.post.assert_called_once()
    assert "/access/ticket" in client.post.call_args.args[0]
    get_kwargs = client.get.call_args.kwargs
    assert get_kwargs["cookies"]["PBSAuthCookie"] == "TICKET"
    assert get_kwargs["headers"]["CSRFPreventionToken"] == "CSRF"


def test_missing_auth_raises() -> None:
    src = _source()
    with pytest.raises(ValueError, match="api_token_id/api_token_secret or user/password"):
        _authenticated_get(MagicMock(), src, "/version")


def test_probe_pbs_source_reports_password_mode() -> None:
    src = _source(user="u@pbs", password="pw")
    version = MagicMock(status_code=200)

    class FakeClient:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def post(self, *a: Any, **k: Any) -> MagicMock:
            r = MagicMock(status_code=200)
            r.json.return_value = {"data": {"ticket": "T", "CSRFPreventionToken": "C"}}
            return r

        def get(self, *a: Any, **k: Any) -> MagicMock:
            return version

    with patch("pbs_client.httpx.Client", FakeClient):
        ok, msg = probe_pbs_source(src)
    assert ok is True
    assert "password" in msg
