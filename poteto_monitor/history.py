"""価格の時系列履歴（SQLite）。

時間窓アラート・24 時間変化率・チャート・定期レポートの前回時刻などの土台。
接続は操作ごとに開いて閉じる（ポーラーのスレッドと API のスレッドから安全に使うため）。

基準通貨で値の単位が変わる crypto は ``base`` 列に基準通貨を、それ以外は空文字を入れる。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    key   TEXT    NOT NULL,
    base  TEXT    NOT NULL,
    ts    INTEGER NOT NULL,
    value REAL    NOT NULL,
    PRIMARY KEY (key, base, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
CREATE TABLE IF NOT EXISTS meta (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

Row = tuple[str, str, int, float]  # (key, base, ts, value)


def sample_base(asset_type: str, base_currency: str) -> str:
    """履歴の base 列に入れる値（基準通貨に依存するのは crypto だけ）。"""
    return base_currency if asset_type == "crypto" else ""


class HistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                # 他ユーザーから読めないよう 600 で作る。
                os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        conn = sqlite3.connect(self.path, timeout=10)
        if not self._ready:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._ready = True
        return conn

    # ── 書き込み ─────────────────────────────────────────────────────
    def add(self, rows: Iterable[Row]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with closing(self._connect()) as conn, conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO samples (key, base, ts, value) VALUES (?, ?, ?, ?)", rows
            )
            return cur.rowcount

    def prune(self, before_ts: int) -> int:
        with closing(self._connect()) as conn, conn:
            return conn.execute("DELETE FROM samples WHERE ts < ?", (before_ts,)).rowcount

    def set_meta(self, name: str, value: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO meta (name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, value),
            )

    # ── 読み出し ─────────────────────────────────────────────────────
    def get_meta(self, name: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM meta WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def value_at(self, key: str, base: str, ts: int, tolerance: int) -> float | None:
        """ts 以前で最も新しい値。ts - tolerance より古いものしか無ければ None。"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value FROM samples WHERE key = ? AND base = ? AND ts <= ? AND ts >= ? "
                "ORDER BY ts DESC LIMIT 1",
                (key, base, ts, ts - tolerance),
            ).fetchone()
        return row[0] if row else None

    def values_at(self, pairs: Iterable[tuple[str, str]], ts: int, tolerance: int) -> dict[str, float]:
        """(key, base) ごとの value_at をまとめて返す（見つからないキーは含めない）。"""
        out: dict[str, float] = {}
        for key, base in pairs:
            value = self.value_at(key, base, ts, tolerance)
            if value is not None:
                out[key] = value
        return out

    def series(self, key: str, base: str, since: int, until: int, max_points: int = 240) -> list[tuple[int, float]]:
        """[since, until] の値を時刻順に返す。点が多いときは区間ごとの最終値に間引く。"""
        bucket = max(1, -(-(until - since) // max_points))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT MAX(ts), value FROM samples WHERE key = ? AND base = ? AND ts BETWEEN ? AND ? "
                "GROUP BY (ts - ?) / ? ORDER BY 1",
                (key, base, since, until, since, bucket),
            ).fetchall()
        return [(int(t), float(v)) for t, v in rows]

    # ── 旧 history.json の取り込み ──────────────────────────────────
    def import_entries(self, entries: Iterable[Any]) -> int:
        """v1（``bitcoin_usd`` 形式）と v2（``values`` 形式）の history.json を取り込む。重複は無視。"""
        rows: list[Row] = []
        for entry in entries:
            if not isinstance(entry, dict) or "timestamp" not in entry:
                continue
            try:
                ts = int(datetime.fromisoformat(str(entry["timestamp"])).timestamp())
            except ValueError:
                continue
            if isinstance(entry.get("values"), dict):
                rows.extend(_v2_rows(entry["values"], ts))
            else:
                for name, value in entry.items():
                    coin, _, currency = name.rpartition("_")
                    if name != "timestamp" and coin and currency and isinstance(value, (int, float)):
                        rows.append((f"crypto:{coin}", currency, ts, float(value)))
        return self.add(rows)


def _v2_rows(values: dict, ts: int) -> list[Row]:
    rows: list[Row] = []
    for key, fields in values.items():
        if not isinstance(fields, dict):
            continue
        nums = {k: float(v) for k, v in fields.items() if isinstance(v, (int, float))}
        if key.startswith("crypto:"):
            rows.extend((key, currency, ts, v) for currency, v in nums.items())
        elif key.startswith("hl:") and "usd" in nums:
            rows.append((key, "", ts, nums["usd"]))
        elif key.startswith("ratio:") and "ratio" in nums:
            rows.append((key, "", ts, nums["ratio"]))
        elif key.startswith("forex:") and len(nums) == 1:
            rows.append((key, "", ts, next(iter(nums.values()))))
        elif "value" in nums:
            rows.append((key, "", ts, nums["value"]))
    return rows
