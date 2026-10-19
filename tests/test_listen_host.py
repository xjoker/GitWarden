"""Listen address: loopback by default, explicit settings win, the image keeps 0.0.0.0."""

import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import gitwarden.__main__ as entry
from gitwarden.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


def _env(tmp_path: Path, toml: str | None = None, **extra: str) -> dict[str, str]:
    config_path = tmp_path / "gitwarden.toml"
    if toml is not None:
        config_path.write_text(toml)
    return {
        "GW_DATA_DIR": str(tmp_path / "data"),
        "GW_CONFIG": str(config_path),
        "GW_ADMIN_PASSWORD": "test-password",
        **extra,
    }


def test_default_listen_host_is_loopback(tmp_path: Path) -> None:
    assert load_config(_env(tmp_path)).host == "127.0.0.1"


def test_toml_server_host_is_used(tmp_path: Path) -> None:
    config = load_config(_env(tmp_path, '[server]\nhost = "0.0.0.0"\n'))
    assert config.host == "0.0.0.0"


def test_gw_host_overrides_toml(tmp_path: Path) -> None:
    config = load_config(_env(tmp_path, '[server]\nhost = "127.0.0.1"\n', GW_HOST="0.0.0.0"))
    assert config.host == "0.0.0.0"


@pytest.mark.parametrize("toml_host", ["127.0.0.1", "::", "0.0.0.0", "192.0.2.10"])
def test_toml_host_beats_image_default(tmp_path: Path, toml_host: str) -> None:
    config = load_config(
        _env(tmp_path, f'[server]\nhost = "{toml_host}"\n', GW_DEFAULT_HOST="0.0.0.0")
    )
    assert config.host == toml_host


def test_image_default_applies_without_explicit_host(tmp_path: Path) -> None:
    assert load_config(_env(tmp_path, GW_DEFAULT_HOST="0.0.0.0")).host == "0.0.0.0"


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_environment_values_fall_through(tmp_path: Path, blank: str) -> None:
    assert load_config(_env(tmp_path, GW_HOST=blank)).host == "127.0.0.1"
    assert load_config(_env(tmp_path, GW_HOST=blank, GW_DEFAULT_HOST="0.0.0.0")).host == "0.0.0.0"
    assert load_config(_env(tmp_path, GW_DEFAULT_HOST=blank)).host == "127.0.0.1"
    toml = '[server]\nhost = "::"\n'
    assert load_config(_env(tmp_path, toml, GW_HOST=blank)).host == "::"


@pytest.mark.parametrize("literal", ['""', '"  "', "0", "true", "[]"])
def test_invalid_toml_host_is_rejected(tmp_path: Path, literal: str) -> None:
    with pytest.raises(ConfigError, match="host"):
        load_config(_env(tmp_path, f"[server]\nhost = {literal}\n"))


BAD_HOSTS = [
    "not a host!",
    "[::]",
    "0.0.0.0 127.0.0.1",
    "*",
    "0",
    "127.1",
    "0x7f000001",
    "-x",
    "::ffff:127.0.0.1",
    "::1%x y",
    "127.0.0.1.",
    "08.0.0.1",
    "4294967296",
    "my_host",
]
GOOD_HOSTS = [
    "127.0.0.1",
    "0.0.0.0",
    "::",
    "::1",
    "192.0.2.10",
    "localhost",
    "gw.example.lan",
    "fe80::1%eth0",
    "::%1",
]


@pytest.mark.parametrize("value", BAD_HOSTS)
def test_invalid_gw_host_is_rejected_naming_the_environment_variable(
    tmp_path: Path, value: str
) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(_env(tmp_path, GW_HOST=value))
    message = str(caught.value)
    assert "环境变量 GW_HOST" in message and repr(value) in message
    assert "[server] host" not in message


