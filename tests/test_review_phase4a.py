"""コードレビュー (2026-10-07) 段階 4a（セキュリティと不具合）のテスト。"""

from __future__ import annotations

import asyncio
import json

import pytest

from poteto_monitor import config as config_mod
from poteto_monitor.config import ConfigError, parse_config, read_raw
from poteto_monitor.models import Reading
from poteto_monitor.notify import split_messages
from poteto_monitor.providers import fetch_hyperliquid


class Resp:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class HLSession:
    def post(self, url, json=None, timeout=None):
        return Resp({"HYPE": "30", "kPEPE": "0.01", "BTC": "100000"})


# ── U-1: Hyperliquid の銘柄名 ────────────────────────────────────────
@pytest.mark.parametrize("coin,expected", [("kPEPE", 0.01), ("kpepe", 0.01), ("hype", 30.0), ("HYPE", 30.0)])
def test_hyperliquid_coin_case(coin, expected):
    assets = parse_config({"watch": [{"type": "hyperliquid", "coin": coin}]}).assets
    result = fetch_hyperliquid(assets, session=HLSession())
    assert result.readings[0].value == expected
    assert assets[0].key == f"hl:{coin.upper()}"  # 既存の履歴キーと互換


# ── B-9: Discord の embed 上限 ───────────────────────────────────────
def test_split_messages_respects_discord_limits():
    report = {"title": "📊 マーケット定期レポート",
              "fields": [{"name": f"n{i}", "value": "v" * 100, "inline": True} for i in range(60)]}
    alert = {"title": "🚨", "description": "x"}
    messages = split_messages([report, alert])
    embeds = [e for m in messages for e in m]
    assert all(len(e.get("fields", [])) <= 25 for e in embeds)
    assert sum(len(e.get("fields", [])) for e in embeds) == 60
    assert all(len(m) <= 10 for m in messages)
    for m in messages:
        total = sum(len(e.get("title", "")) + len(e.get("description", "")) +
                    sum(len(f["name"]) + len(f["value"]) for f in e.get("fields", [])) for e in m)
        assert total <= 6000
    assert embeds[0]["title"].endswith("(1/3)")


# ── B-11: 1 回実行モードは送信に失敗しても前回値を保存 ──────────────
def test_run_saves_prices_even_if_send_fails(tmp_path, monkeypatch):
    from poteto_monitor import monitor as monitor_mod

    prices = tmp_path / "prices.json"
    monkeypatch.setattr(monitor_mod, "PRICES_FILE", prices)

    def boom(url, embeds):
        raise RuntimeError("discord down")

    monkeypatch.setattr(monitor_mod, "send", boom)
    monkeypatch.setattr(monitor_mod, "fetch_all", lambda assets, base, **kw: [
        Reading(key="crypto:bitcoin", label="BTC", emoji="", value=100.0, display="", threshold=10, type="crypto")])
    with pytest.raises(RuntimeError):
        monitor_mod.run(parse_config({"webhook_url": "https://discord.com/api/webhooks/x/y",
                                      "watch": [{"type": "crypto", "id": "bitcoin"}]}))
    assert json.loads(prices.read_text(encoding="utf-8"))["values"]["crypto:bitcoin"] == 100.0


# ── B-14: 壊れた config.json ────────────────────────────────────────
def test_read_raw_rejects_broken_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ConfigError):
        read_raw(path)


# ── E-10: SSE キュー ────────────────────────────────────────────────
def test_broadcast_serializes_once_and_keeps_latest():
    from poteto_monitor.web.state import LiveState

    async def scenario():
        state = LiveState()
        q = state.subscribe()
        for i in range(20):
            state.updated_at = str(i)
            state.broadcast()
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return items

    items = asyncio.run(scenario())
    assert all(isinstance(i, str) for i in items)
    assert json.loads(items[-1])["updated_at"] == "19"  # 最新は捨てない


# ── Web: 認証まわり（S-2, S-4, S-5, S-6）──────────────────────────────
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from poteto_monitor.history import HistoryStore  # noqa: E402
from poteto_monitor.providers import Source, SourceResult  # noqa: E402
from poteto_monitor.web import poller as poller_mod  # noqa: E402
from poteto_monitor.web import server as server_mod  # noqa: E402
from poteto_monitor.web.context import AppContext  # noqa: E402

TOKEN = "s3cret-token"


def _fake(assets, base_currency, session=None, api_key=""):
    return SourceResult(readings=[Reading(key=a.key, label=a.label, emoji="", value=1.0, display="1",
                                          threshold=a.threshold, type=a.type) for a in assets])


