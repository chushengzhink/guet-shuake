"""HAR 分析工具：从抓包复现接口清单、抽取样例、导出测试 fixtures。

    python tools/har_analyze.py <file.har>                 # 主机 / 端点数排行
    python tools/har_analyze.py <file.har> --endpoints      # 只看 courses 域端点
    python tools/har_analyze.py <file.har> --dump 855       # 打印某条请求的请求头/体/响应
    python tools/har_analyze.py <file.har> --export tests/fixtures
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

DOMAIN = "courses.guet.edu.cn"

FIXTURES = {
    "activities.json": 421,          # GET /api/courses/29066/activities
    "activity_reads.json": 890,      # GET /api/course/29066/activity-reads-for-user
    "my_completeness.json": 504,     # GET /api/course/29066/my-completeness
    "activity_92918.json": 820,      # GET /api/activities/92918
    "modules.json": 404,             # GET /api/courses/29066/modules
    "activity_read_92918.json": 833,  # GET /api/course/activity-read/92918
    "course_meta.json": 499,         # GET /api/courses/29066
}


def load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["log"]["entries"]


def hosts(entries: list[dict]) -> None:
    counter = Counter()
    for entry in entries:
        url = entry["request"]["url"]
        counter[url.split("/")[2] if "://" in url else url] += 1
    for host, count in counter.most_common(40):
        print(f"{count:5d}  {host}")


def endpoints(entries: list[dict]) -> None:
    counter = Counter()
    for entry in entries:
        url = entry["request"]["url"]
        if DOMAIN not in url:
            continue
        path = url.split(DOMAIN, 1)[1].split("?")[0]
        if path.startswith("/static") or "/api/uploads/" in path:
            continue
        if path.endswith((".js", ".css", ".png", ".jpg", ".ico", ".woff")):
            continue
        counter[(entry["request"]["method"], path)] += 1
    for (method, path), count in sorted(counter.items(), key=lambda item: item[0][1]):
        print(f"{count:4d}  {method:7s} {path}")


def dump(entries: list[dict], index: int) -> None:
    entry = entries[index]
    request, response = entry["request"], entry["response"]
    print("=" * 100)
    print(index, request["method"], response["status"], request["url"])
    print("-- request headers --")
    for header in request["headers"]:
        value = header["value"]
        print(f"  {header['name']}: {value[:200]}{'…' if len(value) > 200 else ''}")
    post = request.get("postData")
    if post:
        print("-- request body --")
        print((post.get("text") or "")[:3000])
    print("-- response --")
    print((response.get("content", {}).get("text") or "")[:3000])


def export(entries: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, index in FIXTURES.items():
        text = entries[index]["response"]["content"].get("text") or ""
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError:
            print(f"跳过 {name}：第 {index} 条不是 JSON")
            continue
        (out_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"写入 {out_dir / name}（来自第 {index} 条：{entries[index]['request']['url'][:80]}）")


def main() -> int:
    parser = argparse.ArgumentParser(description="Har 分析")
    parser.add_argument("har", type=Path)
    parser.add_argument("--endpoints", action="store_true")
    parser.add_argument("--dump", type=int, default=None)
    parser.add_argument("--export", type=Path, default=None)
    args = parser.parse_args()

    entries = load(args.har)
    print(f"共 {len(entries)} 条请求")
    if args.dump is not None:
        dump(entries, args.dump)
    elif args.export is not None:
        export(entries, args.export)
    elif args.endpoints:
        endpoints(entries)
    else:
        hosts(entries)
        print()
        endpoints(entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
