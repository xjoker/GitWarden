from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gitwarden.db import Store
from gitwarden.git import (
    _ACTIVE_PROCESSES,
    _ACTIVE_PROCESSES_LOCK,
    _DEFAULT_CONTEXT,
    GitError,
    _run_streamed,
    execution_context,
    run,
    terminate_active_processes,
)
from gitwarden.scheduler import Scheduler


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def test_active_process_registry_tracks_spawn_and_cleans_up_on_exit(tmp_path: Path) -> None:
    ready_file = tmp_path / "ready"
    release_file = tmp_path / "release"
    script = (
        "import pathlib, time\n"
        f"pathlib.Path({str(ready_file)!r}).write_text('ok')\n"
        f"while not pathlib.Path({str(release_file)!r}).exists():\n"
        "    time.sleep(0.01)\n"
    )
    results: list[subprocess.CompletedProcess[str]] = []

    def target() -> None:
        results.append(
            _run_streamed(
                [sys.executable, "-c", script],
                input=None,
                env=dict(os.environ),
                command=None,
                context=_DEFAULT_CONTEXT,
            )
        )

    thread = threading.Thread(target=target)
    thread.start()
    try:
        _wait_until(ready_file.exists)
        with _ACTIVE_PROCESSES_LOCK:
            assert len(_ACTIVE_PROCESSES) == 1
    finally:
        release_file.write_text("go")
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert results and results[0].returncode == 0
    with _ACTIVE_PROCESSES_LOCK:
        assert len(_ACTIVE_PROCESSES) == 0


def test_terminate_active_processes_returns_zero_immediately_when_empty() -> None:
    with _ACTIVE_PROCESSES_LOCK:
        assert len(_ACTIVE_PROCESSES) == 0

    started = time.monotonic()
    count = terminate_active_processes(grace_seconds=5.0)
    elapsed = time.monotonic() - started

    assert count == 0
    assert elapsed < 0.2


