from pathlib import Path

import pytest

from gitwarden.config import ConfigError, load_config


def _env(tmp_path: Path, config_path: Path, **extra: str) -> dict[str, str]:
    return {
        "GW_DATA_DIR": str(tmp_path / "data"),
        "GW_CONFIG": str(config_path),
        "GW_ADMIN_PASSWORD": "test-password",
        **extra,
    }


def test_scheduler_and_log_settings_load_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "gitwarden.toml"
    config_path.write_text(
        """
[server]
port = 9090
[scheduler]
tick_seconds = 0.25
workers = 3
min_sync_minutes = 6
min_discovery_minutes = 16
stall_minutes = 11
[logs]
retention_days = 30
"""
    )

    config = load_config(_env(tmp_path, config_path))

    assert config.port == 9090
    assert config.tick_seconds == 0.25
    assert config.workers == 3
    assert config.min_sync_minutes == 6
    assert config.min_discovery_minutes == 16
    assert config.stall_minutes == 11
    assert config.log_retention_days == 30


def test_environment_overrides_scheduler_and_log_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "gitwarden.toml"
    config_path.write_text(
        """
[scheduler]
workers = 3
min_sync_minutes = 6
min_discovery_minutes = 16
stall_minutes = 11
[logs]
retention_days = 30
"""
    )

    config = load_config(
        _env(
            tmp_path,
            config_path,
            GW_WORKERS="4",
            GW_MIN_SYNC_MINUTES="7",
            GW_MIN_DISCOVERY_MINUTES="17",
            GW_STALL_MINUTES="12",
            GW_LOG_RETENTION_DAYS="45",
        )
    )

    assert config.workers == 4
    assert config.min_sync_minutes == 7
    assert config.min_discovery_minutes == 17
    assert config.stall_minutes == 12
    assert config.log_retention_days == 45


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GW_WORKERS", "0"),
        ("GW_WORKERS", "5"),
        ("GW_MIN_SYNC_MINUTES", "0"),
        ("GW_MIN_DISCOVERY_MINUTES", "0"),
        ("GW_STALL_MINUTES", "0"),
        ("GW_LOG_RETENTION_DAYS", "0"),
    ],
)
def test_invalid_scheduler_or_log_settings_are_rejected(
    tmp_path: Path, key: str, value: str
) -> None:
    config_path = tmp_path / "gitwarden.toml"

    with pytest.raises(ConfigError):
        load_config(_env(tmp_path, config_path, **{key: value}))


@pytest.mark.parametrize("password", ["", "   ", "\t\r\n"])
def test_blank_admin_password_is_rejected(tmp_path: Path, password: str) -> None:
    config_path = tmp_path / "gitwarden.toml"
    with pytest.raises(ConfigError):
        load_config(_env(tmp_path, config_path, GW_ADMIN_PASSWORD=password))


def test_admin_password_preserves_surrounding_whitespace(tmp_path: Path) -> None:
    config_path = tmp_path / "gitwarden.toml"
    password = "  secret\t "
    config = load_config(_env(tmp_path, config_path, GW_ADMIN_PASSWORD=password))
    assert config.admin_password == password


def test_delete_guard_defaults_toml_and_env(tmp_path: Path) -> None:
    base = {"GW_DATA_DIR": str(tmp_path), "GW_ADMIN_PASSWORD": "pw"}
    config = load_config(base)
    assert (config.delete_guard, config.delete_guard_ratio, config.delete_guard_min_refs) == (
        True,
        0.5,
        3,
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gitwarden.toml").write_text(
        "[safety]\ndelete_guard_ratio = 0.8\ndelete_guard_min_refs = 5\n"
    )
    config = load_config(base)
    assert (config.delete_guard_ratio, config.delete_guard_min_refs) == (0.8, 5)
    config = load_config({**base, "GW_DELETE_GUARD": "off", "GW_DELETE_GUARD_RATIO": "0.3"})
    assert config.delete_guard is False and config.delete_guard_ratio == 0.3


