"""离线演示：把向导接到假服务端，完整跑一遍「选账号 → 选课 → 选节奏 → 刷」。

不联网、不需要登录，用来先看一眼向导长什么样：

    python tools/demo_wizard.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from guet import scheduler, wizard  # noqa: E402
from guet.client import TronClassClient  # noqa: E402
from guet.util import shutdown_loggers  # noqa: E402
from test_integration import FakeTransport, fixture  # noqa: E402


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="guet-demo-"))
    (root / "accounts.yaml").write_text(
        'defaults:\n  course_ids: []\n  semester_id: "14"\n  academic_year_id: "5"\n'
        "accounts:\n  - name: acc01\n    enabled: true\n"
        "  - name: acc02\n    enabled: true\n",
        encoding="utf-8",
    )
    (root / "config.yaml").write_text(
        "rps: 3\ngap_seconds: [0, 0]\nchunk_seconds: 600\ntelemetry: false\n",
        encoding="utf-8",
    )

    activities = fixture("activities.json")["activities"]
    reads = {int(r["activity_id"]): r for r in fixture("activity_reads.json")["activity_reads"]}
    transport = FakeTransport(activities, reads)

    wizard.session.cookies_from_profile = lambda app, acc: {"session": "fake"}  # type: ignore[assignment]

    def make_client(app, account, limiter, global_limiter):
        return TronClassClient(transport, app.runtime, limiter=limiter), transport

    scheduler.make_client = make_client  # type: ignore[assignment]

    answers = ["1", "", "", "", "y", ""]

    def ask(prompt: str) -> str:
        answer = answers.pop(0) if answers else ""
        print(f"  ? {prompt}{answer or '（回车）'}")
        return answer

    print("下面是一次完整向导的演示（假服务端）：\n")
    code = wizard.run_wizard(root, ask=ask)
    print(f"\n[演示结束] 退出码 = {code}")
    shutdown_loggers()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
