"""配置与账号清单加载。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util import setup_logger

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

ENGINES = ("http", "browser")
SPEEDS = ("realtime", "tick", "probe", "normal", "human")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")


class ConfigError(RuntimeError):
    """配置不合法。"""


@dataclass
class RuntimeConfig:
    base_url: str = "https://courses.guet.edu.cn"
    engine: str = "http"
    headless: bool = True
    auto_login: bool = False
    login_timeout: int = 300
    speed: str = "normal"
    chunk_seconds: int = 120
    gap_min: float = 1.5
    gap_max: float = 3.0
    gap_explicit: bool = False
    target_percent: int = 100
    telemetry: bool = True
    sub_course_id: int = 0
    realtime_lanes: Any = 8          # 数字 / "auto"（按实测推荐）/ 0 或 "all"（有多少开多少）
    realtime_max_lanes: int = 16
    realtime_eta_seconds: int = 0    # 期望多久刷完（秒），0 = 默认 1 小时目标
    tick_interval: int = 0           # tick 档的轮次间隔，0 = 等于段长（默认 120s）
    tick_once: bool = False          # tick 档跑一轮就退出（配合计划任务）
    tick_threads: int = 1            # tick 档同一轮内并发上报数（>1 有顶号风险，默认串行）
    account_jobs: int = 4
    per_account_jobs: int = 1
    rps: float = 3.0
    global_rps: float = 8.0
    max_browser_contexts: int = 2
    timeout: float = 30.0
    retries: int = 3
    proxy: str = ""
    user_agent: str = DEFAULT_UA
    profile_dir: str = "profiles"
    state_dir: str = "data"
    log_dir: str = "data/logs"
    course_cache_ttl: int = 1800

    @property
    def gap(self) -> tuple[float, float]:
        return (self.gap_min, self.gap_max)


@dataclass
class AccountConfig:
    name: str
    username: str = ""
    password: str = ""
    enabled: bool = True
    note: str = ""
    course_ids: list[int] = field(default_factory=list)
    semester_id: str = ""
    academic_year_id: str = ""
    speed: str | None = None
    target_percent: int | None = None
    engine: str | None = None
    per_account_jobs: int | None = None
    realtime_lanes: Any = None

    def effective_speed(self, rt: RuntimeConfig) -> str:
        return (self.speed or rt.speed).lower()

    def effective_target(self, rt: RuntimeConfig) -> int:
        return int(self.target_percent if self.target_percent is not None else rt.target_percent)

    def effective_engine(self, rt: RuntimeConfig) -> str:
        return (self.engine or rt.engine).lower()


@dataclass
class AppConfig:
    root: Path
    runtime: RuntimeConfig
    accounts: list[AccountConfig]
    config_path: Path | None = None
    accounts_path: Path | None = None

    # ---- 路径 ----
    def _resolve(self, raw: str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else (self.root / path)

    def profile_path(self, account: AccountConfig) -> Path:
        return self._resolve(self.runtime.profile_dir) / account.name

    def state_path(self, account: AccountConfig) -> Path:
        return self._resolve(self.runtime.state_dir) / f"state.{account.name}.json"

    def log_path(self, account: AccountConfig) -> Path:
        return self._resolve(self.runtime.log_dir) / f"{account.name}.log"

    def course_cache_path(self) -> Path:
        return self._resolve(self.runtime.state_dir) / "course-cache.json"

    def result_path(self) -> Path:
        return self._resolve(self.runtime.state_dir) / "result.json"

    def logger(self, name: str, account: AccountConfig | None = None):
        return setup_logger(name, self.log_path(account) if account else None)

    # ---- 账号 ----
    def enabled_accounts(self) -> list[AccountConfig]:
        return [a for a in self.accounts if a.enabled]

    def account(self, name: str) -> AccountConfig:
        for acc in self.accounts:
            if acc.name == name:
                return acc
        raise ConfigError(f"accounts.yaml 里没有名为 {name} 的账号")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _to_int_list(value: Any) -> list[int]:
    out: list[int] = []
    for item in _as_list(value):
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _apply_runtime(raw: dict[str, Any]) -> RuntimeConfig:
    rt = RuntimeConfig()
    if not isinstance(raw, dict):
        return rt
    for key, value in raw.items():
        if value is None:
            continue
        if key == "gap_seconds":
            seq = _as_list(value)
            if seq:
                rt.gap_min = float(seq[0])
                rt.gap_max = float(seq[-1])
                rt.gap_explicit = True
            continue
        if not hasattr(rt, key):
            continue
        setattr(rt, key, value)
    rt.engine = str(rt.engine).lower()
    rt.speed = str(rt.speed).lower()
    rt.proxy = (rt.proxy or "").strip()
    return rt


def _apply_account(raw: dict[str, Any], defaults: dict[str, Any], index: int) -> AccountConfig:
    merged: dict[str, Any] = {**defaults, **{k: v for k, v in raw.items() if v is not None}}
    name = str(merged.get("name") or f"acc{index:02d}").strip()
    acc = AccountConfig(
        name=name,
        username=str(merged.get("username") or "").strip(),
        password=str(merged.get("password") or ""),
        enabled=bool(merged.get("enabled", True)),
        note=str(merged.get("note") or ""),
        course_ids=_to_int_list(merged.get("course_ids")),
        semester_id=str(merged.get("semester_id") or ""),
        academic_year_id=str(merged.get("academic_year_id") or ""),
        speed=(str(merged["speed"]).lower() if merged.get("speed") else None),
        target_percent=(int(merged["target_percent"]) if merged.get("target_percent") is not None else None),
        engine=(str(merged["engine"]).lower() if merged.get("engine") else None),
        per_account_jobs=(
            int(merged["per_account_jobs"]) if merged.get("per_account_jobs") is not None else None
        ),
        realtime_lanes=_parse_lanes(merged.get("realtime_lanes")),
    )
    return acc


def _parse_lanes(value: Any) -> Any:
    """`8` / `"8"` / `"auto"` / `"all"` / 0 都允许。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if text in {"auto", "all", "max"}:
        return text
    try:
        return int(text)
    except ValueError:
        return text


