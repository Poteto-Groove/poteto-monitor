"""後方互換シム。

旧構成では `python monitor.py` で実行していました。本体は `poteto_monitor`
パッケージへ移行済みです。このファイルはそのまま呼び出せるよう残しています。
"""

import sys

from poteto_monitor.monitor import main

if __name__ == "__main__":
    sys.exit(main())
