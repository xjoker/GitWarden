"""Phase 2 Engine / Git / Platform P1 regression tests (P1-1, P1-3, P1-4, P1-5, P1-6, P1-7)."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from gitwarden import platform as plat
from gitwarden import runner
from gitwarden import target as tgt
from gitwarden.archive import ArchiveBusy, archive_lock, capture, init_archive
from gitwarden.db import Store
from gitwarden.git import execution_context, rev_parse, run
from gitwarden.platform import PlatformClient, PlatformError
from gitwarden.runner import archive_path, audit_target, bind_target, retry_target, run_task
from gitwarden.target import PUSH_BATCH_SIZE, Target, push_target
from tests.conftest import commit, make_source


def _setup_task(tmp_path: Path, *, num_targets: int = 1):
    data = tmp_path / "data"
    store = Store(data)
    src = make_source(tmp_path / "src")
    commit(src, "init")
    commit(src, "two")
    task_id = store.add_task("demo", str(src))
    targets = []
    for idx in range(num_targets):
        main = tmp_path / f"main-{idx}.git"
        hist = tmp_path / f"hist-{idx}.git"
        run(None, "init", "--bare", str(main))
        run(None, "init", "--bare", str(hist))
        tid = store.add_target(task_id, f"t{idx}", str(main), str(hist))
        bind_target(store, data, tid)
        targets.append((tid, main, hist))
    return data, store, src, task_id, targets


# --- P1-1: Single for-each-ref + eliminate redundant ls-remote calls ---


def test_p1_1_push_target_uses_single_for_each_ref_for_many_preserved_tags(tmp_path, monkeypatch):
    data, store, src, task_id, targets = _setup_task(tmp_path)
    _, main, _ = targets[0]
    archive = archive_path(data, store.get_task(task_id))
    run_task(store, data, task_id)

    for idx in range(5):
        commit(src, f"extra-{idx}")
        capture(archive, str(src), "demo")
        run(src, "reset", "--hard", "HEAD~1")
        commit(src, f"rewrite-{idx}")
        capture(archive, str(src), "demo")

    for_each_calls: list[tuple[str, ...]] = []
    real_run = tgt.run

    def spy_run(repo, *args, **kwargs):
        if args[:1] == ("for-each-ref",):
            for_each_calls.append(args)
        return real_run(repo, *args, **kwargs)

    monkeypatch.setattr(tgt, "run", spy_run)
    tid, _, _ = targets[0]
    row = store.get_target(tid)
    res = push_target(archive, Target(str(row["id"]), row["main_url"], row["history_url"]))
    assert res.main_ok
    assert len(res.history_pushed) == 5

    preserved_queries = [c for c in for_each_calls if "refs/preserved" in c]
    assert preserved_queries == [
        ("for-each-ref", "--format=%(refname) %(object)", "refs/preserved")
    ]


def test_p1_1_no_redundant_ls_remote_on_history_url(tmp_path, monkeypatch):
    data, store, src, task_id, targets = _setup_task(tmp_path)
    _, _, hist = targets[0]
    run_task(store, data, task_id)

    hist_ls_calls: list[str] = []
    real_ls_remote = tgt._ls_remote

    def spy_ls_remote(url, env=None):
        if url == str(hist):
            hist_ls_calls.append(url)
        return real_ls_remote(url, env=env)

    monkeypatch.setattr(tgt, "_ls_remote", spy_ls_remote)

    # No-change run (pending is empty): only 1 ls-remote on history_url (reused for history + audit)
    run_task(store, data, task_id)
    assert len(hist_ls_calls) == 1

    # Force push (pending is non-empty): 2 ls-remote calls on history_url
    # (binding check + post-push verify)
    hist_ls_calls.clear()
    run(src, "reset", "--hard", "HEAD~1")
    commit(src, "rewritten")
    run_task(store, data, task_id)
    assert len(hist_ls_calls) == 2


# --- P1-3: report.unknown included in audit_alert ---


def test_p1_3_unknown_history_tag_triggers_audit_alert_in_push_and_audit_target(tmp_path):
    data, store, src, task_id, targets = _setup_task(tmp_path)
    tid, _, hist = targets[0]
    run_task(store, data, task_id)

    binding_oid = rev_parse(hist, "refs/tags/gw/binding")
    assert binding_oid is not None
    run(hist, "update-ref", "refs/tags/gw/ROGUE1", binding_oid)

    # Standalone audit_target() flags unknown tags
    report = audit_target(store, data, tid)
    assert report.unknown == ["ROGUE1"]
    latest_run = store.list_runs(task_id)[0]
    assert latest_run["status"] == "audit_alert"
    assert "ROGUE1" in (latest_run["message"] or "")
    assert "run.audit_alert_unknown" in (latest_run["message"] or "")

    # Sync run_task() -> _push_one() also flags unknown tags as audit_alert
    run_task(store, data, task_id)
    target_row = store.get_target(tid)
    assert target_row["last_status"] == "audit_alert"
    assert "ROGUE1" in (target_row["last_detail"] or "")
    assert store.get_task(task_id)["last_status"] == "failed"


# --- P1-4: init_archive config idempotency + archive_lock ---


def test_p1_4_init_archive_skips_redundant_config_writes_and_repairs_drift(tmp_path, monkeypatch):
    from gitwarden import archive as arch

    ar = tmp_path / "archive.git"
    init_archive(ar)

    config_writes: list[tuple[str, ...]] = []
    real_run = arch.run

    def spy_run(repo, *args, **kwargs):
        if args[:1] == ("config",) and "--list" not in args:
            config_writes.append(args)
        return real_run(repo, *args, **kwargs)

    monkeypatch.setattr(arch, "run", spy_run)

    # Already-initialized archive -> 0 git config writes
    init_archive(ar)
    assert config_writes == []

    # If a key drifts, only that key is rewritten
    real_run(ar, "config", "gc.auto", "1")
    init_archive(ar)
    assert config_writes == [("config", "gc.auto", "0")]


def test_p1_4_archive_lock_protects_init_archive_and_run_task_marks_busy(tmp_path):
    data, store, _, task_id, _ = _setup_task(tmp_path)
    ar = archive_path(data, store.get_task(task_id))

    with archive_lock(ar):
        with pytest.raises(ArchiveBusy):
            init_archive(ar)
        with pytest.raises(ArchiveBusy):
            with archive_lock(ar):
                pass
        captured = run_task(store, data, task_id)

    assert captured is None
    assert store.get_task(task_id)["last_status"] == "busy"
    assert store.list_runs(task_id)[0]["status"] == "busy"


def test_p1_4_archive_lock_creates_missing_dir_and_init_still_initializes(tmp_path):
    ar = tmp_path / "new-archive.git"
    with archive_lock(ar):
        assert ar.exists()
    init_archive(ar)
    assert (ar / "HEAD").exists()


# --- P1-5: retry_target updates task.last_status accurately ---


def test_p1_5_retry_target_recovers_task_last_status_when_source_ok(tmp_path):
    data, store, _, task_id, targets = _setup_task(tmp_path, num_targets=2)
    t1, _, hist1 = targets[0]
    t2, _, hist2 = targets[1]

    away1 = tmp_path / "away1.git"
    away2 = tmp_path / "away2.git"
    shutil.move(hist1, away1)
    shutil.move(hist2, away2)

    # Source succeeds, both targets fail -> task.last_status == "failed"
    assert run_task(store, data, task_id) is True
    assert store.get_task(task_id)["last_status"] == "failed"
    assert store.get_task(task_id)["last_success_at"] is None
    last_run_at = store.get_task(task_id)["last_run_at"]

    # Recover first target -> task becomes "partial"
    shutil.move(away1, hist1)
    assert retry_target(store, data, t1) == "ok"
    assert store.get_target(t1)["last_status"] == "ok"
    assert store.get_task(task_id)["last_status"] == "partial"

    # Recover second target -> task becomes "ok" and sets last_success_at
    # without altering last_run_at
    shutil.move(away2, hist2)
    assert retry_target(store, data, t2) == "ok"
    task = store.get_task(task_id)
    assert task["last_status"] == "ok"
    assert task["last_success_at"] is not None
    assert task["last_run_at"] == last_run_at


def test_p1_5_retry_target_keeps_failed_when_source_capture_failed(tmp_path):
    data, store, _, task_id, targets = _setup_task(tmp_path)
    tid, _, _ = targets[0]
    run_task(store, data, task_id)

    # Break source URL so capture fails while target still succeeds
    store.update_task(task_id, source_url=str(tmp_path / "missing-src.git"))
    assert run_task(store, data, task_id) is False
    assert store.get_task(task_id)["last_status"] == "failed"

    # Retrying the healthy target does NOT clear the source failure
    assert retry_target(store, data, tid) == "ok"
    assert store.get_target(tid)["last_status"] == "ok"
    assert store.get_task(task_id)["last_status"] == "failed"


def test_p1_5_retry_target_busy_does_not_mark_healthy_task_failed(tmp_path):
    data, store, _, task_id, targets = _setup_task(tmp_path)
    tid, _, _ = targets[0]
    run_task(store, data, task_id)
    assert store.get_task(task_id)["last_status"] == "ok"

    ar = archive_path(data, store.get_task(task_id))
    with archive_lock(ar):
        assert retry_target(store, data, tid) == "busy"
    assert store.get_task(task_id)["last_status"] == "ok"


# --- P1-6: PlatformClient._list_hooks defensive pagination ---


def test_p1_6_list_hooks_rejects_non_list_response():
    client = PlatformClient("https://git.example.test", "secret-token", page_size=2)
    with patch.object(client, "_request", return_value=({"unexpected": True}, {})):
        with pytest.raises(PlatformError):
            client._list_hooks("/api/v1/repos/a/b/hooks")


def test_p1_6_list_hooks_stops_on_short_page_without_extra_request():
    client = PlatformClient("https://git.example.test", "secret-token", page_size=5)
    calls: list[str] = []

    def fake_request(_method, path):
        calls.append(path)
        return ([{"id": 1}, {"id": 2}], {})

    with patch.object(client, "_request", side_effect=fake_request):
        hooks = client._list_hooks("/api/v1/repos/a/b/hooks")

    assert hooks == [{"id": 1}, {"id": 2}]
    assert len(calls) == 1


def test_p1_6_list_hooks_stops_at_max_pages_on_broken_server(monkeypatch):
    client = PlatformClient("https://git.example.test", "secret-token", page_size=2)
    monkeypatch.setattr(plat.PlatformClient, "_MAX_HOOK_PAGES", 3)
    calls = 0

    def always_full(_method, _path):
        nonlocal calls
        calls += 1
        return ([{"id": calls * 2 - 1}, {"id": calls * 2}], {})

    with patch.object(client, "_request", side_effect=always_full):
        with pytest.raises(PlatformError):
            client._list_hooks("/api/v1/repos/a/b/hooks")

    assert calls == 3


# --- P1-7: Batched git push + stall_minutes on retry_target / audit_target ---


def test_p1_7_batched_git_push_for_history_and_main_updates_and_deletes(tmp_path, monkeypatch):
    assert PUSH_BATCH_SIZE == 150
    data, store, src, task_id, targets = _setup_task(tmp_path)
    tid, main, hist = targets[0]
    ar = archive_path(data, store.get_task(task_id))
    run_task(store, data, task_id)

    # Create 7 branches and 7 preserved tags
    base_oid = rev_parse(src, "refs/heads/main")
    assert base_oid is not None
    for idx in range(7):
        run(src, "update-ref", f"refs/heads/feat-{idx}", base_oid)
    capture(ar, str(src), "demo")

    for idx in range(7):
        run(src, "update-ref", "-d", f"refs/heads/feat-{idx}")
    capture(ar, str(src), "demo")
    # Re-create 5 branches so main has both updates and deletes
    for idx in range(5):
        run(src, "update-ref", f"refs/heads/new-{idx}", base_oid)
    capture(ar, str(src), "demo")

    push_calls: list[tuple[str, ...]] = []
    real_run = tgt.run

    def spy_run(repo, *args, **kwargs):
        if args[:1] == ("push",):
            push_calls.append(args)
        return real_run(repo, *args, **kwargs)

    monkeypatch.setattr(tgt, "PUSH_BATCH_SIZE", 3)
    monkeypatch.setattr(tgt, "run", spy_run)

    row = store.get_target(tid)
    res = push_target(
        ar,
        Target(str(row["id"]), row["main_url"], row["history_url"]),
        guard=tgt.DeleteGuard(enabled=False),
    )
    assert res.main_ok
    assert len(res.history_pushed) == 7

    # 7 preserved tags with batch size 3 -> 3 history push calls (3 + 3 + 1)
    hist_pushes = [c for c in push_calls if c[1] == str(hist)]
    assert len(hist_pushes) == 3
    # 5 main updates (new-0..4) with batch size 3 -> 2 main push calls (3 + 2)
    main_pushes = [c for c in push_calls if c[1] == str(main)]
    assert len(main_pushes) == 2

    # Now delete all 5 new-* branches so main deletes are also batched (5 deletes -> 2 batches)
    push_calls.clear()
    for idx in range(5):
        run(src, "update-ref", "-d", f"refs/heads/new-{idx}")
    capture(ar, str(src), "demo")
    res2 = push_target(
        ar,
        Target(str(row["id"]), row["main_url"], row["history_url"]),
        guard=tgt.DeleteGuard(enabled=False),
    )
    assert res2.main_ok
    main_delete_pushes = [c for c in push_calls if c[1] == str(main)]
    assert len(main_delete_pushes) == 2
    for idx in range(5):
        assert rev_parse(main, f"refs/heads/new-{idx}") is None


def test_p1_7_retry_and_audit_respect_stall_minutes_and_outer_context(tmp_path, monkeypatch):
    data, store, src, task_id, targets = _setup_task(tmp_path)
    tid, _, _ = targets[0]
    run_task(store, data, task_id)

    observed_stalls: list[float | None] = []
    real_exec = runner.execution_context

    def spy_exec_ctx(*, stall_minutes=None, on_output=None, observer=None):
        observed_stalls.append(stall_minutes)
        return real_exec(stall_minutes=stall_minutes, on_output=on_output, observer=observer)

    with monkeypatch.context() as mp:
        mp.setattr(runner, "execution_context", spy_exec_ctx)
        retry_target(store, data, tid, stall_minutes=4.5)
        audit_target(store, data, tid, stall_minutes=7.25)

    assert observed_stalls == [4.5, 7.25]

    from gitwarden.git import _execution_context

    with execution_context(stall_minutes=12.5):
        with runner._execution_context(lambda _t: None):
            assert _execution_context.get().stall_minutes == 12.5

    # End-to-end stall detection on audit_target with short stall_minutes
    upload_pack = tmp_path / "stalled-upload-pack"
    upload_pack.write_text("#!/bin/sh\nsleep 30\n")
    upload_pack.chmod(0o755)

    def stalled_audit(*_args, **_kwargs):
        run(None, "ls-remote", f"--upload-pack={upload_pack}", f"file://{src}")

    monkeypatch.setattr(runner, "audit", stalled_audit)
    with pytest.raises(Exception, match="Git stalled"):
        audit_target(store, data, tid, stall_minutes=0.02)

    latest = store.list_runs(task_id)[0]
    assert latest["status"] == "failed"
    assert "Git stalled" in (latest["message"] or "")
