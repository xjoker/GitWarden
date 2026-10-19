"""真实 Gitea / Forgejo 上的源仓库变空、404、401 与正常删除单个分支。"""

import uuid

from gitwarden.creds import git_env
from gitwarden.git import run
from gitwarden.provision import provision_target
from gitwarden.runner import run_task
from tests.conftest import ID, commit, make_source


def unique(prefix):
    return prefix + "-" + uuid.uuid4().hex[:10]


def remote_refs(forge, tmp_path, owner, name):
    """用 git ls-remote 读真实 ref；平台的 branches API 在推送后异步更新，不可靠。"""
    cred = {"kind": "https", "username": "gwadmin", "secret": forge.token}
    with git_env(cred, tmp_path / "admin-auth") as env:
        out = run(None, "ls-remote", "--refs", f"{forge.url}/{owner}/{name}.git", env=env).stdout
    return dict(reversed(line.split("\t")) for line in out.splitlines())


def branches_and_tags(forge, tmp_path, owner, name):
    refs = remote_refs(forge, tmp_path, owner, name)
    return (
        {r.removeprefix("refs/heads/") for r in refs if r.startswith("refs/heads/")},
        {r.removeprefix("refs/tags/") for r in refs if r.startswith("refs/tags/")},
    )


def seeded(forge, tmp_path, private=True):
    name = unique("gone")
    repo = forge.alice.create_repo("alice", name, private=private)
    source = make_source(tmp_path / "source")
    commit(source, "one")
    for branch in ("dev", "feat-a", "feat-b"):
        run(source, "branch", branch)
    run(source, *ID, "tag", "-a", "-m", "v1", "v1")
    cred = {"kind": "https", "username": "alice", "secret": forge.alice_token}
    with git_env(cred, tmp_path / "auth") as env:
        run(source, "push", repo["clone_url"], "--all", env=env)
        run(source, "push", repo["clone_url"], "--tags", env=env)
    alice_cred = forge.store.add_cred(unique("alice"), "https", "alice", forge.alice_token)
    task = forge.store.add_task(name, repo["clone_url"], source_cred_id=alice_cred)
    target_id = provision_target(forge.store, forge.data, task, forge.access_id, "backups", name)
    run_task(forge.store, forge.data, task)
    assert forge.store.get_target(target_id)["last_status"] == "ok"
    return name, repo, source, cred, task, target_id


def test_single_branch_delete_still_mirrors(forge, tmp_path):
    name, repo, source, cred, task, target_id = seeded(forge, tmp_path)
    with git_env(cred, tmp_path / "auth") as env:
        run(source, "push", repo["clone_url"], ":feat-a", env=env)

    run_task(forge.store, forge.data, task)

    assert forge.store.get_target(target_id)["last_status"] == "ok"
    branches, _ = branches_and_tags(forge, tmp_path, "backups", name)
    assert branches == {"main", "dev", "feat-b"}


def test_recreated_empty_source_is_held(forge, tmp_path):
    name, repo, source, cred, task, target_id = seeded(forge, tmp_path)
    before = branches_and_tags(forge, tmp_path, "backups", name)
    forge.api("DELETE", f"/repos/alice/{name}")
    forge.alice.create_repo("alice", name, private=True)

    run_task(forge.store, forge.data, task)

    assert forge.store.get_target(target_id)["last_status"] == "delete_guard"
    assert forge.store.get_task(task)["last_status"] == "delete_guard"
    assert branches_and_tags(forge, tmp_path, "backups", name) == before
    history = remote_refs(forge, tmp_path, "backups", f"{name}-history")
    assert len([r for r in history if r != "refs/tags/gw/binding"]) == 5


def test_deleted_source_404_and_private_401_keep_mirror(forge, tmp_path):
    name, repo, source, cred, task, target_id = seeded(forge, tmp_path)
    before = branches_and_tags(forge, tmp_path, "backups", name)
    forge.api("DELETE", f"/repos/alice/{name}")

    run_task(forge.store, forge.data, task)

    row = forge.store.list_runs(task)[0]
    assert row["status"] == "failed" and "not found" in row["message"].lower()
    assert branches_and_tags(forge, tmp_path, "backups", name) == before

    # 401：私有仓库、来源不带凭据。
    name2 = unique("private")
    forge.alice.create_repo("alice", name2, private=True)
    with git_env(cred, tmp_path / "auth") as env:
        run(source, "push", f"{forge.url}/alice/{name2}.git", "--all", env=env)
    forge.store.update_task(task, source_url=f"{forge.url}/alice/{name2}.git", source_cred_id=None)
    run_task(forge.store, forge.data, task)
    row = forge.store.list_runs(task)[0]
    assert row["status"] == "failed"
    assert "could not read username" in row["message"].lower()
    assert branches_and_tags(forge, tmp_path, "backups", name) == before
