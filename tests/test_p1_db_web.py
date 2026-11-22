import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import gitwarden.web as web
from gitwarden.archive import archive_lock
from gitwarden.config import Config
from gitwarden.db import Store
from gitwarden.git import _execution_context, run
from gitwarden.i18n import LANG_COOKIE, msg, render
from gitwarden.runner import archive_path, run_task
from gitwarden.ui_overview import _overview_context, task_summary
from gitwarden.web import create_app
from tests.conftest import commit, make_source

PASSWORD = "pw-p1-db-web-test"


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "data", admin_password=PASSWORD, stall_minutes=3.5)


@pytest.fixture
def client(config):
    app = create_app(config, start_scheduler=False)
    with TestClient(app) as test_client:
        yield test_client


def login(client):
    resp = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert resp.status_code == 303
    return client


def csrf_of(client, path="/"):
    match = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert match, f"{path} missing csrf token"
    return match.group(1)


# --- P1-2: SQLite secondary indexes & no N+1 / full-table event scan ---


def test_sqlite_secondary_indexes_created_and_used_in_query_plans(tmp_path):
    store = Store(tmp_path / "data")
    with sqlite3.connect(store.path) as conn:
        indexes = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
            ).fetchall()
        }
        expected = {
            "idx_targets_task_id": ("targets", ["task_id"]),
            "idx_events_task_id": ("events", ["task_id"]),
            "idx_runs_task_started": ("runs", ["task_id", "started_at"]),
            "idx_run_steps_run_id": ("run_steps", ["run_id"]),
            "idx_run_refs_run_id": ("run_refs", ["run_id"]),
            "idx_activity_at": ("activity", ["at"]),
        }
        for idx_name, (table, cols) in expected.items():
            assert indexes.get(idx_name) == table
            info = conn.execute(f"PRAGMA index_info('{idx_name}')").fetchall()
            assert [row[2] for row in info] == cols

        for sql in (
            "SELECT * FROM events WHERE task_id=1 ORDER BY id DESC LIMIT 10",
            "SELECT * FROM runs WHERE task_id=1 ORDER BY started_at DESC",
            "SELECT * FROM run_steps WHERE run_id=1 ORDER BY id",
            "SELECT * FROM run_refs WHERE run_id=1 ORDER BY id",
            "SELECT * FROM targets WHERE task_id=1 ORDER BY id",
            "SELECT * FROM activity WHERE at>='2026-01-01' ORDER BY at DESC",
        ):
            plan = " ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall())
            assert "USING INDEX" in plan, (sql, plan)


def test_overview_and_tasks_use_single_connection_without_full_event_scan(
    client, config, monkeypatch
):
    login(client)
    store = Store(config.data_dir)
    cred_id = store.add_cred("plat", "https", username="admin", secret="tok")
    instance_id = store.add_instance("gitea", "gitea", "https://git.example.test")
    access_id = store.add_access(instance_id, "all", cred_id)

    for idx in range(10):
        task_id = store.add_task(
            f"task-{idx}",
            f"https://source.example/repo-{idx}.git",
            instance_id=instance_id,
            access_id=access_id if idx % 2 == 0 else None,
            platform_repo_id=idx + 1 if idx % 2 == 0 else None,
        )
        tid = store.add_target(
            task_id,
            "nas",
            f"https://backup.example/m-{idx}.git",
            f"https://backup.example/h-{idx}.git",
        )
        store.set_bound(tid, True)
        store.set_target_status(tid, "ok", "ok")
        store.finish_task(task_id, "ok")
        run_id = store.start_run(task_id)
        store.finish_run(run_id, "ok", "Sync completed")
        store.add_event(
            task_id,
            "refs/heads/main",
            "force",
            "aaaa",
            "bbbb",
            f"refs/preserved/{idx}" if idx % 2 == 0 else None,
        )

    def forbid_list_events(*args, **kwargs):
        raise AssertionError("list_events must not be called by overview or _find_event")

    monkeypatch.setattr(Store, "list_events", forbid_list_events)

    connections = 0
    orig_connect = Store._connect

    def counting_connect(self):
        nonlocal connections
        connections += 1
        return orig_connect(self)

    monkeypatch.setattr(Store, "_connect", counting_connect)

    # _overview_context itself opens exactly 1 connection for all N=10 tasks
    connections = 0
    fake_req = SimpleNamespace(app=client.app)
    overview_ctx = _overview_context(fake_req, store)
    assert connections == 1
    assert len(overview_ctx["tasks"]) == 10

    # Full HTTP GET / and GET /tasks open only O(1) connections (3: 1 snapshot + 2 scheduler.status)
    connections = 0
    resp = client.get("/")
    assert resp.status_code == 200
    assert connections == 3
    assert "task-0" in resp.text

    connections = 0
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert connections == 3
    assert "task-9" in resp.text

    # Verify preserved counts were aggregated accurately via SQL
    assert store.count_events_by_task(preserved_only=True) == {
        i + 1: 1 for i in range(10) if i % 2 == 0
    }
    assert store.count_events_by_task(preserved_only=False) == {i + 1: 1 for i in range(10)}


