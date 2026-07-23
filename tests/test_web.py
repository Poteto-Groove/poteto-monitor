"""Web API のテスト（ネットワークとファイルパスをモック）。"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from poteto_monitor import config as config_mod  # noqa: E402
from poteto_monitor.models import Reading  # noqa: E402
from poteto_monitor.web import poller as poller_mod  # noqa: E402
from poteto_monitor.web import server as server_mod  # noqa: E402


def _fake_readings(assets, base_currency, session=None):
    out = []
    for a in assets:
        out.append(
            Reading(key=a.key, label=a.label, emoji=a.emoji, value=123.0,
                    display="123", threshold=a.threshold, type=a.type)
        )
    return out


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(poller_mod, "PRICES_FILE", tmp_path / "prices.json")
    monkeypatch.setattr(poller_mod, "HISTORY_FILE", tmp_path / "history.json")
    # ポーラーがネットワークに出ないようスタブ化。
    monkeypatch.setattr(poller_mod, "fetch_all", _fake_readings)

    cfg_file.write_text(
        '{"webhook_url":"","poll_interval":300,"report_interval":0,'
        '"watch":[{"type":"crypto","id":"bitcoin","label":"BTC"}]}',
        encoding="utf-8",
    )
    from poteto_monitor.web.context import AppContext

    ctx = AppContext(config_mod.load_config())
    app = server_mod.create_app(ctx)
    with TestClient(app) as c:
        yield c


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "poteto-monitor" in r.text


def test_state_endpoint(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert "assets" in body and body["base_currency"] == "usd"


def test_config_masks_secrets(client, tmp_path):
    # webhook を設定して、GET では伏せられることを確認。
    r = client.put("/api/config", json={"webhook_url": "https://discord.com/api/webhooks/x/y"})
    assert r.status_code == 200
    got = client.get("/api/config").json()
    assert got["webhook_configured"] is True
    assert got["webhook_url"] == ""  # 生の URL は返さない


def test_config_update_changes_watch(client):
    payload = {"watch": [{"type": "forex", "base": "USD", "quote": "JPY", "label": "ドル円"}]}
    r = client.put("/api/config", json=payload)
    assert r.status_code == 200
    got = client.get("/api/config").json()
    assert got["watch"][0]["quote"] == "JPY"


def test_invalid_config_rejected(client):
    r = client.put("/api/config", json={"watch": [{"type": "crypto"}]})  # id 欠落
    assert r.status_code == 400


def test_auth_required_when_token_set(client):
    # トークンを設定 → 以降 config API は認証必須。
    client.put("/api/config", json={"web": {"auth_token": "s3cret"}})
    assert client.get("/api/config").status_code == 401
    ok = client.get("/api/config", headers={"X-Auth-Token": "s3cret"})
    assert ok.status_code == 200


def test_refresh_endpoint(client):
    assert client.post("/api/refresh").status_code == 200
