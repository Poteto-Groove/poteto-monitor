"""``python -m poteto_monitor`` のエントリポイント。"""

import sys

from .monitor import main

if __name__ == "__main__":
    sys.exit(main())