def test_find_event_uses_point_lookup_and_respects_task_id(config, monkeypatch):
    store = Store(config.data_dir)
    t1 = store.add_task("t1", "https://source.example/1.git")
    t2 = store.add_task("t2", "https://source.example/2.git")
    store.add_event(t1, "refs/heads/main", "force", "a", "b", "refs/preserved/1")
    e1 = Store._rows(store, "SELECT id FROM events WHERE task_id=?", (t1,))[0]["id"]

    monkeypatch.setattr(
        Store,
        "list_events",
        lambda *a, **kw: pytest.fail("list_events must not be called by _find_event"),
    )
    assert web._find_event(store, t1, e1)["id"] == e1
    assert web._find_event(store, t2, e1) is None


# --- P1-3: audit_alert unknown tags surfacing ---


def test_audit_alert_unknown_renders_in_en_and_zh_and_overview_latest_runs(client, config):
    login(client)
    store = Store(config.data_dir)
    task_id = store.add_task("audit-task", "https://source.example/audit.git")
    combined = (
        msg("run.audit_alert", missing=["gw/1"], modified=[])
        + " "
        + msg("run.audit_alert_unknown", count=2, items="gw/rogue1, gw/rogue2")
    )
    assert "1" in render(combined, "en") and "2 unexpected tag(s)" in render(combined, "en")
    assert "gw/rogue1, gw/rogue2" in render(combined, "en")
    assert "多出 2 个未知标签" in render(combined, "zh_CN")

    run_id = store.start_run(task_id)
    store.finish_run(run_id, "audit_alert", combined)

    en_page = client.get("/").text
    assert "Audit alert" in en_page
    client.cookies.set(LANG_COOKIE, "zh_CN")
    zh_page = client.get("/").text
    assert "核对告警" in zh_page


# --- P1-4: event_bundle archive lock ---


def test_event_bundle_uses_archive_lock_and_returns_409_when_busy(client, config):
    login(client)
    source = make_source(config.data_dir.parent / "bundle-src")
    commit(source, "one")
    commit(source, "two")
    store = Store(config.data_dir)
    task_id = store.add_task("bundle-lock", str(source))
    run_task(store, config.data_dir, task_id)
    run(source, "reset", "--hard", "HEAD~1")
    run_task(store, config.data_dir, task_id)
    event = store.list_events(task_id)[0]
    task = store.get_task(task_id)
    archive = archive_path(config.data_dir, task)

    with archive_lock(archive):
        busy_resp = client.get(f"/tasks/{task_id}/events/{event['id']}/bundle")
    assert busy_resp.status_code == 409
    assert not list((config.data_dir / "work").glob("bundle-*"))

    ok_resp = client.get(f"/tasks/{task_id}/events/{event['id']}/bundle")
    assert ok_resp.status_code == 200
    assert ok_resp.content.startswith(b"# v")


