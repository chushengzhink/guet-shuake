#!/usr/bin/env python
"""打包入口 / 直接运行入口：GUET 畅课助手（图形界面）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guet.gui.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
