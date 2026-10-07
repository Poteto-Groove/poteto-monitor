"""テスト共通: 履歴 DB を一時ディレクトリへ向ける（/var/lib に書かないため）。"""

from __future__ import annotations

import pytest

from poteto_monitor import config as config_mod


@pytest.fixture(autouse=True)
def _isolated_history_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "HISTORY_DB", tmp_path / "history.db")