@pytest.mark.parametrize("value", BAD_HOSTS)
def test_invalid_toml_host_is_rejected_naming_the_file(tmp_path: Path, value: str) -> None:
    env = _env(tmp_path, f"[server]\nhost = {value!r}\n".replace("'", '"'))
    with pytest.raises(ConfigError) as caught:
        load_config(env)
    message = str(caught.value)
    assert env["GW_CONFIG"] in message and "[server] host" in message
    assert repr(value) in message and "GW_HOST" not in message


def test_invalid_image_default_is_rejected_naming_gw_default_host(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="环境变量 GW_DEFAULT_HOST"):
        load_config(_env(tmp_path, GW_DEFAULT_HOST="*"))


def test_invalid_toml_host_is_rejected_even_when_gw_host_is_set(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[server\] host"):
        load_config(_env(tmp_path, '[server]\nhost = "*"\n', GW_HOST="127.0.0.1"))


@pytest.mark.parametrize("value", GOOD_HOSTS)
def test_valid_hosts_are_accepted_from_every_source(tmp_path: Path, value: str) -> None:
    assert load_config(_env(tmp_path, GW_HOST=value)).host == value
    assert load_config(_env(tmp_path, f'[server]\nhost = "{value}"\n')).host == value
    assert load_config(_env(tmp_path, GW_DEFAULT_HOST=value)).host == value


def test_gw_host_overriding_toml_logs_one_warning(tmp_path: Path, caplog) -> None:
    env = _env(tmp_path, '[server]\nhost = "127.0.0.1"\n', GW_HOST="0.0.0.0")
    with caplog.at_level(logging.WARNING, logger="gitwarden"):
        config = load_config(env)
    assert config.host == "0.0.0.0"  # the warning does not change the priority
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "GW_HOST=0.0.0.0" in text and "127.0.0.1" in text and env["GW_CONFIG"] in text


@pytest.mark.parametrize(
    ("toml", "gw_host"),
    [
        ('[server]\nhost = "127.0.0.1"\n', "127.0.0.1"),  # same value
        ('[server]\nhost = "127.0.0.1"\n', " 127.0.0.1 "),  # same after strip
        ('[server]\nhost = "127.0.0.1"\n', ""),  # blank GW_HOST counts as unset
        ("[server]\nport = 8080\n", "0.0.0.0"),  # no TOML host
    ],
)
def test_no_override_warning_without_a_conflict(
    tmp_path: Path, caplog, toml: str, gw_host: str
) -> None:
    with caplog.at_level(logging.WARNING, logger="gitwarden"):
        load_config(_env(tmp_path, toml, GW_HOST=gw_host))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_image_default_does_not_trigger_the_override_warning(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="gitwarden"):
        config = load_config(
            _env(tmp_path, '[server]\nhost = "127.0.0.1"\n', GW_DEFAULT_HOST="0.0.0.0")
        )
    assert config.host == "127.0.0.1"
    assert not caplog.records


def test_process_exits_before_serving_on_invalid_gw_host(tmp_path: Path) -> None:
    # Real process, nothing mocked: startup must stop in config loading with the source named.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GW_")}
    env |= _env(tmp_path, GW_HOST="*", GW_PORT="1")
    result = subprocess.run(
        [sys.executable, "-m", "gitwarden"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "启动失败" in result.stderr and "环境变量 GW_HOST" in result.stderr
    assert "Uvicorn running" not in result.stderr
    assert not (tmp_path / "data" / "gitwarden.db").exists()


def test_entrypoint_passes_configured_host_to_uvicorn(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(entry, "create_app", lambda config: "app")
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: calls.append(kw))
    for key in ("GW_HOST", "GW_DEFAULT_HOST", "GW_PORT", "GW_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GW_ADMIN_PASSWORD", "test-password")

    entry.main()
    monkeypatch.setenv("GW_HOST", "0.0.0.0")
    entry.main()

    assert [c["host"] for c in calls] == ["127.0.0.1", "0.0.0.0"]


def _final_stage_env(dockerfile: str) -> dict[str, str]:
    """Collect ENV settings of the last build stage (continuations, comments, legacy form)."""
    logical: list[str] = []
    pending = ""
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if pending and line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        logical.append(pending + line)
        pending = ""
    if pending:
        logical.append(pending)
    env: dict[str, str] = {}
    for line in logical:
        word, _, rest = line.partition(" ")
        if word.upper() == "FROM":
            env = {}
        elif word.upper() == "ENV":
            tokens = shlex.split(rest)
            if all("=" in token for token in tokens):
                env.update(token.split("=", 1) for token in tokens)
            else:
                env[tokens[0]] = " ".join(tokens[1:])
    return env


def test_dockerfile_env_parser_handles_common_forms() -> None:
    sample = (
        "FROM a AS builder\nENV GW_DEFAULT_HOST=0.0.0.0\n"
        "FROM b\n# ENV GW_HOST=0.0.0.0\n"
        'ENV TZ=UTC \\\n    GW_DEFAULT_HOST="0.0.0.0" \\\n    X=1\nENV LEGACY some value\n'
    )
    assert _final_stage_env(sample) == {
        "TZ": "UTC",
        "GW_DEFAULT_HOST": "0.0.0.0",
        "X": "1",
        "LEGACY": "some value",
    }


def test_image_listens_on_all_interfaces_without_overriding_explicit_settings() -> None:
    env = _final_stage_env((ROOT / "Dockerfile").read_text())
    # Container ports are only reachable through Docker's port mapping on 0.0.0.0.
    assert env.get("GW_DEFAULT_HOST") == "0.0.0.0"
    # GW_HOST would outrank [server] host in a mounted gitwarden.toml.
    assert "GW_HOST" not in env


def test_compose_keeps_image_default_and_loopback_host_mapping() -> None:
    compose = (ROOT / "compose.yml").read_text()
    assert "GW_HOST" not in compose and "GW_DEFAULT_HOST" not in compose
    assert '"${GW_BIND:-127.0.0.1}:${GW_PORT:-8080}:${GW_PORT:-8080}"' in compose


def test_shipped_examples_do_not_pin_a_listen_host() -> None:
    # Users may copy the TOML example into a Docker volume; a pinned host would override
    # the image default there, so it must stay commented out.
    for name in ("data/config/gitwarden.toml.example", ".env.example"):
        for line in (ROOT / name).read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            key = stripped.partition("=")[0].strip()
            assert key not in {"host", "GW_HOST", "GW_DEFAULT_HOST"}, (name, line)


def test_healthcheck_url_rewrites_all_interfaces(tmp_path: Path) -> None:
    from gitwarden.config import healthcheck_url

    assert healthcheck_url(_env(tmp_path, GW_DEFAULT_HOST="0.0.0.0", GW_PORT="9090")).endswith(
        "://127.0.0.1:9090/health"
    )
    assert (
        healthcheck_url(_env(tmp_path, GW_HOST="::", GW_PORT="9091")) == "http://[::1]:9091/health"
    )
    assert (
        healthcheck_url(_env(tmp_path, GW_HOST="172.30.0.2", GW_PORT="9092"))
        == "http://172.30.0.2:9092/health"
    )
    assert (
        healthcheck_url(_env(tmp_path, GW_HOST="::1", GW_PORT="9093")) == "http://[::1]:9093/health"
    )
    assert (
        healthcheck_url(_env(tmp_path, GW_HOST="2001:db8::1", GW_PORT="9094"))
        == "http://[2001:db8::1]:9094/health"
    )
    assert (
        healthcheck_url(_env(tmp_path, GW_HOST="fe80::1%eth0", GW_PORT="9095"))
        == "http://[fe80::1%25eth0]:9095/health"
    )


def test_entrypoint_warns_when_listening_on_all_interfaces(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(entry, "create_app", lambda config: "app")
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: None)
    for key in ("GW_HOST", "GW_DEFAULT_HOST", "GW_PORT", "GW_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GW_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("GW_HOST", "0.0.0.0")
    with caplog.at_level(logging.WARNING, logger="gitwarden"):
        entry.main()
    assert any("0.0.0.0" in r.getMessage() and "警告" in r.getMessage() for r in caplog.records)
