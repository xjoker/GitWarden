"""源仓库变空或大量删除时，主镜像仓要 fail-closed；档案和历史仓照常保全。"""

import shutil
from pathlib import Path

import pytest

from gitwarden import target as tgt
from gitwarden.archive import capture, init_archive
from gitwarden.db import Store
from gitwarden.git import run
from gitwarden.i18n import render
from gitwarden.runner import _held_summary, _push_one, bind_target, retry_target, run_task
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
    assert sorted(res.delete_guard) == sorted(before)  # 两者同时成立
    assert mirror_refs(env.main) == before


# ---- 所有分支被删：不看剩下多少 tag ----


def _drop_all_branches(src: Path) -> None:
    run(src, "checkout", "-q", "--detach")
    for name in run(src, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.split():
        run(src, "branch", "-D", name)


def _add_tags(src: Path, count: int) -> None:
    for i in range(count):
        run(src, *ID, "tag", "-a", "-m", f"x{i}", f"x{i}")


@pytest.mark.parametrize(
    ("deletes", "total", "branch_deletes", "branches", "expected"),
    [
        (2, 7, 2, 2, True),  # 分支全没了，只剩 tag：新规则拦截
        (2, 7, 0, 0, False),  # 没有分支信息时退回原规则：2 < min_refs
        (1, 7, 1, 2, False),  # 还剩一个分支：不拦
        (4, 14, 4, 4, True),  # tag 稀释：4/14 < 0.5，但分支全删
        (7, 7, 2, 2, True),  # 删光
    ],
)
def test_all_branches_rule_unit(deletes, total, branch_deletes, branches, expected) -> None:
    guard = tgt.DeleteGuard()
    got = guard.trips(deletes, total, branch_deletes=branch_deletes, branches=branches)
    assert got is expected
    disabled = tgt.DeleteGuard(enabled=False)
    assert not disabled.trips(deletes, total, branch_deletes=branch_deletes, branches=branches)


def test_all_branches_deleted_with_tags_left_is_held(tmp_path: Path) -> None:
    """main + b1 + 5 个 tag，源上分支全没了、tag 都还在：2/7 不到门槛，但仍必须拦。"""
    env = Env(tmp_path, branches=2)
    _add_tags(env.src, 4)
    env.cap()
    assert push_target(env.ar, env.t).main_ok
    before = mirror_refs(env.main)
    assert len(before) == 7
    _drop_all_branches(env.src)
    env.cap()
    assert not tgt.DeleteGuard().trips(2, 7)  # 只看数量和比例时放行

    res = push_target(env.ar, env.t)

    assert res.delete_guard == ["refs/heads/b1", "refs/heads/main"]
    assert not res.main_ok
    assert mirror_refs(env.main) == before
    # 被删的分支照常保全到历史仓。
    preserved = refs(env.ar, "refs/preserved")
    assert len(preserved) == 2
    hist = refs(env.hist)
    for name, oid in preserved.items():
        assert hist["refs/tags/gw/" + name.removeprefix("refs/preserved/")] == oid


def test_tag_dilution_then_delete_all_branches_is_held(tmp_path: Path) -> None:
    """Elliot S5：先加 10 个垃圾 tag，下一轮删光 4 个真分支。旧规则 4/15 放行，现在拦。"""
    env = Env(tmp_path, branches=4)
    _add_tags(env.src, 10)
    env.cap()
    assert push_target(env.ar, env.t).main_ok
    before = mirror_refs(env.main)
    _drop_all_branches(env.src)
    env.cap()

    res = push_target(env.ar, env.t)

    assert len(res.delete_guard) == 4
    assert all(r.startswith("refs/heads/") for r in res.delete_guard)
    assert mirror_refs(env.main) == before


def test_some_branches_left_with_tags_is_not_held(tmp_path: Path) -> None:
    env = Env(tmp_path, branches=2)
    _add_tags(env.src, 4)
    env.cap()
    assert push_target(env.ar, env.t).main_ok
    run(env.src, "branch", "-D", "b1")
    env.cap()

    res = push_target(env.ar, env.t)

    assert res.main_ok and not res.delete_guard
    assert "refs/heads/b1" not in mirror_refs(env.main)


def test_all_branches_rule_is_off_when_guard_disabled(tmp_path: Path) -> None:
    env = Env(tmp_path, branches=2)
    _add_tags(env.src, 4)
    env.cap()
    assert push_target(env.ar, env.t).main_ok
    _drop_all_branches(env.src)
    env.cap()

    res = push_target(env.ar, env.t, guard=tgt.DeleteGuard(enabled=False))

    # 关闭后连同“删光/分支全删”一起放行；bare 目标默认拒绝删除 HEAD 分支，
    # 所以 main 留下并报冲突，b1 被删——与开启保护前的旧行为一致。
    assert not res.delete_guard
    assert "refs/heads/b1" not in mirror_refs(env.main)


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


def test_gate_block_outranks_delete_guard_at_runner_level(renv: RunEnv, monkeypatch) -> None:
    renv.empty_source()
    archive = renv.data / "archives" / f"{renv.store.get_task(renv.task_id)['archive_uuid']}.git"
    capture(archive, str(renv.src), "t")
    monkeypatch.setattr(tgt, "_push_history", lambda *a, **k: None)
    held: list[list[str]] = []

    status, _ = _push_one(renv.store, renv.data, archive, renv.store.get_target(renv.tid), held)

    assert status == "gate_blocked"
    assert held == []  # 没有进入 delete_guard 分支


def test_held_run_has_summary_message_and_is_not_initial_backup(renv: RunEnv) -> None:
    renv.empty_source()

    run_task(renv.store, renv.data, renv.task_id)
    run_task(renv.store, renv.data, renv.task_id)  # 第二轮：档案已无镜像 ref

    for row in renv.store.list_runs(renv.task_id)[:2]:
        assert row["status"] == "delete_guard"
        assert not row["initial_backup"]
        message = render(row["message"], "en")
        assert message.startswith("Mass deletion held: this round would delete 3 refs")
        assert "refs/heads/dev" in message and "refs/heads/main" in message


def test_retry_on_held_target_is_recorded_as_delete_guard(renv: RunEnv) -> None:
    renv.empty_source()
    run_task(renv.store, renv.data, renv.task_id)

    status = retry_target(renv.store, renv.data, renv.tid)

    assert status == "delete_guard"
    row = renv.store.list_runs(renv.task_id)[0]
    assert row["status"] == "delete_guard"
    assert "3 refs" in render(row["message"], "en")
    assert renv.store.get_target(renv.tid)["last_status"] == "delete_guard"


def test_summary_lists_first_refs_and_target_count() -> None:
    many = [f"refs/heads/b{i}" for i in range(8)]
    one = render(_held_summary([many]), "en")
    assert "8 refs" in one and "refs/heads/b4 …" in one and "refs/heads/b5" not in one
    two = render(_held_summary([many, many[:2]]), "en")
    assert two.startswith("Mass deletion held on 2 targets")
    assert _held_summary([]) is None


# ---- 同名重建（Elliot M7，D-all3 / D-one）：固定现有的无状态行为 ----
# 保护只按 ref 名字计数、不记被拦状态。下面两条测试把“同名重建即放行”和
# “部分重建后低于门槛的删除直接生效”固定下来；“记住被拦集合”留作 follow-up。


def _orphan(src: Path, label: str) -> str:
    tree = run(src, "mktree", input="").stdout.strip()
    return run(src, *ID, "commit-tree", tree, "-m", label).stdout.strip()


def _held_after_deleting_b1_to_b3(tmp_path: Path) -> tuple[Env, dict[str, str]]:
    env = Env(tmp_path, branches=4)  # main, b1, b2, b3 + v1 = 5 refs
    before = mirror_refs(env.main)
    assert len(before) == 5
    for name in ("b1", "b2", "b3"):
        run(env.src, "branch", "-D", name)
    env.cap()
    res = push_target(env.ar, env.t)
    assert res.delete_guard == ["refs/heads/b1", "refs/heads/b2", "refs/heads/b3"]
    assert mirror_refs(env.main) == before
    return env, before


def test_same_name_recreate_with_unrelated_commits_releases_guard(tmp_path: Path) -> None:
    """D-all3：被拦后源上用无关提交同名重建 b1..b3，下一轮自动放行，主镜像被非快进改写。"""
    env, before = _held_after_deleting_b1_to_b3(tmp_path)
    new = {}
    for name in ("b1", "b2", "b3"):
        new[name] = _orphan(env.src, f"recreated {name}")
        run(env.src, "branch", name, new[name])
    env.cap()

    res = push_target(env.ar, env.t)

    assert res.main_ok and not res.delete_guard
    after = mirror_refs(env.main)
    for name in ("b1", "b2", "b3"):
        assert after[f"refs/heads/{name}"] == new[name] != before[f"refs/heads/{name}"]
        # 旧提交在被拦的那一轮已经推到历史仓，可以找回。
        assert run(env.hist, "cat-file", "-e", before[f"refs/heads/{name}"] + "^{commit}")
    assert after["refs/heads/main"] == before["refs/heads/main"]


def test_partial_recreate_applies_remaining_deletes_below_threshold(tmp_path: Path) -> None:
    """D-one：只同名重建 b1；剩下 b2、b3 两个删除低于 min_refs=3，这一轮直接删掉。"""
    env, before = _held_after_deleting_b1_to_b3(tmp_path)
    new_b1 = _orphan(env.src, "recreated b1")
    run(env.src, "branch", "b1", new_b1)
    env.cap()

    res = push_target(env.ar, env.t)

    assert res.main_ok and not res.delete_guard
    after = mirror_refs(env.main)
    assert after["refs/heads/b1"] == new_b1
    assert "refs/heads/b2" not in after and "refs/heads/b3" not in after
    assert sorted(after) == ["refs/heads/b1", "refs/heads/main", "refs/tags/v1"]
    for name in ("b1", "b2", "b3"):
        assert run(env.hist, "cat-file", "-e", before[f"refs/heads/{name}"] + "^{commit}")
