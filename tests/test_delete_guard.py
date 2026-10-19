"""源仓库变空或大量删除时，主镜像仓要 fail-closed；档案和历史仓照常保全。"""

import shutil
from pathlib import Path

import pytest

from gitwarden import target as tgt
from gitwarden.archive import capture, init_archive
from gitwarden.db import Store
from gitwarden.git import run
from gitwarden.runner import _push_one, bind_target, run_task
from gitwarden.target import Target, bind, push_target
from tests.conftest import ID, commit, make_source


def refs(repo: Path, *prefixes: str) -> dict[str, str]:
    out = run(repo, "for-each-ref", "--format=%(refname) %(objectname)", *prefixes).stdout
    return dict(line.split(" ") for line in out.splitlines())


def mirror_refs(repo: Path) -> dict[str, str]:
    return refs(repo, "refs/heads", "refs/tags")


class Env:
    def __init__(self, tmp: Path, branches: int = 3) -> None:
        self.tmp = tmp
        self.src = make_source(tmp / "src")
        commit(self.src, "a")
        for i in range(1, branches):
            run(self.src, "branch", f"b{i}")
        run(self.src, *ID, "tag", "-a", "-m", "v1", "v1")
        self.ar = tmp / "archive.git"
        init_archive(self.ar)
        self.main = tmp / "main.git"
        self.hist = tmp / "hist.git"
        run(None, "init", "--bare", str(self.main))
        run(None, "init", "--bare", str(self.hist))
        self.t = Target("t1", str(self.main), str(self.hist))
        capture(self.ar, str(self.src), "src1")
        bind(self.ar, self.t)
        assert push_target(self.ar, self.t).main_ok

    def cap(self) -> None:
        capture(self.ar, str(self.src), "src1")

    def empty_source(self) -> None:
        shutil.rmtree(self.src)
        run(None, "init", "--bare", str(self.src))


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_empty_source_keeps_main_mirror_and_preserves_history(env: Env) -> None:
    before = mirror_refs(env.main)
    assert len(before) == 4
    env.empty_source()
    env.cap()

    res = push_target(env.ar, env.t)

    # 主镜像一个 ref 都不能少。
    assert mirror_refs(env.main) == before
    assert not res.main_ok
    assert sorted(res.delete_guard) == sorted(before)
    # 档案和历史仓照常保全：每个被删的 ref 都有保留标签，并已推到历史仓。
    preserved = refs(env.ar, "refs/preserved")
    assert len(preserved) == 4
    hist = refs(env.hist)
    for name, oid in preserved.items():
        assert hist["refs/tags/gw/" + name.removeprefix("refs/preserved/")] == oid


def test_guard_holds_every_main_update_in_the_round(env: Env) -> None:
    """拦截时连同同轮的更新一起不推，不会留下“删了一半”或“只更新没删除”的半截状态。"""
    before = mirror_refs(env.main)
    for i in (1, 2):
        run(env.src, "branch", "-D", f"b{i}")
    run(env.src, "tag", "-d", "v1")
    commit(env.src, "new on main")  # 同轮还有一个快进更新
    env.cap()

    res = push_target(env.ar, env.t)

    assert sorted(res.delete_guard) == ["refs/heads/b1", "refs/heads/b2", "refs/tags/v1"]
    assert mirror_refs(env.main) == before


def test_single_branch_delete_is_not_held(tmp_path: Path) -> None:
    env = Env(tmp_path, branches=6)
    run(env.src, "branch", "-D", "b3")
    env.cap()

    res = push_target(env.ar, env.t)

    assert res.main_ok and not res.delete_guard
    assert "refs/heads/b3" not in mirror_refs(env.main)


def test_small_repo_single_branch_delete_is_not_held(tmp_path: Path) -> None:
    """main + 一个分支，删掉分支就是 50%；数量低于下限，不应误拦。"""
    env = Env(tmp_path, branches=2)
    run(env.src, "tag", "-d", "v1")
    run(env.src, "branch", "-D", "b1")
    env.cap()

    res = push_target(env.ar, env.t)

    assert res.main_ok and not res.delete_guard
    assert list(mirror_refs(env.main)) == ["refs/heads/main"]


def test_last_single_ref_delete_is_held(tmp_path: Path) -> None:
    """只剩一个 ref 时删除也等于清空，必须拦。"""
    env = Env(tmp_path, branches=1)
    run(env.src, "tag", "-d", "v1")
    env.cap()
    assert push_target(env.ar, env.t).main_ok
    env.empty_source()
    env.cap()

    res = push_target(env.ar, env.t)

    assert res.delete_guard == ["refs/heads/main"]
    assert list(mirror_refs(env.main)) == ["refs/heads/main"]


