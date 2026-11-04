"""生成 README 用的界面截图：启动 GUI，注入演示数据，截 1200x900。

    python packaging\make_screenshot.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "docs" / "screenshot.png"

DEMO_JS = r"""
() => {
  for (let i = 1; i < 5000; i++) { clearInterval(i); clearTimeout(i); }
  const toastEl = document.getElementById('toast');
  if (toastEl) { toastEl.classList.remove('show'); toastEl.textContent = ''; }
  state.accounts = [
    {name: 'acc01', enabled: true, logged_in: true},
    {name: 'acc02', enabled: true, logged_in: true},
  ];
  state.browser = 'Microsoft Edge 156';
  state.status = 'running';
  state.courses = [
    {id: 29066, name: '基础工业工程（挑战性学习课程）-2610082', code: '2610082', completed: 63, total: 74, percent: 87.4},
    {id: 29117, name: '管理学原理-2610083', code: '2610083', completed: 12, total: 48, percent: 25.0},
    {id: 29142, name: '运筹学-2610084', code: '2610084', completed: 41, total: 52, percent: 78.8},
  ];
  state.selected = new Set([29066, 29142]);
  state.speed = 'probe';
  state.progress = {acc01: {done: 12, failed: 0, pending: 2}, acc02: {done: 5, failed: 0, pending: 0}};
  renderAccounts(); renderCourses(); renderSpeeds(); renderProgress(); renderRunState();
  document.getElementById('sessionState').textContent = '已登录 · acc01';
  document.getElementById('sessionState').className = 'status-pill running';
  document.getElementById('courseHint').textContent = '账号 acc01：共 3 门课程，勾选要刷的课。';
  const lines = [
    ['INFO', '14:47:02  [scheduler] 开始执行：2 个账号，并发 2，引擎 http，节奏 probe'],
    ['INFO', '14:47:03  [browser] 浏览器：msedge'],
    ['INFO', '14:47:05  [run.acc01] 课程 基础工业工程（挑战性学习课程）-2610082：共 60 个活动（视频 23 / 资料 18 / 跳过 16 / 人工 3），当前完整度 87.4%'],
    ['INFO', '14:47:06  [run.acc01]   → 92921 工艺程序分析 时长 9m54s，待补 5 段'],
    ['INFO', '14:47:18  [run.acc01]   ✓ 92921 工艺程序分析 → done（100%，已刷满）'],
    ['INFO', '14:47:19  [run.acc01]   ✓ 92922 流程程序分析概念 → done（100%，已刷满）'],
    ['INFO', '14:47:21  [run.acc01]   ✓ 92923 流程程序分析步骤和应用 → done（100%，已刷满）'],
    ['INFO', '14:47:24  [run.acc01]   ✓ 83743 时间研究 → done（100%，已全部已读）'],
    ['INFO', '14:47:26  [run.acc02] 课程 管理学原理-2610083：共 48 个活动（视频 21 / 资料 9 / 跳过 15 / 人工 3），当前完整度 25.0%'],
    ['INFO', '14:47:29  [run.acc02]   ✓ 88112 组织行为基础 → done（100%，已刷满）'],
    ['WARNING', '14:47:31  [run.acc02]   92918 服务端限制单段 ≤125s，自动改用 120s 段'],
    ['INFO', '14:47:33  [run.acc01] 课程 基础工业工程 处理结束：成功 12 / 失败 0 / 待续 2，用时 31s，请求 96 次'],
  ];
  const box = document.getElementById('log');
  box.innerHTML = '';
  for (const [lv, text] of lines) {
    const div = document.createElement('div');
    div.className = 'lv-' + lv;
    div.textContent = text;
    box.appendChild(div);
  }
  box.scrollTop = box.scrollHeight;
  return true;
}
"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT.parent.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="guet-shot-"))
    url_file = data_dir / "url.txt"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--no-window",
         "--data-dir", str(data_dir), "--port", "39240", "--url-file", str(url_file)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(40):
            if url_file.exists() and url_file.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.3)
        url = url_file.read_text(encoding="utf-8").strip()
        print("GUI:", url)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 980},
                                    device_scale_factor=2)
            page.goto(url, wait_until="load")
            page.wait_for_timeout(1200)
            page.evaluate(DEMO_JS)
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT))
            browser.close()
        print("写入", OUT, f"{OUT.stat().st_size / 1024:.0f} KB")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())

