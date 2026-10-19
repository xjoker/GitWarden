"""Listen address: loopback by default, explicit settings win, the image keeps 0.0.0.0."""

import shlex
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
    # The TOML example is also copied into Docker volumes; a pinned host would override
    # the image default there, so it must stay commented out.
    for name in ("data/config/gitwarden.toml.example", ".env.example"):
        for line in (ROOT / name).read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            key = stripped.partition("=")[0].strip()
            assert key not in {"host", "GW_HOST", "GW_DEFAULT_HOST"}, (name, line)
