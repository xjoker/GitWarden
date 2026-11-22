from __future__ import annotations

import shlex
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from gitwarden import telemetry
from gitwarden.creds import check_url, git_env
from gitwarden.db import Store
from gitwarden.discovery import discover, preview
from gitwarden.git import _child_env, run
from gitwarden.runner import _maintain_archive, _progress_text, archive_path, run_task
from tests.conftest import commit, make_source

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_compose_hh_lon_production_constraints() -> None:
    compose_prod = (REPO_ROOT / "compose.hh-lon.yml").read_text(encoding="utf-8")
    compose_dev = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")

    # Bind mounts to /data HDD with dedicated scratch dir, no named volume.
    assert "/data/gitwarden:/data" in compose_prod
    assert "/data/gitwarden-work:/work" in compose_prod
    assert "gitwarden-data:/data" not in compose_prod
    assert "10001:10001" in compose_prod
    assert "0700" in compose_prod

    # Existing compose.yml stays intact for local dev.
    assert "gitwarden-data:/data" in compose_dev

    # Loopback-by-default dual-mode binding, resource caps, tmpfs size cap, and security hardening.
    assert "${GW_BIND_ADDR:-127.0.0.1}:${GW_PORT:-8080}:8080" in compose_prod
    assert "mem_limit: 1g" in compose_prod
    assert 'cpus: "1.0"' in compose_prod
    assert "pids_limit: 256" in compose_prod
    assert "/tmp:rw,noexec,nosuid,nodev,size=128m" in compose_prod
    assert 'GW_WORKERS: "1"' in compose_prod
    assert 'GW_WORK_DIR: "/work"' in compose_prod
    assert 'GW_COOKIE_SECURE: "${GW_COOKIE_SECURE:-auto}"' in compose_prod
    assert "read_only: true" in compose_prod
    assert "- ALL" in compose_prod
    assert "no-new-privileges=true" in compose_prod


def test_maintain_archive_passes_bounded_pack_flags(tmp_path: Path, monkeypatch) -> None:
    import gitwarden.runner as runner

    store = Store(tmp_path / "data")
    task_id = store.add_task("gc-flags", str(tmp_path / "src.git"), enabled=False)
    run_id = store.start_run(task_id)
    archive = tmp_path / "archive.git"
    run(None, "init", "--bare", str(archive))

    recorded: list[tuple[str, ...]] = []
    real_run = runner.run

    def spy_run(repo, *args, **kwargs):
        recorded.append(args)
        return real_run(repo, *args, **kwargs)

    monkeypatch.setattr(runner, "run", spy_run)
    _maintain_archive(store, run_id, archive)

    assert recorded == [
        (
            "-c",
            "pack.threads=1",
            "-c",
            "pack.windowMemory=128m",
            "-c",
            "pack.packSizeLimit=256m",
            "gc",
            "--no-prune",
        )
    ]
    assert (archive / "gitwarden-maintenance").exists()


def test_tail_buffer_caps_bytes_and_preserves_utf8_tail() -> None:
    buf = telemetry.TailBuffer(max_bytes=128 * 1024)
    # Append >512 KiB in small chunks including multi-byte UTF-8 characters.
    chunk = "进度-αβγ-0123456789abcdef\n"
    for _ in range(20000):
        buf.append(chunk)
    buf.append("FINAL_TAIL_MARKER\n")

    out = buf.text()
    encoded = out.encode("utf-8")
    assert buf.truncated is True
    assert len(encoded) <= 128 * 1024
    assert out.endswith("FINAL_TAIL_MARKER\n")
    assert "\ufffd" not in out


def test_step_record_bounds_output_and_avoids_duplicate_command_tail() -> None:
    record = telemetry.StepRecord("capture")
    big_chunk = "A" * 4096 + "\n"
    for _ in range(48):  # ~192 KiB > 128 KiB
        record.add_output(big_chunk)
    record.add_output("TAIL_ERROR_LINE\n")

    full_stderr = (big_chunk * 48) + "TAIL_ERROR_LINE\n"
    before = record.raw
    assert len(before.encode("utf-8")) <= telemetry.MAX_TAIL_BYTES
    assert before.endswith("TAIL_ERROR_LINE\n")

    # Simulating command_output(proc) passing full stderr should not duplicate or corrupt tail.
    record.add_command_output(full_stderr)
    assert record.raw == before


