"""独立探针：不依赖 run.py，直接测某个视频活动的上报接受度。

    python tools/probe.py <账号名> <活动id> [课程id]
    python tools/probe.py acc01 92918 29066 --speed normal

输出里 recommend 字段就是建议写进 accounts.yaml 的 speed。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guet.config import ConfigError, load_config  # noqa: E402
from guet.probe import probe_activity  # noqa: E402
from guet.util import setup_logger  # noqa: E402

log = setup_logger("probe-cli")


def main() -> int:
    parser = argparse.ArgumentParser(description="TronClass 视频上报接受度探针")
    parser.add_argument("account", help="accounts.yaml 里的账号名")
    parser.add_argument("activity", type=int, help="视频活动 id")
    parser.add_argument("course", type=int, nargs="?", default=None, help="课程 id")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--accounts", type=Path, default=None)
    args = parser.parse_args()

    try:
        app = load_config(ROOT, args.config, args.accounts)
        account = app.account(args.account)
    except ConfigError as exc:
        log.error("配置错误：%s", exc)
        return 1

    result = probe_activity(app, account, args.activity, args.course)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    advice = result.get("recommend")
    if advice:
        log.info("建议在 accounts.yaml 里把该账号 speed 设为：%s", advice)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
