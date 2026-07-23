"""ネットワークをモックした単体テスト。"""

from __future__ import annotations

import json

import pytest

from poteto_monitor import config as config_mod
from poteto_monitor.config import ConfigError, parse_config
from poteto_monitor.format import fmt_pct, money, pct_change, rate, trend_emoji
from poteto_monitor.notify import find_alerts
from poteto_monitor.providers import fetch_all
from poteto_monitor.models import Reading


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """URL に応じて CoinGecko / forex のレスポンスを返す最小スタブ。"""

    def get(self, url, params=None, timeout=None):
        if "coingecko" in url:
            return FakeResp(
                {
                    "bitcoin": {"usd": 100000.0, "jpy": 15000000.0},
                    "ethereum": {"usd": 4000.0, "jpy": 600000.0},
                }
            )
        if "er-api" in url:
            return FakeResp({"result": "success", "rates": {"JPY": 157.0, "EUR": 0.92}})
        raise AssertionError(f"unexpected url: {url}")


# ── format ────────────────────────────────────────────────────────────
def test_money_symbols():
    assert money(1234.5, "usd") == "$1,234.50"
    assert money(15000000, "jpy") == "¥15,000,000"
    assert money(0.0005, "btc").startswith("₿0.0005")
    assert money(12.0, "xyz") == "12.00 XYZ"


def test_rate_keeps_decimals_for_jpy():
    # 為替レートは JPY でも小数を残す（価格の money() とは異なる）。
    assert rate(157.23, "jpy") == "¥157.23"
    assert rate(0.92, "eur") == "€0.920000"


def test_pct_change_and_labels():
    assert pct_change(100, 110) == pytest.approx(10.0)
    assert pct_change(0, 5) is None
    assert fmt_pct(None) == "初回取得"
    assert fmt_pct(3.2).startswith("+")
    assert trend_emoji(None) == "🆕"
    assert trend_emoji(-6) == "💥"


# ── config ────────────────────────────────────────────────────────────
def test_default_watch_when_empty(monkeypatch):
    monkeypatch.delenv("ALERT_THRESHOLD", raising=False)
    cfg = parse_config({})
    keys = [a.key for a in cfg.assets]
    assert "crypto:bitcoin" in keys
    assert "forex:USDJPY" in keys


def test_forex_pair_shorthand():
    cfg = parse_config({"watch": [{"type": "forex", "pair": "USD/JPY"}]})
    asset = cfg.assets[0]
    assert asset.base == "USD" and asset.quote == "JPY"


def test_per_asset_threshold_default():
    cfg = parse_config(
        {"alert_threshold": 8, "watch": [{"type": "crypto", "id": "bitcoin"}]}
    )
    assert cfg.assets[0].threshold == 8


def test_duplicate_key_rejected():
    with pytest.raises(ConfigError):
        parse_config(
            {"watch": [{"type": "crypto", "id": "bitcoin"}, {"type": "crypto", "id": "bitcoin"}]}
        )


def test_env_override(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setenv("ALERT_THRESHOLD", "25")
    cfg = parse_config({"webhook_url": "unused", "alert_threshold": 5})
    assert cfg.webhook_url == "https://example.test/hook"
    assert cfg.alert_threshold == 25


# ── providers (mocked) ────────────────────────────────────────────────
def test_fetch_all_mixed():
    cfg = parse_config(
        {
            "watch": [
                {"type": "crypto", "id": "bitcoin", "label": "BTC", "vs": ["usd", "jpy"]},
                {"type": "forex", "base": "USD", "quote": "JPY", "label": "USDJPY"},
            ]
        }
    )
    readings = fetch_all(cfg.assets, cfg.base_currency, session=FakeSession())
    assert [r.key for r in readings] == ["crypto:bitcoin", "forex:USDJPY"]
    assert readings[0].value == 100000.0
    assert "¥" in readings[0].display  # jpy が括弧内に出る
    assert readings[1].value == 157.0


# ── alerts ────────────────────────────────────────────────────────────
def test_find_alerts_respects_threshold():
    readings = [
        Reading(key="a", label="A", emoji="🟡", value=120.0, display="", threshold=10),
        Reading(key="b", label="B", emoji="🔷", value=105.0, display="", threshold=10),
    ]
    previous = {"a": 100.0, "b": 100.0}
    alerts = find_alerts(readings, previous)
    assert [r.key for r, _ in alerts] == ["a"]


def test_no_alert_on_first_run():
    readings = [Reading(key="a", label="A", emoji="🟡", value=120.0, display="", threshold=1)]
    assert find_alerts(readings, {}) == []
