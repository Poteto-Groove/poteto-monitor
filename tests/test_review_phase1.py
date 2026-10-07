"""コードレビュー (2026-10-07) 段階 1 の再現テスト。"""

from __future__ import annotations

import asyncio
import stat

import pytest

from poteto_monitor.config import ConfigError, parse_config, write_raw
from poteto_monitor.format import money
from poteto_monitor.models import Reading


def _reading(key: str, value: float, type_: str = "crypto") -> Reading:
    return Reading(key=key, label=key, emoji="", value=value, display="", threshold=10, type=type_)


# ── B-8: 小さな価格の表示 ─────────────────────────────────────────────
def test_money_tiny_values_keep_significant_digits():
    assert money(1.234e-07, "usd") == "$0.0000001234"
    assert money(1.234e-05, "usd") == "$0.00001234"
    assert money(0.5, "usd") == "$0.5"


# ── V-1〜V-3: 入力検証 ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw",
    [
        {"alert_threshold": None},
        {"poll_interval": "abc"},
        {"report_interval": [1]},
        {"web": {"port": "x"}},
        {"watch": [{"type": "crypto", "id": "bitcoin", "threshold": "high"}]},
    ],
)
def test_invalid_types_raise_config_error(raw, monkeypatch):
    monkeypatch.delenv("ALERT_THRESHOLD", raising=False)
    monkeypatch.delenv("POLL_INTERVAL", raising=False)
    monkeypatch.delenv("WEB_PORT", raising=False)
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_vs_string_is_not_split_into_chars():
    one = parse_config({"watch": [{"type": "crypto", "id": "bitcoin", "vs": "usd"}]}).assets[0]
    assert one.vs == ("usd",)
    two = parse_config({"watch": [{"type": "crypto", "id": "bitcoin", "vs": "usd, jpy"}]}).assets[0]
    assert two.vs == ("usd", "jpy")


@pytest.mark.parametrize("days", [0, -1])
def test_retention_days_must_be_positive(days):
    # 段階 3 で history_limit は retention_days に置き換え（history_limit は無視される）。
    with pytest.raises(ConfigError):
        parse_config({"retention_days": days})


# ── S-1: config.json のパーミッション ────────────────────────────────
def test_write_raw_creates_file_with_0600(tmp_path):
    path = tmp_path / "config.json"
    write_raw({"watch": [{"type": "crypto", "id": "bitcoin"}]}, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_raw_keeps_0600_on_overwrite(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    # 前回の一時ファイルが緩い権限で残っていても引き継がない。
    stale = tmp_path / "config.json.tmp"
    stale.write_text("", encoding="utf-8")
    stale.chmod(0o644)
    write_raw({"watch": [{"type": "crypto", "id": "bitcoin"}]}, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ── B-3 / B-5: ライブ状態の変化率 ────────────────────────────────────
def test_snapshot_change_survives_repeated_snapshots():
    from poteto_monitor.web.state import LiveState

    state = LiveState()
    state.update([_reading("a", 100.0)], "t1", "usd")
    state.update([_reading("a", 110.0)], "t2", "usd")
    assert state.snapshot()["assets"][0]["change_pct"] == pytest.approx(10.0)
    assert state.snapshot()["assets"][0]["change_pct"] == pytest.approx(10.0)


def test_base_currency_change_drops_only_base_dependent_previous():
    from poteto_monitor.web.state import LiveState

    state = LiveState()
    state.update([_reading("btc", 100.0), _reading("fx", 150.0, "forex")], "t1", "usd")
    assert state.comparable_previous("usd") == {"btc": 100.0, "fx": 150.0}
    assert state.comparable_previous("jpy") == {"fx": 150.0}

    state.update([_reading("btc", 15000.0), _reading("fx", 151.5, "forex")], "t2", "jpy")
    changes = {a["key"]: a["change_pct"] for a in state.snapshot()["assets"]}
    assert changes["btc"] is None
    assert changes["fx"] == pytest.approx(1.0)


def test_poller_no_false_alert_after_base_currency_change(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from poteto_monitor.providers import Source, SourceResult
    from poteto_monitor.web import poller as poller_mod
    from poteto_monitor.web.context import AppContext

    prices = {"usd": 100.0, "jpy": 15000.0}

    def fake_fetch(assets, base_currency, session=None, api_key=""):
        return SourceResult(readings=[_reading(a.key, prices[base_currency]) for a in assets])

    sent: list = []
    monkeypatch.setattr(poller_mod, "send", lambda url, embeds: sent.append(embeds))
    monkeypatch.setattr(poller_mod, "PRICES_FILE", tmp_path / "prices.json")

    raw = {"webhook_url": "https://discord.com/api/webhooks/x/y", "report_interval": 0,
           "watch": [{"type": "crypto", "id": "bitcoin"}]}

    async def scenario():
        ctx = AppContext(parse_config(raw))
        ctx.fetcher.sources = {"fake": Source(types=("crypto",), fetch=fake_fetch)}
        await poller_mod._tick(ctx)
        ctx.config = parse_config({**raw, "base_currency": "jpy"})
        await poller_mod._tick(ctx)
        await ctx.close()

    asyncio.run(scenario())
    assert sent == []


# ── S-3: 更新要求の間引き ────────────────────────────────────────────
def test_refresh_is_throttled():
    pytest.importorskip("fastapi")
    from poteto_monitor.web.context import AppContext

    ctx = AppContext(parse_config({"watch": [{"type": "crypto", "id": "bitcoin"}]}))
    assert ctx.trigger_refresh() is True
    assert ctx.trigger_refresh() is False


# ── B-7: 停止時に SSE 接続を待ち続けない ─────────────────────────────
def test_serve_sets_graceful_shutdown_timeout(monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")
    from poteto_monitor.monitor import serve

    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    serve(parse_config({"watch": [{"type": "crypto", "id": "bitcoin"}]}))
    assert captured.get("timeout_graceful_shutdown") is not None