def test_progress_text_bounds_large_sequence_and_tail_buffer(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    parts = [f"Receiving objects: {i % 100}% ({i}/10000)\r" for i in range(10000)]
    text = _progress_text(store, parts, tmp_path, True)
    assert text is not None
    assert len(text.encode("utf-8")) <= 65536
    assert text.endswith("Receiving objects: 99% (9999/10000)\r")

    buf = telemetry.TailBuffer()
    for part in parts:
        buf.append(part)
    buf_text = _progress_text(store, buf, tmp_path, True)
    assert buf_text == text


def test_run_task_streams_large_progress_without_unbounded_memory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = Store(data_dir)
    src = make_source(tmp_path / "src")
    commit(src, "init")
    task_id = store.add_task("big-progress", str(src))

    upload_pack = tmp_path / "noisy-upload-pack"
    upload_pack.write_text(
        "#!/bin/sh\n"
        "i=0; while [ $i -lt 5000 ]; do "
        "printf 'progress chunk %05d xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\\n' \"$i\" >&2; "
        "i=$((i + 1)); done\n"
        "printf 'fatal: simulated tail failure\\n' >&2\n"
        "exit 9\n"
    )
    upload_pack.chmod(0o755)

    import gitwarden.runner as runner

    real_capture = runner.capture

    def noisy_capture(archive, source_url, name, *, env=None):
        run(None, "ls-remote", f"--upload-pack={upload_pack}", f"file://{src}", env=env)
        return real_capture(archive, source_url, name, env=env)

    # Invoke via run_task to exercise progress_buffer + telemetry StepRecord together.
    orig_capture = runner.capture
    runner.capture = noisy_capture
    try:
        captured = run_task(store, data_dir, task_id)
    finally:
        runner.capture = orig_capture

    assert captured is False
    run_row = store.list_runs(task_id)[0]
    assert run_row["status"] == "failed"
    assert "fatal: simulated tail failure" in (run_row["message"] or "")
    assert archive_path(data_dir, store.get_task(task_id)).exists()


def test_git_child_env_strips_gw_secrets_and_preserves_standard_vars(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GW_ADMIN_PASSWORD", "super-secret-admin-pw")
    monkeypatch.setenv("GW_SECRET", "leaked-env-secret")
    monkeypatch.setenv("GW_CUSTOM_TOKEN", "should-not-leak")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    # Without explicit GW_* in env, all GW_* from os.environ are stripped.
    sanitized = _child_env(None)
    assert "GW_ADMIN_PASSWORD" not in sanitized
    assert "GW_SECRET" not in sanitized
    assert "GW_CUSTOM_TOKEN" not in sanitized
    assert sanitized["HTTPS_PROXY"] == "http://proxy.internal:3128"
    assert sanitized["LANG"] == "en_US.UTF-8"
    assert "PATH" in sanitized
    assert sanitized["GIT_TERMINAL_PROMPT"] == "0"
    assert sanitized["LC_ALL"] == "C"

    # Even if a caller passed GW_ADMIN_PASSWORD explicitly, it is removed.
    assert "GW_ADMIN_PASSWORD" not in _child_env({"GW_ADMIN_PASSWORD": "x"})

    # Explicit GW_USERNAME / GW_SECRET from git_env still reach askpass subprocesses.
    with git_env({"kind": "https", "username": "bot", "secret": "tok-123"}, tmp_path / "w") as env:
        merged = _child_env(env)
        assert merged["GW_USERNAME"] == "bot"
        assert merged["GW_SECRET"] == "tok-123"
        assert "GW_ADMIN_PASSWORD" not in merged

    # End-to-end check with a real git subprocess (upload-pack inspecting env).
    dump = tmp_path / "env.txt"
    repo = tmp_path / "repo.git"
    run(None, "init", "--bare", str(repo))
    probe = tmp_path / "probe-upload-pack"
    probe.write_text(f"#!/bin/sh\nenv > {shlex.quote(str(dump))}\nexit 1\n")
    probe.chmod(0o755)
    run(None, "ls-remote", f"--upload-pack={probe}", f"file://{repo}", check=False)
    dumped = dump.read_text(encoding="utf-8")
    assert "super-secret-admin-pw" not in dumped
    assert "GW_ADMIN_PASSWORD=" not in dumped
    assert "GW_CUSTOM_TOKEN=" not in dumped
    assert "HTTPS_PROXY=http://proxy.internal:3128" in dumped


def test_ssh_git_env_uses_system_tempdir_and_cleans_up_on_error(tmp_path: Path) -> None:
    workdir = tmp_path / "data" / "work"
    workdir.mkdir(parents=True)
    cred = {
        "kind": "ssh",
        "private_key": "PRIVATE-KEY-DATA",
        "known_hosts": "git.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5",
    }

    temp_root = Path(tempfile.gettempdir()).resolve()
    key_dir: Path | None = None

    with pytest.raises(RuntimeError, match="boom"):
        with git_env(cred, workdir) as env:
            parts = shlex.split(env["GIT_SSH_COMMAND"])
            key_path = Path(parts[parts.index("-i") + 1])
            kh_path = Path(
                next(p for p in parts if p.startswith("UserKnownHostsFile=")).split("=", 1)[1]
            )
            key_dir = key_path.parent
            assert key_dir.resolve().is_relative_to(temp_root)
            assert not key_dir.resolve().is_relative_to(workdir.resolve())
            assert stat.S_IMODE(key_dir.stat().st_mode) == 0o700
            assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(kh_path.stat().st_mode) == 0o600
            raise RuntimeError("boom")

    assert key_dir is not None
    assert not key_dir.exists()


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "https://example.com/repo.git\nnewline",
        "https://example.com/repo.git\r",
        "https://example.com/repo\t.git",
        "https://example.com/repo\x00.git",
        "https://example.com/repo\x7f.git",
        "https://example.com/repo .git",
        "https://example.com/repo\u00a0.git",
        "/srv/repos/a.git\n",
        "/srv/repos/a b.git",
        "/srv/repos/a\x00.git",
        "-q@example.com:org/repo.git",
        "git@-evil.example.com:org/repo.git",
        "git@example.com:org/repo\n.git",
        "ssh://-q@example.com/org/repo.git",
        "ssh://git@-oProxyCommand=evil/org/repo.git",
    ],
)
def test_check_url_rejects_control_whitespace_and_leading_dash(bad_url: str) -> None:
    with pytest.raises(ValueError):
        check_url(bad_url)