def _read_yaml(path: Path, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"找不到 {path}（可从 {path.stem}.example.yaml 复制）")
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - 依赖外部输入
        raise ConfigError(f"{path.name} 不是合法 YAML：{exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path.name} 顶层必须是映射")
    return loaded


def load_config(
    root: Path,
    config_path: Path | None = None,
    accounts_path: Path | None = None,
) -> AppConfig:
    root = root.resolve()
    cfg_path = (config_path or (root / "config.yaml")).resolve()
    acc_path = (accounts_path or (root / "accounts.yaml")).resolve()

    rt = _apply_runtime(_read_yaml(cfg_path, required=False))
    acc_raw = _read_yaml(acc_path, required=True)
    defaults = acc_raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError("accounts.yaml 的 defaults 必须是映射")
    raw_accounts = acc_raw.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ConfigError("accounts.yaml 需要至少一个 accounts 条目")

    accounts: list[AccountConfig] = []
    for idx, item in enumerate(raw_accounts, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"accounts.yaml 第 {idx} 个账号不是映射")
        accounts.append(_apply_account(item, defaults, idx))

    app = AppConfig(root=root, runtime=rt, accounts=accounts,
                    config_path=cfg_path if cfg_path.exists() else None,
                    accounts_path=acc_path)
    validate(app)
    return app


def validate(app: AppConfig) -> None:
    rt = app.runtime
    if rt.engine not in ENGINES:
        raise ConfigError(f"engine 只能是 {ENGINES}，当前 {rt.engine}")
    if rt.speed not in SPEEDS:
        raise ConfigError(f"speed 只能是 {SPEEDS}，当前 {rt.speed}")
    if not 1 <= int(rt.target_percent) <= 100:
        raise ConfigError("target_percent 必须在 1~100")
    if rt.chunk_seconds < 0:
        raise ConfigError("chunk_seconds 不能为负")
    if rt.gap_min > rt.gap_max:
        raise ConfigError("gap_seconds 下限不能大于上限")
    if int(rt.realtime_max_lanes) < 1:
        raise ConfigError("realtime_max_lanes 至少为 1")
    _check_lanes(rt.realtime_lanes, "realtime_lanes")
    if not rt.base_url.startswith("http"):
        raise ConfigError("base_url 必须是 http(s) 地址")

    seen: set[str] = set()
    for acc in app.accounts:
        if not _NAME_RE.match(acc.name):
            raise ConfigError(
                f"账号名 {acc.name!r} 只能包含字母数字与 _ . - 且不超过 40 字符（会作为目录名）"
            )
        if acc.name in seen:
            raise ConfigError(f"账号名重复：{acc.name}")
        seen.add(acc.name)
        if acc.speed and acc.speed not in SPEEDS:
            raise ConfigError(f"账号 {acc.name} 的 speed 不合法：{acc.speed}")
        if acc.engine and acc.engine not in ENGINES:
            raise ConfigError(f"账号 {acc.name} 的 engine 不合法：{acc.engine}")
        if acc.target_percent is not None and not 1 <= acc.target_percent <= 100:
            raise ConfigError(f"账号 {acc.name} 的 target_percent 必须在 1~100")
        _check_lanes(acc.realtime_lanes, f"账号 {acc.name} 的 realtime_lanes")

    if not app.enabled_accounts():
        raise ConfigError("accounts.yaml 里没有 enabled: true 的账号")


def _check_lanes(value: Any, label: str) -> None:
    if value is None:
        return
    if isinstance(value, int):
        if value < 0 or value > 256:
            raise ConfigError(f"{label} 必须是 0~256 的整数，或 auto/all")
        return
    text = str(value).strip().lower()
    if text in {"auto", "all", "max"}:
        return
    raise ConfigError(f"{label} 只接受数字、auto 或 all，收到 {value!r}")
