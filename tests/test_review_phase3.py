"""コードレビュー (2026-10-07) 段階 3（SQLite 履歴とその上の機能）のテスト。"""

from __future__ import annotations

import asyncio
import json
import stat
from datetime import datetime, timezone

import pytest

from poteto_monitor import config as config_mod
from poteto_monitor.config import ConfigError, parse_config
from poteto_monitor.history import HistoryStore, sample_base
from poteto_monitor.models import Reading

T0 = 1_791_360_000  # 2026-10-07 頃


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


# ── HistoryStore ─────────────────────────────────────────────────────
def test_value_at_respects_base_and_tolerance(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.add([("crypto:bitcoin", "usd", T0, 100.0), ("crypto:bitcoin", "jpy", T0, 15000.0),
               ("crypto:bitcoin", "usd", T0 + 600, 105.0)])
    assert store.value_at("crypto:bitcoin", "usd", T0 + 300, tolerance=600) == 100.0
    assert store.value_at("crypto:bitcoin", "jpy", T0 + 300, tolerance=600) == 15000.0
    assert store.value_at("crypto:bitcoin", "usd", T0 + 700, tolerance=600) == 105.0
    assert store.value_at("crypto:bitcoin", "usd", T0 - 1, tolerance=600) is None  # それ以前は無い
    assert store.value_at("crypto:bitcoin", "usd", T0 + 5000, tolerance=600) is None  # 古すぎる


def test_db_file_is_private(tmp_path):
    path = tmp_path / "h.db"
    HistoryStore(path).add([("a", "", T0, 1.0)])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_series_downsamples_and_prune(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.add([("hl:HYPE", "", T0 + i * 60, float(i)) for i in range(1000)])
    pts = store.series("hl:HYPE", "", since=T0, until=T0 + 60_000, max_points=100)
    assert 50 <= len(pts) <= 101
    assert pts == sorted(pts)
    assert store.prune(T0 + 500 * 60) == 500
    assert store.series("hl:HYPE", "", since=T0, until=T0 + 60_000, max_points=10_000)[0][0] == T0 + 500 * 60


def test_meta_roundtrip(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    assert store.get_meta("last_report_at") is None
    store.set_meta("last_report_at", "123")
    assert HistoryStore(tmp_path / "h.db").get_meta("last_report_at") == "123"


def test_sample_base():
    assert sample_base("crypto", "jpy") == "jpy"
    assert sample_base("forex", "jpy") == ""


def test_import_v1_and_v2_history(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    v1 = [{"timestamp": _dt(T0).isoformat(), "bitcoin_usd": 100, "bitcoin_jpy": 15000, "ethereum_usd": 10}]
    v2 = [{"timestamp": _dt(T0 + 3600).isoformat(), "values": {
        "crypto:bitcoin": {"usd": 110, "jpy": 16000},
        "forex:USDJPY": {"USDJPY": 150.0},
        "hl:HYPE": {"usd": 30.0},
        "ratio:jpyc/usd-coin": {"ratio": 0.0066, "jpyc_usd": 0.0066, "usd-coin_usd": 1.0},
    }}]
    assert store.import_entries(v1 + v2) == 8
    assert store.value_at("crypto:bitcoin", "jpy", T0, 0) == 15000
    assert store.value_at("crypto:ethereum", "usd", T0, 0) == 10
    assert store.value_at("hl:HYPE", "", T0 + 3600, 0) == 30.0
    assert store.value_at("ratio:jpyc/usd-coin", "", T0 + 3600, 0) == 0.0066
    assert store.import_entries(v1) == 0  # 取り込み済みは重複しない


# ── config ───────────────────────────────────────────────────────────
def test_new_config_defaults_and_history_limit_ignored():
    cfg = parse_config({"history_limit": 0})
    assert (cfg.retention_days, cfg.alert_window, cfg.alert_cooldown) == (30, 3600, 3600)


@pytest.mark.parametrize("raw", [{"retention_days": 0}, {"alert_window": 30}, {"alert_cooldown": -1}])
def test_new_config_validation(raw):
    with pytest.raises(ConfigError):
        parse_config(raw)


# ── poller ───────────────────────────────────────────────────────────
pytest.importorskip("fastapi")
from poteto_monitor.providers import RateLimitError, Source, SourceResult  # noqa: E402
from poteto_monitor.web import poller as poller_mod  # noqa: E402
from poteto_monitor.web.context import AppContext  # noqa: E402
from poteto_monitor.web.fetcher import SourceScheduler  # noqa: E402

WEBHOOK = "https://discord.com/api/webhooks/x/y"


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self):
        return self.now


class Feed:
    def __init__(self, value: float):
        self.value = value
        self.exc: Exception | None = None

    def __call__(self, assets, base_currency, session=None, api_key=""):
        if self.exc:
            raise self.exc
        return SourceResult(readings=[
            Reading(key=a.key, label=a.label, emoji="", value=self.value, display=str(self.value),
                    threshold=a.threshold, type=a.type) for a in assets])


def _ctx(tmp_path, monkeypatch, feed, clock, **raw):
    monkeypatch.setattr(poller_mod, "PRICES_FILE", tmp_path / "prices.json")
    sent: list = []
    monkeypatch.setattr(poller_mod, "send", lambda url, embeds: sent.append(embeds))
    cfg = parse_config({"webhook_url": WEBHOOK, "report_interval": 0,
                        "watch": [{"type": "hyperliquid", "coin": "HYPE"}], **raw})
    ctx = AppContext(cfg, history=HistoryStore(tmp_path / "h.db"))
    ctx.fetcher = SourceScheduler({"hyperliquid": Source(types=("hyperliquid",), fetch=feed)}, clock=clock)
    return ctx, sent


def _run(ctx, *ticks):
    async def scenario():
        for ts in ticks:
            ctx.fetcher._clock.now = ts
            await poller_mod._tick(ctx, _dt(ts))
            if ctx.background:
                await asyncio.wait(ctx.background)

    asyncio.run(scenario())


def _titles(sent):
    return [e["title"] for msg in sent for e in msg]


def test_alert_compares_with_window_not_previous_poll(tmp_path, monkeypatch):
    feed = Feed(100.0)
    ctx, sent = _ctx(tmp_path, monkeypatch, feed, Clock(T0))
    _run(ctx, T0)
    # 60 秒ごとに少しずつ上がり、1 時間で +12%（1 回あたりは 10% 未満）。
    for i in range(1, 61):
        feed.value = 100.0 + 0.2 * i
        _run(ctx, T0 + 60 * i)
    alerts = [t for t in _titles(sent) if "大きな変動" in t]
    assert len(alerts) == 1


def test_alert_cooldown_survives_restart(tmp_path, monkeypatch):
    feed = Feed(100.0)
    ctx, sent = _ctx(tmp_path, monkeypatch, feed, Clock(T0))
    _run(ctx, T0)
    feed.value = 120.0
    _run(ctx, T0 + 3600)
    ctx2, sent2 = _ctx(tmp_path, monkeypatch, feed, Clock(T0 + 3660))
    _run(ctx2, T0 + 3660)  # 再起動直後: まだクールダウン中
    assert sum("大きな変動" in t for t in _titles(sent)) == 1
    assert not any("大きな変動" in t for t in _titles(sent2))


def test_report_not_resent_after_restart_and_compares_with_last_report(tmp_path, monkeypatch):
    feed = Feed(100.0)
    ctx, sent = _ctx(tmp_path, monkeypatch, feed, Clock(T0), report_interval=3600)
    _run(ctx, T0)
    assert _titles(sent) == ["📊 マーケット定期レポート"]
    feed.value = 103.0
    ctx2, sent2 = _ctx(tmp_path, monkeypatch, feed, Clock(T0 + 60), report_interval=3600)
    _run(ctx2, T0 + 60, T0 + 1800)
    assert sent2 == []  # 再起動しても間隔内は送らない
    _run(ctx2, T0 + 3600)
    report = sent2[-1][0]
    assert report["title"] == "📊 マーケット定期レポート"
    assert "+3.00%" in report["fields"][0]["value"]  # 前回レポート (100) 比


def test_change_24h_in_snapshot(tmp_path, monkeypatch):
    feed = Feed(100.0)
    ctx, _ = _ctx(tmp_path, monkeypatch, feed, Clock(T0))
    _run(ctx, T0)
    assert ctx.state.snapshot()["assets"][0]["change_24h"] is None
    feed.value = 90.0
    _run(ctx, T0 + 86400)
    asset = ctx.state.snapshot()["assets"][0]
    assert asset["change_24h"] == pytest.approx(-10.0)
    assert asset["sampled_at"] == _dt(T0 + 86400).isoformat()


def test_outage_and_recovery_notifications(tmp_path, monkeypatch):
    feed = Feed(100.0)
    ctx, sent = _ctx(tmp_path, monkeypatch, feed, Clock(T0))
    _run(ctx, T0)
    feed.exc = RateLimitError("429", retry_after=60)
    _run(ctx, T0 + 60, T0 + 300)
    assert not any("障害" in t for t in _titles(sent))  # 10 分未満は通知しない
    _run(ctx, T0 + 700)
    assert sum("障害" in t for t in _titles(sent)) == 1
    _run(ctx, T0 + 800)
    assert sum("障害" in t for t in _titles(sent)) == 1  # 続報は送らない
    feed.exc = None
    _run(ctx, T0 + 900)
    assert any("復旧" in t for t in _titles(sent))


def test_samples_recorded_only_when_fresh(tmp_path, monkeypatch):
    feed = Feed(100.0)
    ctx, _ = _ctx(tmp_path, monkeypatch, feed, Clock(T0), intervals={"hyperliquid": 300})
    _run(ctx, T0, T0 + 60, T0 + 120)
    assert len(ctx.history.series("hl:HYPE", "", T0 - 1, T0 + 1000, max_points=1000)) == 1


# ── API ──────────────────────────────────────────────────────────────
from fastapi.testclient import TestClient  # noqa: E402

from poteto_monitor.web import server as server_mod  # noqa: E402


@pytest.fixture
def api(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_FILE", cfg_file)
    monkeypatch.setattr(poller_mod, "PRICES_FILE", tmp_path / "prices.json")
    cfg_file.write_text(json.dumps({"poll_interval": 300, "report_interval": 0,
                                    "watch": [{"type": "hyperliquid", "coin": "HYPE"}]}), encoding="utf-8")
    ctx = AppContext(config_mod.load_config(), history=HistoryStore(tmp_path / "h.db"))
    feed = Feed(100.0)
    ctx.fetcher = SourceScheduler({"hyperliquid": Source(types=("hyperliquid",), fetch=feed)})
    with TestClient(server_mod.create_app(ctx)) as c:
        c.feed = feed
        c.ctx = ctx
        yield c


def test_healthz(api):
    import time

    for _ in range(50):  # ポーラーの初回取得を待つ
        if api.ctx.last_tick:
            break
        time.sleep(0.05)
    r = api.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    api.ctx.fetcher._states["hyperliquid"].failures = 1
    assert api.get("/healthz").status_code == 503


def test_history_api(api):
    import time

    now = int(time.time())
    api.ctx.history.add([("hl:HYPE", "", now - 3600 * i, 100.0 + i) for i in range(30)])
    body = api.get("/api/history", params={"range": "24h"}).json()
    pts = body["series"]["hl:HYPE"]
    assert pts and all(now - 86400 <= t <= now + 60 for t, _ in pts)
    assert api.get("/api/history", params={"range": "7d", "key": "hl:HYPE"}).status_code == 200
    assert api.get("/api/history", params={"range": "1y"}).status_code == 400
    assert api.get("/api/history", params={"key": "nope"}).status_code == 404


# ── 1 回実行モードと取り込み CLI ─────────────────────────────────────
def test_run_mode_records_to_db(tmp_path, monkeypatch):
    from poteto_monitor import monitor as monitor_mod

    monkeypatch.setattr(monitor_mod, "PRICES_FILE", tmp_path / "prices.json")
    monkeypatch.setattr(monitor_mod, "send", lambda url, embeds: None)
    monkeypatch.setattr(monitor_mod, "fetch_all", lambda assets, base, **kw: [
        Reading(key="crypto:bitcoin", label="BTC", emoji="", value=100.0, display="", threshold=10, type="crypto")])
    monitor_mod.run(parse_config({"webhook_url": WEBHOOK, "watch": [{"type": "crypto", "id": "bitcoin"}]}))
    store = HistoryStore(config_mod.HISTORY_DB)
    assert len(store.series("crypto:bitcoin", "usd", 0, 2**40, max_points=10)) == 1


def test_import_history_cli(tmp_path):
    from poteto_monitor.monitor import main

    src = tmp_path / "history.v1.json"
    src.write_text(json.dumps([{"timestamp": _dt(T0).isoformat(), "bitcoin_usd": 1}]), encoding="utf-8")
    assert main(["import-history", str(src)]) == 0
    assert HistoryStore(config_mod.HISTORY_DB).value_at("crypto:bitcoin", "usd", T0, 0) == 1


def test_concurrent_first_access_does_not_lock(tmp_path):
    """初回アクセスが複数スレッドで重なっても database is locked にならない（WAL 化の競合）。"""
    import threading

    for n in range(20):
        store = HistoryStore(tmp_path / f"race{n}.db")
        errors: list[Exception] = []
        barrier = threading.Barrier(8)

        def worker(i: int) -> None:
            try:
                barrier.wait()
                store.add([("k", "", T0 + i, float(i))])
                store.value_at("k", "", T0 + 10, 100)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