@pytest.mark.parametrize(
    "bad_clone_url",
    [
        "/etc/passwd",
        "/data/gitwarden/archives/abc.git",
        "file:///tmp/repo.git",
        "git://forge.example.com/org/repo.git",
        "git@forge.example.com:org/repo.git",
        "https://evil.example.com/org/repo.git",
        "http://127.0.0.1:8080/org/repo.git",
        "http://169.254.169.254/latest/meta-data",
    ],
)
def test_discovery_rejects_local_or_cross_host_clone_urls(
    tmp_path: Path, monkeypatch, bad_clone_url: str
) -> None:
    store = Store(tmp_path / "data")
    cred = store.add_cred("bot", "https", username="bot", secret="tok")
    instance = store.add_instance("forge", "gitea", "https://forge.example.com:8443")
    access = store.add_access(instance, "bot", cred)

    repos = [
        {
            "id": 42,
            "full_name": "org/evil",
            "clone_url": bad_clone_url,
            "mirror": False,
        }
    ]
    monkeypatch.setattr(
        "gitwarden.discovery.client_from_store",
        lambda *_: SimpleNamespace(list_all_repos=lambda: repos),
    )

    with pytest.raises(ValueError):
        preview(store, access)
    with pytest.raises(ValueError):
        discover(store, access)
    assert store.list_tasks() == []


@pytest.mark.parametrize(
    "valid_clone_url",
    [
        "https://FORGE.EXAMPLE.COM/org/repo.git",
        "http://forge.example.com:8080/org/repo.git",
        "ssh://git@Forge.Example.com:2222/org/repo.git",
    ],
)
def test_discovery_accepts_same_host_http_https_ssh_case_insensitively(
    tmp_path: Path, monkeypatch, valid_clone_url: str
) -> None:
    store = Store(tmp_path / "data")
    cred = store.add_cred("bot", "https", username="bot", secret="tok")
    instance = store.add_instance("forge", "gitea", "https://forge.example.com:8443")
    access = store.add_access(instance, "bot", cred)

    repos = [
        {
            "id": 7,
            "full_name": "org/repo",
            "clone_url": valid_clone_url,
            "mirror": False,
        }
    ]
    monkeypatch.setattr(
        "gitwarden.discovery.client_from_store",
        lambda *_: SimpleNamespace(list_all_repos=lambda: repos),
    )

    result = discover(store, access)
    assert result.created == ["org/repo"]
    assert store.list_tasks()[0]["source_url"] == valid_clone_url
