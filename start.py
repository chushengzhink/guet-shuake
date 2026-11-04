#!/usr/bin/env python
"""一键向导：登录 → 自动拉课程 → 选课程 → 开刷。

    python start.py            # 全自动向导
    python start.py --yes      # 全部用默认答案（不提问，适合挂机）

流程：
  1. 检查 Chromium
  2. 没有 accounts.yaml / config.yaml 就现建
  3. 逐个账号检查会话，失效就弹出浏览器让你登录一次
  4. 自动拉「我的课程」并显示完成度
  5. 你选课程（1 / 1,3 / all）
  6. 显示待处理内容与预计耗时，选节奏
  7. 开刷，打印汇总，问要不要继续刷别的课
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guet.util import fix_console_encoding  # noqa: E402
from guet.wizard import run_wizard  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    fix_console_encoding()
    parser = argparse.ArgumentParser(
        prog="start.py",
        description="GUET 畅课后台刷课一键向导",
    )
    parser.add_argument("--config", type=Path, default=None, help="config.yaml 路径")
    parser.add_argument("--accounts", type=Path, default=None, help="accounts.yaml 路径")
    parser.add_argument("--yes", action="store_true",
                        help="不提问，全用默认答案（全部账号、回车默认的课程与节奏）")
    args = parser.parse_args(argv)

    try:
        return run_wizard(
            ROOT,
            config_path=args.config,
            accounts_path=args.accounts,
            assume_yes=args.yes,
        )
    except KeyboardInterrupt:
        print("\n已中断。进度保存在 data/state.<账号>.json，重新运行会接着做。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
