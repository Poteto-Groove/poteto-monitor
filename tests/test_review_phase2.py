"""コードレビュー (2026-10-07) 段階 2（API 制限対策）のテスト。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from poteto_monitor.config import ConfigError, parse_config
from poteto_monitor.models import Reading
from poteto_monitor.providers import (
    ProviderError,
    RateLimitError,
    Source,
    SourceResult,
    fetch_all,
    fetch_coingecko,
    fetch_forex,
)


class Resp:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class RecordingSession:
    def __init__(self, resp):
        self.resp = resp
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self.resp


CG_PAYLOAD = {
    "bitcoin": {"usd": 100000.0, "jpy": 15000000.0, "last_updated_at": 1791360000},
    "jpyc": {"usd": 0.0066, "last_updated_at": 1791359000},
    "usd-coin": {"usd": 1.0, "last_updated_at": 1791360000},
}


def _assets(watch):
    return parse_config({"watch": watch}).assets


# ── E-2: CoinGecko 呼び出しの統合と Demo キー ─────────────────────────
def test_coingecko_single_request_for_crypto_and_ratio():
    session = RecordingSession(Resp(CG_PAYLOAD))
    assets = _assets([
        {"type": "crypto", "id": "bitcoin"},
        {"type": "ratio", "num": "jpyc", "den": "usd-coin"},
    ])
    result = fetch_coingecko(assets, "usd", session=session)
    assert len(session.calls) == 1
    assert set(session.calls[0]["params"]["ids"].split(",")) == {"bitcoin", "jpyc", "usd-coin"}
    assert [r.key for r in result.readings] == ["crypto:bitcoin", "ratio:jpyc/usd-coin"]
    assert result.readings[1].as_of.startswith("2026-10-07")  # 古い方の時刻


def test_coingecko_api_key_header_only_when_set():
    assets = _assets([{"type": "crypto", "id": "bitcoin"}])
    with_key = RecordingSession(Resp(CG_PAYLOAD))
    fetch_coingecko(assets, "usd", session=with_key, api_key="CG-demo")
    assert with_key.calls[0]["headers"] == {"x-cg-demo-api-key": "CG-demo"}
    without = RecordingSession(Resp(CG_PAYLOAD))
    fetch_coingecko(assets, "usd", session=without)
    assert without.calls[0]["headers"] == {}


def test_coingecko_api_key_from_env_only(monkeypatch):
    monkeypatch.setenv("COINGECKO_API_KEY", " CG-demo ")
    assert parse_config({}).coingecko_api_key == "CG-demo"


# ── B-6: 銘柄単位の失敗の切り分け ────────────────────────────────────
def test_unknown_coin_does_not_break_other_coins():
    assets = _assets([{"type": "crypto", "id": "bitcoin"}, {"type": "crypto", "id": "bitcoinn"}])
    result = fetch_coingecko(assets, "usd", session=RecordingSession(Resp(CG_PAYLOAD)))
    assert [r.key for r in result.readings] == ["crypto:bitcoin"]
    assert "bitcoinn" in result.errors["crypto:bitcoinn"]


def test_fetch_all_still_raises_on_any_error_for_run_mode():
    assets = _assets([{"type": "crypto", "id": "bitcoinn"}])
    with pytest.raises(ProviderError):
        fetch_all(assets, "usd", session=RecordingSession(Resp(CG_PAYLOAD)))


# ── E-1 / E-9: 為替の次回更新時刻と 429 ──────────────────────────────
def test_forex_reports_next_update_and_as_of():
    payload = {"result": "success", "rates": {"JPY": 150.0},
               "time_last_update_unix": 1791331201, "time_next_update_unix": 1791417601}
    assets = _assets([{"type": "forex", "pair": "USD/JPY"}])
    result = fetch_forex(assets, session=RecordingSession(Resp(payload)))
    assert result.next_update == 1791417601
    assert result.readings[0].as_of is not None


def test_rate_limit_uses_retry_after_or_provider_default():
    assets = _assets([{"type": "forex", "pair": "USD/JPY"}])
    with pytest.raises(RateLimitError) as exc:
        fetch_forex(assets, session=RecordingSession(Resp({}, 429)))
    assert exc.value.retry_after == 1200
    with pytest.raises(RateLimitError) as exc:
        fetch_forex(assets, session=RecordingSession(Resp({}, 429, {"Retry-After": "90"})))
    assert exc.value.retry_after == 90


# ── E-3: intervals 設定 ──────────────────────────────────────────────
def test_intervals_defaults_and_override():
    cfg = parse_config({"intervals": {"coingecko": 600}})
    assert cfg.intervals == {"coingecko": 600, "forex": 3600, "hyperliquid": 0}


@pytest.mark.parametrize("raw", [{"intervals": {"binance": 5}}, {"intervals": {"forex": -1}}, {"intervals": 5}])
def test_invalid_intervals_rejected(raw):
    with pytest.raises(ConfigError):
        parse_config(raw)


# ── スケジューラ（常駐モード）──────────────────────────────────────
fastapi = pytest.importorskip("fastapi")
from poteto_monitor.web.fetcher import SourceScheduler  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class FakeSource:
    """呼び出し回数を数え、指定どおりに値・例外を返すソース。"""

    def __init__(self, value=100.0, next_update=None):
        self.calls = 0
        self.value = value
        self.exc: Exception | None = None
        self.next_update = next_update

    def __call__(self, assets, base_currency, session=None, api_key=""):
        self.calls += 1
        if self.exc:
            raise self.exc
        readings = [Reading(key=a.key, label=a.label, emoji="", value=self.value, display="",
                            threshold=a.threshold, type=a.type) for a in assets]
        return SourceResult(readings=readings, next_update=self.next_update)


def _scheduler(clock, **sources):
    types = {"coingecko": ("crypto", "ratio"), "forex": ("forex",), "hyperliquid": ("hyperliquid",)}
    return SourceScheduler({n: Source(types=types[n], fetch=f) for n, f in sources.items()}, clock=clock)


WATCH = [{"type": "crypto", "id": "bitcoin"}, {"type": "forex", "pair": "USD/JPY"},
         {"type": "hyperliquid", "coin": "HYPE"}]


def test_scheduler_respects_per_source_intervals():
    clock = Clock()
    cg, fx, hl = FakeSource(), FakeSource(next_update=clock.now + 86400), FakeSource()
    sched = _scheduler(clock, coingecko=cg, forex=fx, hyperliquid=hl)
    cfg = parse_config({"watch": WATCH})

    first = asyncio.run(sched.fetch(cfg))
    assert first.fresh == {"crypto:bitcoin", "forex:USDJPY", "hl:HYPE"}

    clock.now += 60
    second = asyncio.run(sched.fetch(cfg))
    assert second.fresh == {"hl:HYPE"}  # coingecko(300s) と forex(次回更新待ち) は取りに行かない
    assert [r.key for r in second.readings] == ["crypto:bitcoin", "forex:USDJPY", "hl:HYPE"]

    clock.now += 300
    asyncio.run(sched.fetch(cfg))
    assert (cg.calls, fx.calls, hl.calls) == (2, 1, 3)

    clock.now += 86400
    asyncio.run(sched.fetch(cfg))
    assert fx.calls == 2


def test_scheduler_keeps_stale_value_and_backs_off_on_failure():
    clock = Clock()
    cg, fx = FakeSource(), FakeSource()
    sched = _scheduler(clock, coingecko=cg, forex=fx)
    cfg = parse_config({"watch": WATCH[:2], "intervals": {"forex": 0, "coingecko": 0}})
    asyncio.run(sched.fetch(cfg))

    fx.exc = RateLimitError("429", retry_after=None)
    clock.now += 10
    out = asyncio.run(sched.fetch(cfg))
    assert out.fresh == {"crypto:bitcoin"}  # 為替の失敗で BTC は止まらない
    assert [r.key for r in out.readings] == ["crypto:bitcoin", "forex:USDJPY"]  # 古い値を残す
    assert "forex:USDJPY" in out.errors

    fx.exc = None
    clock.now += 300  # 429 の既定待ち（600 秒）より前は取りに行かない
    out = asyncio.run(sched.fetch(cfg))
    assert fx.calls == 2 and "forex:USDJPY" in out.errors
    clock.now += 400
    out = asyncio.run(sched.fetch(cfg))
    assert fx.calls == 3 and out.errors == {}


def test_scheduler_refetches_immediately_when_assets_change():
    clock = Clock()
    cg = FakeSource()
    sched = _scheduler(clock, coingecko=cg)
    asyncio.run(sched.fetch(parse_config({"watch": WATCH[:1]})))
    clock.now += 10
    out = asyncio.run(sched.fetch(parse_config({"watch": WATCH[:1], "base_currency": "jpy"})))
    assert cg.calls == 2 and out.fresh == {"crypto:bitcoin"}


def test_scheduler_reuses_session_per_source():
    clock = Clock()
    sessions = []

    def capture(assets, base_currency, session=None, api_key=""):
        sessions.append(session)
        return SourceResult()

    sched = SourceScheduler({"hyperliquid": Source(types=("hyperliquid",), fetch=capture)}, clock=clock)
    cfg = parse_config({"watch": WATCH[2:]})
    asyncio.run(sched.fetch(cfg))
    asyncio.run(sched.fetch(cfg))
    assert sessions[0] is sessions[1] and sessions[0] is not None
    sched.close()


# ── E-6 / E-7: 通知の非同期化と前回値の復元 ──────────────────────────
def test_poller_restores_previous_and_alerts_in_background(tmp_path, monkeypatch):
    from poteto_monitor.web import poller as poller_mod
    from poteto_monitor.web.context import AppContext

    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"base_currency": "usd", "values": {"crypto:bitcoin": 100.0}}), encoding="utf-8")
    monkeypatch.setattr(poller_mod, "PRICES_FILE", prices)
    sent: list = []
    monkeypatch.setattr(poller_mod, "send", lambda url, embeds: sent.append(embeds))

    raw = {"webhook_url": "https://discord.com/api/webhooks/x/y", "report_interval": 0,
           "watch": WATCH[:1]}

    async def scenario():
        ctx = AppContext(parse_config(raw))
        ctx.fetcher = _scheduler(Clock(), coingecko=FakeSource(value=120.0))
        poller_mod._restore_previous(ctx)
        # 段階 3 以降、急変アラートは履歴 DB の alert_window 秒前の値と比べる。
        now = datetime.now(timezone.utc)
        ctx.history.add([("crypto:bitcoin", "usd", int(now.timestamp()) - 3600, 100.0)])
        await poller_mod._tick(ctx, now)
        snap = ctx.state.snapshot()
        await ctx.close()  # 送信タスクの完了を待つ
        return snap

    snap = asyncio.run(scenario())
    assert snap["assets"][0]["change_pct"] == pytest.approx(20.0)
    assert len(sent) == 1 and "大きな変動" in sent[0][0]["title"]
    saved = json.loads(prices.read_text(encoding="utf-8"))
    assert saved["base_currency"] == "usd" and saved["values"]["crypto:bitcoin"] == 120.0


def test_snapshot_marks_stale_assets_and_degraded_status():
    from poteto_monitor.web.state import LiveState

    state = LiveState()
    r1 = Reading(key="a", label="A", emoji="", value=1.0, display="", threshold=10, type="crypto")
    r2 = Reading(key="b", label="B", emoji="", value=2.0, display="", threshold=10, type="forex")
    state.update([r1, r2], "t", "usd", fresh={"a"}, errors={"b": "429"})
    snap = state.snapshot()
    assert snap["status"] == "degraded" and snap["error"] == "429"
    assert [a["stale"] for a in snap["assets"]] == [False, True]