def _client(tmp_path, monkeypatch, web: dict, https: bool = False):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(poller_mod, "PRICES_FILE", tmp_path / "prices.json")
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    cfg_file.write_text(json.dumps({"poll_interval": 300, "report_interval": 0, "web": web,
                                    "watch": [{"type": "hyperliquid", "coin": "HYPE"}]}), encoding="utf-8")
    ctx = AppContext(config_mod.load_config(), history=HistoryStore(tmp_path / "h.db"))
    ctx.fetcher.sources = {"fake": Source(types=("hyperliquid",), fetch=_fake)}
    base_url = "https://testserver" if https else "http://testserver"
    return TestClient(server_mod.create_app(ctx), base_url=base_url)


def test_writes_disabled_without_token(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, {}) as c:
        assert c.get("/api/state").status_code == 200
        assert c.put("/api/config", json={"alert_threshold": 5}).status_code == 403
        assert c.post("/api/refresh").status_code == 403


def test_read_protected_by_default_when_token_set(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, {"auth_token": TOKEN}) as c:
        assert c.get("/").status_code == 200  # UI の殻（静的ファイル）は公開
        assert c.get("/healthz").status_code in (200, 503)  # 外部監視用は認証なし
        for path in ("/api/state", "/api/history", "/api/config", "/api/stream"):
            assert c.get(path).status_code == 401, path
        assert c.get("/api/state", headers={"X-Auth-Token": TOKEN}).status_code == 200


def test_protect_read_false_opens_reads_only(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, {"auth_token": TOKEN, "protect_read": False}) as c:
        assert c.get("/api/state").status_code == 200
        assert c.get("/api/history").status_code == 200
        assert c.get("/api/config").status_code == 401
        assert c.post("/api/refresh").status_code == 401


def test_cookie_login_flow(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, {"auth_token": TOKEN}, https=True) as c:
        assert c.post("/api/login", json={"token": "wrong"}).status_code == 401
        r = c.post("/api/login", json={"token": TOKEN})
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
        assert c.get("/api/config").status_code == 200
        assert c.put("/api/config", json={"alert_threshold": 5}).status_code == 200
        c.post("/api/logout")
        assert c.get("/api/config").status_code == 401


def test_session_invalidated_when_token_changes(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, {"auth_token": TOKEN}) as c:
        c.post("/api/login", json={"token": TOKEN})
        assert c.put("/api/config", json={"web": {"auth_token": "new-token-123"}}).status_code == 200
        assert c.get("/api/config").status_code == 401


def test_login_rate_limited(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, {"auth_token": TOKEN}) as c:
        codes = [c.post("/api/login", json={"token": f"bad{i}"}).status_code for i in range(12)]
        assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]
        assert c.post("/api/login", json={"token": TOKEN}).status_code == 429  # 正しくても待たせる


def test_webhook_must_be_discord_and_host_not_editable(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, {"auth_token": TOKEN}) as c:
        h = {"X-Auth-Token": TOKEN}
        for bad in ("http://discord.com/api/webhooks/1/x", "https://192.168.1.1/api/webhooks/1/x",
                    "https://discord.com.evil.test/api/webhooks/1/x", "https://discord.com/other"):
            assert c.put("/api/config", json={"webhook_url": bad}, headers=h).status_code == 400, bad
        ok = c.put("/api/config", json={"webhook_url": "https://discord.com/api/webhooks/1/abc"}, headers=h)
        assert ok.status_code == 200
        c.put("/api/config", json={"web": {"host": "0.0.0.0", "port": 1}}, headers=h)
        raw = json.loads(config_mod.CONFIG_FILE.read_text(encoding="utf-8"))
        assert raw["web"].get("host") != "0.0.0.0" and raw["web"].get("port") != 1


def test_security_headers_and_no_docs(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, {}) as c:
        r = c.get("/")
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path).status_code == 404, path


def test_broken_config_returns_error_instead_of_defaults(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, {"auth_token": TOKEN}) as c:
        config_mod.CONFIG_FILE.write_text("{ broken", encoding="utf-8")
        h = {"X-Auth-Token": TOKEN}
        assert c.get("/api/config", headers=h).status_code == 500
        assert c.put("/api/config", json={"alert_threshold": 5}, headers=h).status_code == 500
        assert config_mod.CONFIG_FILE.read_text(encoding="utf-8") == "{ broken"  # 上書きしない