# --- P1-5: overview status derivation ---


def test_overview_status_does_not_mislabel_healthy_or_partial_targets_as_source_failing(
    client, config
):
    login(client)
    store = Store(config.data_dir)

    # Case 1: Recovered task + ok target -> healthy
    t_ok = store.add_task("recovered", "https://source.example/ok.git")
    targ_ok = store.add_target(
        t_ok, "nas-ok", "https://b.example/m1.git", "https://b.example/h1.git"
    )
    store.set_bound(targ_ok, True)
    store.set_target_status(targ_ok, "ok", "Sync completed")
    store.finish_task(t_ok, "ok")

    summary_ok = task_summary(store, store.get_task(t_ok))
    assert summary_ok["status_key"] == "healthy"
    assert summary_ok["targets"][0]["status_key"] == "tasks.target.healthy"

    # Case 2: Partial task (one ok target, one failed target) -> needs_attention, ok stays healthy
    t_part = store.add_task("partial-task", "https://source.example/part.git")
    targ_a = store.add_target(
        t_part, "nas-a", "https://b.example/ma.git", "https://b.example/ha.git"
    )
    targ_b = store.add_target(
        t_part, "nas-b", "https://b.example/mb.git", "https://b.example/hb.git"
    )
    store.set_bound(targ_a, True)
    store.set_bound(targ_b, True)
    store.set_target_status(targ_a, "ok", "Sync completed")
    store.set_target_status(targ_b, "error", "push failed")
    store.finish_task(t_part, "partial")

    summary_part = task_summary(store, store.get_task(t_part))
    assert summary_part["status_key"] == "needs_attention"
    assert [t["status_key"] for t in summary_part["targets"]] == [
        "tasks.target.healthy",
        "tasks.target.failed",
    ]

    # Case 3: Genuine source failure -> failing & source_failing
    t_fail = store.add_task("source-fail", "https://source.example/fail.git")
    targ_f = store.add_target(
        t_fail, "nas-f", "https://b.example/mf.git", "https://b.example/hf.git"
    )
    store.set_bound(targ_f, True)
    store.set_target_status(targ_f, "ok", "Sync completed")
    store.finish_task(t_fail, "failed")

    summary_fail = task_summary(store, store.get_task(t_fail))
    assert summary_fail["status_key"] == "failing"
    assert summary_fail["targets"][0]["status_key"] == "tasks.target.source_failing"

    # Also verify _overview_context end-to-end
    html = client.get("/").text
    assert "nas-a · healthy" in html
    assert "nas-f · source failing" in html


# --- P1-7: WebUI git operations propagate config.stall_minutes ---