def test_terminate_active_processes_sends_sigterm_without_sigkill_when_exiting_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready_file = tmp_path / "ready"
    script = (
        "import pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
        f"pathlib.Path({str(ready_file)!r}).write_text('ok'); "
        "time.sleep(30)"
    )
    signals_sent: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def spy_killpg(pgid: int, sig: int) -> None:
        signals_sent.append((pgid, sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr("gitwarden.git.os.killpg", spy_killpg)

    results: list[subprocess.CompletedProcess[str]] = []
    thread = threading.Thread(
        target=lambda: results.append(
            _run_streamed(
                [sys.executable, "-c", script],
                input=None,
                env=dict(os.environ),
                command=None,
                context=_DEFAULT_CONTEXT,
            )
        )
    )
    thread.start()
    try:
        _wait_until(ready_file.exists)
        with _ACTIVE_PROCESSES_LOCK:
            pids = {proc.pid for proc in _ACTIVE_PROCESSES}
        assert len(pids) == 1

        started = time.monotonic()
        terminated = terminate_active_processes(grace_seconds=3.0)
        elapsed = time.monotonic() - started

        assert terminated == 1
        assert elapsed < 1.0
        assert signals_sent == [(next(iter(pids)), signal.SIGTERM)]
    finally:
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert results and results[0].returncode == 0
    with _ACTIVE_PROCESSES_LOCK:
        assert len(_ACTIVE_PROCESSES) == 0


def test_terminate_active_processes_escalates_to_sigkill_after_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready_file = tmp_path / "ready"
    script = (
        "import pathlib, signal, subprocess, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"pathlib.Path({str(ready_file)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )
    signals_sent: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def spy_killpg(pgid: int, sig: int) -> None:
        signals_sent.append((pgid, sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr("gitwarden.git.os.killpg", spy_killpg)

    results: list[subprocess.CompletedProcess[str]] = []
    thread = threading.Thread(
        target=lambda: results.append(
            _run_streamed(
                [sys.executable, "-c", script],
                input=None,
                env=dict(os.environ),
                command=None,
                context=_DEFAULT_CONTEXT,
            )
        )
    )
    thread.start()
    child_pid = None
    try:
        _wait_until(lambda: ready_file.exists() and ready_file.read_text().strip().isdigit())
        child_pid = int(ready_file.read_text().strip())
        with _ACTIVE_PROCESSES_LOCK:
            pids = {proc.pid for proc in _ACTIVE_PROCESSES}
        assert len(pids) == 1
        leader_pid = next(iter(pids))

        terminated = terminate_active_processes(grace_seconds=0.15)
        assert terminated == 1
        assert signals_sent == [
            (leader_pid, signal.SIGTERM),
            (leader_pid, signal.SIGKILL),
        ]
    finally:
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert results and results[0].returncode != 0
    with _ACTIVE_PROCESSES_LOCK:
        assert len(_ACTIVE_PROCESSES) == 0
    if child_pid is not None:
        # Process group kill must also have terminated the child process.
        _wait_until(lambda: _pid_gone(child_pid), timeout=2.0)


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


@pytest.mark.parametrize("exc_type", [ProcessLookupError, OSError])
def test_terminate_active_processes_handles_killpg_errors_cleanly(
    monkeypatch: pytest.MonkeyPatch, exc_type: type[OSError]
) -> None:
    fake_proc = MagicMock()
    fake_proc.pid = 999999
    fake_proc.poll.return_value = None

    def fail_killpg(_pgid: int, _sig: int) -> None:
        raise exc_type("gone")

    monkeypatch.setattr("gitwarden.git.os.killpg", fail_killpg)
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.add(fake_proc)
    try:
        assert terminate_active_processes(grace_seconds=0.5) == 0
    finally:
        with _ACTIVE_PROCESSES_LOCK:
            _ACTIVE_PROCESSES.discard(fake_proc)


def test_terminate_active_processes_handles_disappeared_process_during_grace_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_proc = MagicMock()
    fake_proc.pid = 888888
    # Still alive at SIGTERM and in the grace check, then disappears right before SIGKILL.
    fake_proc.poll.return_value = None
    calls: list[int] = []

    def killpg(_pgid: int, sig: int) -> None:
        calls.append(sig)
        if sig == signal.SIGKILL:
            raise ProcessLookupError("exited just before SIGKILL")

    monkeypatch.setattr("gitwarden.git.os.killpg", killpg)
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.add(fake_proc)
    try:
        assert terminate_active_processes(grace_seconds=0.05) == 1
        assert calls == [signal.SIGTERM, signal.SIGKILL]
    finally:
        with _ACTIVE_PROCESSES_LOCK:
            _ACTIVE_PROCESSES.discard(fake_proc)


def test_scheduler_stop_calls_terminate_active_processes_before_joining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "data")
    scheduler = Scheduler(store, tmp_path / "data", tick_seconds=0.05, workers=1)
    events: list[str] = []

    monkeypatch.setattr(
        "gitwarden.scheduler.terminate_active_processes",
        lambda: events.append("terminate") or 0,
    )

    scheduler.stop()
    assert events == []

    scheduler.start()
    orig_thread = scheduler._thread
    assert orig_thread is not None
    orig_join = orig_thread.join

    def spy_join(timeout: float | None = None) -> None:
        events.append("join")
        orig_join(timeout)

    monkeypatch.setattr(orig_thread, "join", spy_join)
    scheduler.stop()

    assert events[0] == "terminate"
    assert "join" in events


def test_scheduler_stop_terminates_blocked_git_without_waiting_stop_join_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready_file = tmp_path / "ssh-ready"
    blocker_script = tmp_path / "ssh_blocker.py"
    blocker_script.write_text(
        "import pathlib, time\n"
        f"pathlib.Path({str(ready_file)!r}).write_text('started')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "GIT_SSH_COMMAND",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(blocker_script))}",
    )

    data_dir = tmp_path / "data"
    store = Store(data_dir)
    task_id = store.add_task(
        "blocked-ssh",
        "ssh://example.invalid/repo.git",
        enabled=False,
    )
    scheduler = Scheduler(store, data_dir, tick_seconds=0.01, workers=1, stall_minutes=10)
    scheduler.start()
    try:
        scheduler.trigger(task_id)
        _wait_until(ready_file.exists, timeout=5.0)
        _wait_until(lambda: bool(store.list_runs(task_id)), timeout=5.0)

        started = time.monotonic()
        scheduler.stop()
        elapsed = time.monotonic() - started
    finally:
        if scheduler.status()["running"]:
            scheduler.stop()

    assert elapsed < 3.0
    runs = store.list_runs(task_id)
    assert len(runs) == 1
    assert runs[0]["finished_at"] is not None
    assert runs[0]["status"] == "failed"
    with _ACTIVE_PROCESSES_LOCK:
        assert len(_ACTIVE_PROCESSES) == 0


@pytest.mark.parametrize(
    ("url", "git_args"),
    [
        ("http://127.0.0.1:1/repo.git", ("ls-remote", "http://127.0.0.1:1/repo.git")),
        ("HTTP://127.0.0.1:1/repo.git", ("ls-remote", "HTTP://127.0.0.1:1/repo.git")),
        ("http://127.0.0.1:1/repo.git", ("fetch", "http://127.0.0.1:1/repo.git")),
        (
            "http://127.0.0.1:1/repo.git",
            ("push", "http://127.0.0.1:1/repo.git", "HEAD:refs/heads/main"),
        ),
        ("https://127.0.0.1:1/repo.git", ("ls-remote", "https://127.0.0.1:1/repo.git")),
    ],
)
def test_http_and_https_network_commands_set_low_speed_limits(
    tmp_path: Path, url: str, git_args: tuple[str, ...]
) -> None:
    repo = tmp_path / "repo"
    run(None, "init", str(repo))
    run(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--allow-empty",
        "-m",
        "init",
    )
    trace = tmp_path / "trace.json"
    observed_args: list[list[str]] = []

    with execution_context(observer=lambda args, _res, _ms: observed_args.append(args)):
        with pytest.raises(GitError):
            run(repo, *git_args, env={"GIT_TRACE2_EVENT": str(trace)})

    trace_text = trace.read_text()
    assert "http.lowSpeedLimit=1000" in trace_text
    assert "http.lowSpeedTime=600" in trace_text
    assert observed_args
    assert observed_args[0][:4] == [
        "-c",
        "http.lowSpeedLimit=1000",
        "-c",
        "http.lowSpeedTime=600",
    ]
    assert url in observed_args[0]


def test_non_http_and_non_network_commands_omit_low_speed_limits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run(None, "init", str(repo))
    observed_args: list[list[str]] = []

    with execution_context(observer=lambda args, _res, _ms: observed_args.append(args)):
        run(repo, "remote", "add", "origin", "http://127.0.0.1:1/repo.git")
        with pytest.raises(GitError):
            run(repo, "ls-remote", str(tmp_path / "missing.git"))

    assert len(observed_args) == 2
    for args in observed_args:
        assert "http.lowSpeedLimit=1000" not in args
        assert "http.lowSpeedTime=600" not in args
