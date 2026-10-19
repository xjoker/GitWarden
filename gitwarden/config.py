"""配置加载：环境变量 > TOML 文件 > 默认值。"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """配置错误，消息直接显示给管理员。"""


@dataclass(frozen=True)
class Config:
    data_dir: Path
    port: int = 8080
    tick_seconds: float = 30
    workers: int = 2
    min_sync_minutes: int = 5
    min_discovery_minutes: int = 15
    stall_minutes: float = 10
    log_retention_days: int = 90
    admin_password: str = ""
    delete_guard: bool = True
    delete_guard_ratio: float = 0.5
    delete_guard_min_refs: int = 3


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件格式错误 {path}: {exc}") from exc


def _number(env: Mapping[str, str], key: str, fallback, cast):
    raw = env.get(key)
    if raw is None or raw == "":
        return fallback
    try:
        return cast(raw)
    except ValueError:
        raise ConfigError(f"环境变量 {key} 的值无效: {raw!r}") from None


_TRUE = frozenset({"true", "1", "on", "yes"})
_FALSE = frozenset({"false", "0", "off", "no"})
_FLAG_HINT = "true/false、1/0、on/off、yes/no（不区分大小写）"


def _parse_flag(value: object, where: str) -> bool:
    """严格解析开关：只接受明确的真值/假值，空串和其他值一律报错，不做静默回退。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
    raise ConfigError(f"{where} 的值无效: {value!r}，只接受 {_FLAG_HINT}")


def _flag(env: Mapping[str, str], key: str, toml_key: str, fallback: object) -> bool:
    raw = env.get(key)
    if raw is not None:
        # 与数值类设置不同：显式设置为空串也报错，避免 GW_DELETE_GUARD= 被误当成关闭或默认。
        return _parse_flag(raw, f"环境变量 {key}")
    return _parse_flag(fallback, f"配置项 {toml_key}")


def _toml_number(value: object, where: str, cast):
    """TOML 里的数值必须是原生数值；字符串、布尔值、整数项写成小数都报 ConfigError。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} 必须是数值: {value!r}")
    if cast is int and not isinstance(value, int):
        raise ConfigError(f"{where} 必须是整数: {value!r}")
    return cast(value)


def _validate(config: Config) -> Config:
    if not 1 <= config.workers <= 4:
        raise ConfigError("scheduler workers 必须在 1 到 4 之间")
    if config.min_sync_minutes < 1:
        raise ConfigError("同步间隔下限必须至少为 1 分钟")
    if config.min_discovery_minutes < 1:
        raise ConfigError("发现间隔下限必须至少为 1 分钟")
    if config.stall_minutes <= 0:
        raise ConfigError("卡死检测间隔必须大于 0 分钟")
    if config.log_retention_days < 1:
        raise ConfigError("日志保留天数必须至少为 1 天")
    if config.tick_seconds <= 0:
        raise ConfigError("调度 tick 必须大于 0 秒")
    if not 0 < config.delete_guard_ratio <= 1:
        raise ConfigError("大批删除保护比例必须大于 0 且不超过 1")
    if config.delete_guard_min_refs < 1:
        raise ConfigError("大批删除保护的最少 ref 数必须至少为 1")
    return config


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    data_dir = Path(env.get("GW_DATA_DIR") or "./data")
    config_path = Path(env.get("GW_CONFIG") or data_dir / "config" / "gitwarden.toml")
    toml = _read_toml(config_path)
    server = toml.get("server", {})
    scheduler = toml.get("scheduler", {})
    logs = toml.get("logs", {})
    safety = toml.get("safety", {})
    if not isinstance(safety, dict):
        raise ConfigError("配置文件里的 [safety] 必须是一个表")

    port = _number(env, "GW_PORT", server.get("port", 8080), int)
    tick = _number(env, "GW_TICK_SECONDS", scheduler.get("tick_seconds", 30), float)
    workers = _number(env, "GW_WORKERS", scheduler.get("workers", 2), int)
    min_sync = _number(
        env,
        "GW_MIN_SYNC_MINUTES",
        scheduler.get("min_sync_minutes", 5),
        int,
    )
    min_discovery = _number(
        env,
        "GW_MIN_DISCOVERY_MINUTES",
        scheduler.get("min_discovery_minutes", 15),
        int,
    )
    stall = _number(env, "GW_STALL_MINUTES", scheduler.get("stall_minutes", 10), float)
    retention = _number(
        env,
        "GW_LOG_RETENTION_DAYS",
        logs.get("retention_days", 90),
        int,
    )
    guard = _flag(env, "GW_DELETE_GUARD", "[safety] delete_guard", safety.get("delete_guard", True))
    guard_ratio = _number(
        env,
        "GW_DELETE_GUARD_RATIO",
        _toml_number(
            safety.get("delete_guard_ratio", 0.5), "配置项 [safety] delete_guard_ratio", float
        ),
        float,
    )
    guard_min = _number(
        env,
        "GW_DELETE_GUARD_MIN_REFS",
        _toml_number(
            safety.get("delete_guard_min_refs", 3), "配置项 [safety] delete_guard_min_refs", int
        ),
        int,
    )
    password = env.get("GW_ADMIN_PASSWORD", "")
    if not password.strip():
        raise ConfigError("未设置管理员密码：请在环境变量 GW_ADMIN_PASSWORD 中设置后再启动")
    return _validate(
        Config(
            data_dir=data_dir,
            port=port,
            tick_seconds=tick,
            workers=workers,
            min_sync_minutes=min_sync,
            min_discovery_minutes=min_discovery,
            stall_minutes=stall,
            log_retention_days=retention,
            admin_password=password,
            delete_guard=guard,
            delete_guard_ratio=guard_ratio,
            delete_guard_min_refs=guard_min,
        )
    )