@pytest.mark.parametrize(
    "env",
    [
        {"GW_DELETE_GUARD_RATIO": "0"},
        {"GW_DELETE_GUARD_RATIO": "1.5"},
        {"GW_DELETE_GUARD_MIN_REFS": "0"},
        {"GW_DELETE_GUARD": "maybe"},
    ],
)
def test_invalid_delete_guard_settings_are_rejected(tmp_path: Path, env) -> None:
    with pytest.raises(ConfigError):
        load_config({"GW_DATA_DIR": str(tmp_path), "GW_ADMIN_PASSWORD": "pw", **env})


# ---- delete_guard 开关：严格解析（环境变量和 TOML 同一套规则）----

_TRUTHY = ["true", "TRUE", "1", "on", "On", "yes", "YES", " on "]
_FALSY = ["false", "False", "0", "off", "OFF", "no", "No"]


def _guard_env(tmp_path: Path, toml: str | None = None, **extra: str) -> dict[str, str]:
    base = {"GW_DATA_DIR": str(tmp_path), "GW_ADMIN_PASSWORD": "pw", **extra}
    if toml is not None:
        path = tmp_path / "guard.toml"
        path.write_text(toml)
        base["GW_CONFIG"] = str(path)
    return base


@pytest.mark.parametrize("value", _TRUTHY)
def test_delete_guard_env_true_values(tmp_path: Path, value: str) -> None:
    toml = "[safety]\ndelete_guard = false\n"
    assert load_config(_guard_env(tmp_path, toml, GW_DELETE_GUARD=value)).delete_guard is True


@pytest.mark.parametrize("value", _FALSY)
def test_delete_guard_env_false_values(tmp_path: Path, value: str) -> None:
    assert load_config(_guard_env(tmp_path, GW_DELETE_GUARD=value)).delete_guard is False


@pytest.mark.parametrize("value", ["", " ", "maybe", "2", "-1", "enabled", "tru", "none"])
def test_delete_guard_env_invalid_values_are_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError, match="GW_DELETE_GUARD"):
        load_config(_guard_env(tmp_path, GW_DELETE_GUARD=value))


@pytest.mark.parametrize(
    ("literal", "expected"),
    [("true", True), ("false", False)]
    + [(f'"{v}"', True) for v in _TRUTHY]
    + [(f'"{v}"', False) for v in _FALSY]
    + [("1", True), ("0", False)],
)
def test_delete_guard_toml_values(tmp_path: Path, literal: str, expected: bool) -> None:
    config = load_config(_guard_env(tmp_path, f"[safety]\ndelete_guard = {literal}\n"))
    assert config.delete_guard is expected


@pytest.mark.parametrize("literal", ['""', '" "', '"maybe"', "2", "-1", "0.5", "[]", "{}"])
def test_delete_guard_toml_invalid_values_are_rejected(tmp_path: Path, literal: str) -> None:
    with pytest.raises(ConfigError, match="delete_guard"):
        load_config(_guard_env(tmp_path, f"[safety]\ndelete_guard = {literal}\n"))


def test_delete_guard_env_overrides_valid_toml(tmp_path: Path) -> None:
    toml = "[safety]\ndelete_guard = true\n"
    assert load_config(_guard_env(tmp_path, toml, GW_DELETE_GUARD="off")).delete_guard is False
    # 环境变量非法时不会退回 TOML，直接报错。
    with pytest.raises(ConfigError):
        load_config(_guard_env(tmp_path, toml, GW_DELETE_GUARD=""))


@pytest.mark.parametrize(
    "line",
    [
        'delete_guard_ratio = "0.5"',
        "delete_guard_ratio = true",
        'delete_guard_min_refs = "3"',
        "delete_guard_min_refs = 2.5",
        "delete_guard_min_refs = true",
    ],
)
def test_delete_guard_numbers_in_toml_must_be_numbers(tmp_path: Path, line: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_guard_env(tmp_path, f"[safety]\n{line}\n"))


def test_delete_guard_ratio_toml_int_is_accepted(tmp_path: Path) -> None:
    config = load_config(_guard_env(tmp_path, "[safety]\ndelete_guard_ratio = 1\n"))
    assert config.delete_guard_ratio == 1.0


def test_safety_must_be_a_table(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="safety"):
        load_config(_guard_env(tmp_path, 'safety = "x"\n'))