def test_web_git_operations_propagate_config_stall_minutes(client, config, monkeypatch):
    login(client)
    store = Store(config.data_dir)
    cred_id = store.add_cred("plat", "https", username="admin", secret="tok")
    instance_id = store.add_instance("gitea", "gitea", "https://git.example.test")
    access_id = store.add_access(instance_id, "all", cred_id)
    task_id = store.add_task("stall-test", "https://source.example/stall.git")
    target_id = store.add_target(
        task_id, "nas", "https://b.example/m.git", "https://b.example/h.git"
    )
    store.add_event(task_id, "refs/heads/main", "force", "a", "b", "refs/preserved/1")
    event_id = store._rows("SELECT id FROM events WHERE task_id=?", (task_id,))[0]["id"]

    observed: list[tuple[str, float]] = []

    def record(name):
        observed.append((name, _execution_context.get().stall_minutes))

    monkeypatch.setattr(web, "retry_target", lambda *a, **kw: record("retry"))
    monkeypatch.setattr(web, "audit_target", lambda *a, **kw: record("audit"))
    monkeypatch.setattr(web, "bind_target", lambda *a, **kw: record("bind"))
    monkeypatch.setattr(web, "provision_target", lambda *a, **kw: record("provision") or 1)

    orig_run = web.run

    def fake_bundle_run(repo, *args, **kwargs):
        if args and args[0] == "bundle":
            record("bundle")
            Path(args[2]).write_bytes(b"# v2 git bundle\n")
            return None
        return orig_run(repo, *args, **kwargs)

    monkeypatch.setattr(web, "run", fake_bundle_run)

    csrf = csrf_of(client, f"/tasks/{task_id}")
    assert (
        client.post(
            f"/targets/{target_id}/retry", data={"csrf": csrf}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/targets/{target_id}/audit", data={"csrf": csrf}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/targets/{target_id}/bind", data={"csrf": csrf}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/tasks/{task_id}/targets/provision",
            data={"csrf": csrf, "access_id": str(access_id), "owner": "o", "name": "n"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert client.get(f"/tasks/{task_id}/events/{event_id}/bundle").status_code == 200

    assert observed == [
        ("retry", 3.5),
        ("audit", 3.5),
        ("bind", 3.5),
        ("provision", 3.5),
        ("bundle", 3.5),
    ]


# --- P1-8: Credential validation rules ---


@pytest.mark.parametrize(
    "lang,expected",
    [
        ("en", "password or access token is required"),
        ("zh_CN", "必须提供密码或访问令牌"),
    ],
)
def test_create_https_credential_requires_non_empty_secret(client, lang, expected):
    login(client)
    client.cookies.set(LANG_COOKIE, lang)
    resp = client.post(
        "/creds",
        data={
            "csrf": csrf_of(client, "/creds"),
            "name": "empty-secret",
            "kind": "https",
            "username": "bot",
            "secret": "   ",
        },
    )
    assert resp.status_code == 400
    assert expected in resp.text
    assert Store(client.app.state.data_dir).list_creds() == []


@pytest.mark.parametrize(
    "lang,expected",
    [
        ("en", "Known hosts are required"),
        ("zh_CN", "必须提供已知主机信息"),
    ],
)
def test_create_ssh_credential_requires_non_empty_known_hosts(client, lang, expected):
    login(client)
    client.cookies.set(LANG_COOKIE, lang)
    resp = client.post(
        "/creds",
        data={
            "csrf": csrf_of(client, "/creds"),
            "name": "empty-kh",
            "kind": "ssh",
            "username": "git",
            "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nKEY\n",
            "known_hosts": "   \n",
        },
    )
    assert resp.status_code == 400
    assert expected in resp.text
    assert Store(client.app.state.data_dir).list_creds() == []


@pytest.mark.parametrize(
    "lang,expected",
    [
        ("en", "Username cannot be cleared"),
        ("zh_CN", "不能清空用户名"),
    ],
)
def test_edit_https_credential_used_by_access_forbids_clearing_username(client, lang, expected):
    login(client)
    store = Store(client.app.state.data_dir)
    used_id = store.add_cred("used", "https", username="admin", secret="tok1")
    unused_id = store.add_cred("unused", "https", username="user", secret="tok2")
    instance_id = store.add_instance("gitea", "gitea", "https://git.example.test")
    store.add_access(instance_id, "acc", used_id)

    client.cookies.set(LANG_COOKIE, lang)
    blocked = client.post(
        f"/creds/{used_id}/edit",
        data={
            "csrf": csrf_of(client, f"/creds?edit={used_id}"),
            "name": "used",
            "username": "   ",
            "secret": "",
        },
    )
    assert blocked.status_code == 400
    assert expected in blocked.text
    assert store.get_cred(used_id)["username"] == "admin"

    # Unreferenced HTTPS credential can still clear username
    allowed = client.post(
        f"/creds/{unused_id}/edit",
        data={
            "csrf": csrf_of(client, f"/creds?edit={unused_id}"),
            "name": "unused",
            "username": "",
            "secret": "",
        },
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    assert store.get_cred(unused_id)["username"] is None
