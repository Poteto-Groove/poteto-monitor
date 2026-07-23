"""前回価格 (prices.json) と履歴 (history.json) の入出力。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # アトミックに差し替え、書き込み途中の破損を防ぐ。


def load_previous(path: Path) -> dict[str, float]:
    """前回の代表値を {key: value} で返す。"""
    data = load_json(path, {})
    return data.get("values", {}) if isinstance(data, dict) else {}