def test_ratio_threshold_is_configurable(env: Env) -> None:
    before = mirror_refs(env.main)
    run(env.src, "branch", "-D", "b1")
    run(env.src, "branch", "-D", "b2")
    env.cap()  # 4 个里删 2 个 = 50%，数量 2

    loose = push_target(env.ar, env.t, guard=tgt.DeleteGuard(ratio=0.9, min_refs=1))
    assert loose.main_ok
    assert "refs/heads/b1" not in mirror_refs(env.main)
    assert before != mirror_refs(env.main)


def test_ratio_threshold_trips_when_reached(env: Env) -> None:
    before = mirror_refs(env.main)
    run(env.src, "branch", "-D", "b1")
    run(env.src, "branch", "-D", "b2")
    env.cap()

    res = push_target(env.ar, env.t, guard=tgt.DeleteGuard(ratio=0.5, min_refs=2))

    assert res.delete_guard == ["refs/heads/b1", "refs/heads/b2"]
    assert mirror_refs(env.main) == before


def test_disabled_guard_restores_old_behavior(env: Env) -> None:
    env.empty_source()
    env.cap()

    res = push_target(env.ar, env.t, guard=tgt.DeleteGuard(enabled=False))

    assert res.main_ok
    assert mirror_refs(env.main) == {}


def test_source_restored_clears_guard(env: Env) -> None:
    good = shutil.copytree(env.src, env.tmp / "good")
    before = mirror_refs(env.main)
    env.empty_source()
    env.cap()
    assert push_target(env.ar, env.t).delete_guard
    shutil.rmtree(env.src)
    shutil.copytree(good, env.src)
    env.cap()

    res = push_target(env.ar, env.t)

    assert res.main_ok and not res.delete_guard
    assert mirror_refs(env.main) == before


def test_gate_block_outranks_delete_guard(env: Env, monkeypatch) -> None:
    before = mirror_refs(env.main)
    env.empty_source()
    env.cap()
    monkeypatch.setattr(tgt, "_push_history", lambda *a, **k: None)

    res = push_target(env.ar, env.t)

    assert res.gate_blocked  # 历史未确认：现有门禁照样生效
    assert mirror_refs(env.main) == before


# ---- runner 层：状态、优先级 ----


class RunEnv:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.data = tmp / "data"
        self.store = Store(self.data)
        self.src = make_source(tmp / "src")
        commit(self.src, "one")
        run(self.src, "branch", "dev")
        run(self.src, "tag", "v1")
        self.task_id = self.store.add_task("t", str(self.src))
        self.main = tmp / "main.git"
        self.hist = tmp / "hist.git"
        run(None, "init", "--bare", str(self.main))
        run(None, "init", "--bare", str(self.hist))
        self.tid = self.store.add_target(self.task_id, "a", str(self.main), str(self.hist))
        bind_target(self.store, self.data, self.tid)
        run_task(self.store, self.data, self.task_id)
        assert self.store.get_target(self.tid)["last_status"] == "ok"

    def empty_source(self) -> None:
        shutil.rmtree(self.src)
        run(None, "init", "--bare", str(self.src))


@pytest.fixture
def renv(tmp_path: Path) -> RunEnv:
    return RunEnv(tmp_path)


def test_runner_marks_needs_attention_and_keeps_events(renv: RunEnv) -> None:
    before = mirror_refs(renv.main)
    renv.empty_source()

    run_task(renv.store, renv.data, renv.task_id)

    assert mirror_refs(renv.main) == before
    target = renv.store.get_target(renv.tid)
    assert target["last_status"] == "delete_guard"
    assert "refs/heads/main" in target["last_detail"]
    run_row = renv.store.list_runs(renv.task_id)[0]
    assert run_row["status"] == "delete_guard"
    assert renv.store.get_task(renv.task_id)["last_status"] == "delete_guard"
    events = renv.store.list_events(renv.task_id)
    assert sorted(e["kind"] for e in events) == ["delete", "delete", "delete"]
    assert all(e["preserved_ref"] for e in events)

    # 下一轮源仍为空：不重复记事件，仍然拦截。
    run_task(renv.store, renv.data, renv.task_id)
    assert len(renv.store.list_events(renv.task_id)) == 3
    assert renv.store.get_target(renv.tid)["last_status"] == "delete_guard"
    assert mirror_refs(renv.main) == before


def test_history_conflict_outranks_delete_guard(renv: RunEnv) -> None:
    renv.empty_source()
    archive = renv.data / "archives" / f"{renv.store.get_task(renv.task_id)['archive_uuid']}.git"
    capture(archive, str(renv.src), "t")
    # 在历史仓预先占用一个保留标签名，制造 history_conflict。
    name = next(iter(refs(archive, "refs/preserved"))).removeprefix("refs/preserved/")
    other = refs(archive, "refs/pushed/" + str(renv.tid) + "/binding")
    run(archive, "push", str(renv.hist), f"{next(iter(other.values()))}:refs/tags/gw/{name}")

    status, _ = _push_one(renv.store, renv.data, archive, renv.store.get_target(renv.tid))

    assert status == "history_conflict"
