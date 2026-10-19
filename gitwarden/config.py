"""配置加载：环境变量 > TOML 文件 > 默认值。"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """配置错误，消息直接显示给管理员。"""


@dataclass(frozen=True)
class Config:
    data_dir: Path
    host: str = "127.0.0.1"
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


_HOSTNAME_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")
_HOST_HINT = "应填 IP 地址（如 127.0.0.1、0.0.0.0、::，IPv6 不加方括号）或主机名"


def _check_host(value: str, where: str) -> str:
    """启动前校验监听地址，报错时写明值来自哪里（环境变量还是 TOML）。

    接受 IPv4/IPv6 字面量（含合法 zone id）和 RFC 1123 主机名；拒绝空白与控制
    字符、方括号、通配符 `*`、IPv4 映射的 IPv6（`::ffff:…`）、末级标签为纯数字的
    “主机名”，以及 `0`、`127.1` 这类会被系统解析成 IP 的数字简写。
    """
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ConfigError(f"监听地址 {where} 无效: {value!r}，{_HOST_HINT}")
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.version == 6 and ip.ipv4_mapped is not None:
            raise ConfigError(f"监听地址 {where} 无效: {value!r}，{_HOST_HINT}")
        if "%" in value:
            zone = value.rsplit("%", 1)[1]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", zone):
                raise ConfigError(f"监听地址 {where} 无效: {value!r}，{_HOST_HINT}")
        return value
    labels = value[:-1].split(".") if value.endswith(".") else value.split(".")
    # RFC 1123: the rightmost label of a hostname must not be all-numeric.
    if not labels or labels[-1].isdigit():
        raise ConfigError(f"监听地址 {where} 无效: {value!r}，{_HOST_HINT}")
    is_hostname = len(value) <= 253 and all(_HOSTNAME_LABEL.fullmatch(x) for x in labels)
    if is_hostname:
        try:
            socket.inet_aton(value)
        except OSError:
            return value
    raise ConfigError(f"监听地址 {where} 无效: {value!r}，{_HOST_HINT}")


def healthcheck_url(env: Mapping[str, str] | None = None) -> str:
    """Docker HEALTHCHECK 应探测的 URL：用实际生效的 host/port。

    `0.0.0.0` 改连 `127.0.0.1`，`::` 改连 `::1`（IPv6 回环；URL 写成 `[::1]`），
    否则按绑定地址连（含网卡 IP），避免 host 网络下绑网卡 IP 时一直 unhealthy。
    """
    config = load_config(env)
    host = config.host
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if "%" in host:
        addr, zone = host.rsplit("%", 1)
        host_part = f"[{addr}%25{zone}]"
    elif ":" in host:
        host_part = f"[{host}]"
    else:
        host_part = host
    return f"http://{host_part}:{config.port}/health"


def probe_health(env: Mapping[str, str] | None = None, timeout: float = 3) -> None:
    """供镜像 HEALTHCHECK 调用：连不上或非 2xx 时抛异常。"""
    import urllib.request

    urllib.request.urlopen(healthcheck_url(env), timeout=timeout).read()


def _host(env: Mapping[str, str], server: Mapping, config_path: Path) -> str:
    """监听地址：GW_HOST > [server] host > GW_DEFAULT_HOST（镜像设为 0.0.0.0）> 127.0.0.1。"""
    toml_where = f"配置文件 {config_path} 中的 [server] host"
    toml_host = None
    if "host" in server:
        value = server["host"]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{toml_where} 必须是非空字符串: {value!r}")
        toml_host = _check_host(value.strip(), toml_where)
    raw = (env.get("GW_HOST") or "").strip()
    if raw:
        host = _check_host(raw, "环境变量 GW_HOST")
        if toml_host is not None and toml_host != host:
            logging.getLogger("gitwarden").warning(
                "警告: 环境变量 GW_HOST=%s 覆盖了%s=%s", host, toml_where, toml_host
            )
        return host
    if toml_host is not None:
        return toml_host
    fallback = (env.get("GW_DEFAULT_HOST") or "").strip()
    if fallback:
        return _check_host(fallback, "环境变量 GW_DEFAULT_HOST")
    return "127.0.0.1"


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
    host = _host(env, server, config_path)
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
            host=host,
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
