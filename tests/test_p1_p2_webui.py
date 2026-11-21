import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import gitwarden.web as web
from gitwarden.config import Config
from gitwarden.db import Store
from gitwarden.i18n import LANG_COOKIE
from gitwarden.platform import PlatformError
from gitwarden.ui_overview import _overview_context, task_summary
from gitwarden.ui_task import _clone_dir, _event_view
from gitwarden.web import create_app

PASSWORD = "pw-p1-p2-webui-test"


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "data", admin_password=PASSWORD)


@pytest.fixture
def client(config):
    app = create_app(config, start_scheduler=False)
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient) -> TestClient:
    resp = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert resp.status_code == 303
    return client


def csrf_of(client: TestClient, path: str = "/") -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert match, f"{path} missing csrf token"
    return match.group(1)


# --- P1-9.1: Platform instance delete UI ---


def test_platform_instance_delete_ui_confirmation_and_in_use_guard(client, config):
    login(client)
    store = Store(config.data_dir)
    cred_id = store.add_cred("plat-cred", "https", username="admin", secret="tok")
    unused_inst = store.add_instance("Unused Forgejo", "forgejo", "https://unused.example")
    used_inst = store.add_instance("Used Gitea", "gitea", "https://used.example")
    store.add_access(used_inst, "team-a", cred_id)

    page = client.get("/platforms").text
    assert f'action="/instances/{unused_inst}/delete"' in page
    assert "confirm-delete" in page
    assert f'action="/instances/{used_inst}/delete"' not in page
    assert "disabled" in page

    # POST delete on in-use instance returns 400 and preserves instance
    blocked = client.post(
        f"/instances/{used_inst}/delete",
        data={"csrf": csrf_of(client, "/platforms")},
        follow_redirects=False,
    )
    assert blocked.status_code == 400
    assert "referenced" in blocked.text
    assert store.get_instance(used_inst) is not None

    # POST delete on unused instance removes it
    deleted = client.post(
        f"/instances/{unused_inst}/delete",
        data={"csrf": csrf_of(client, "/platforms")},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert store.get_instance(unused_inst) is None


# --- P1-9.2: Access preview/save error preserves draft & selected access ---


def test_access_preview_and_save_errors_keep_selected_access_and_draft_and_save_redirects_with_id(
    client, config, monkeypatch
):
    login(client)
    store = Store(config.data_dir)
    cred_id = store.add_cred("plat-cred", "https", username="admin", secret="tok")
    inst_id = store.add_instance("Forgejo", "forgejo", "https://git.example")
    access1 = store.add_access(inst_id, "first-access", cred_id)
    access2 = store.add_access(inst_id, "second-access", cred_id)

    monkeypatch.setattr(
        web,
        "preview",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PlatformError("upstream down")),
    )
    monkeypatch.setattr(client.app.state.scheduler, "trigger_discovery", lambda _id: None)

    draft_payload = {
        "csrf": csrf_of(client, f"/platforms?access_id={access2}"),
        "name": "second-draft-edited",
        "cred_id": str(cred_id),
        "scope_mode": "owners",
        "owners": "alice, bob",
        "include_forks": ["0", "1"],
        "include_archived": "0",
        "include_public": ["0", "1"],
        "include_private": ["0", "1"],
        "rule_mode": "include",
        "patterns": "alice/*",
        "auto_enable": "0",
        "detailed_default": ["0", "1"],
        "discover_interval_minutes": "120",
        "default_sync_interval_minutes": "90",
    }

    # Preview PlatformError keeps access2 selected and preserves all draft fields
    preview_err = client.post(f"/accesses/{access2}/preview", data=draft_payload)
    assert preview_err.status_code == 400
    assert f'data-preview-access-id="{access2}"' in preview_err.text
    assert f'formaction="/accesses/{access2}"' in preview_err.text
    assert 'name="name" value="second-draft-edited"' in preview_err.text
    assert 'name="discover_interval_minutes" value="120"' in preview_err.text
    assert 'name="default_sync_interval_minutes" value="90"' in preview_err.text
    assert ">alice\nbob</textarea>" in preview_err.text
    assert ">alice/*</textarea>" in preview_err.text

    # Preview validation error (invalid integer) also preserves access2 and raw draft
    bad_preview = client.post(
        f"/accesses/{access2}/preview",
        data={**draft_payload, "discover_interval_minutes": "0"},
    )
    assert bad_preview.status_code == 400
    assert f'data-preview-access-id="{access2}"' in bad_preview.text
    assert 'name="name" value="second-draft-edited"' in bad_preview.text
    assert 'name="discover_interval_minutes" value="0"' in bad_preview.text

    # Save validation error (owners mode with empty owners) keeps access2 & draft
    save_err = client.post(
        f"/accesses/{access2}",
        data={**draft_payload, "owners": ""},
    )
    assert save_err.status_code == 400
    assert f'data-preview-access-id="{access2}"' in save_err.text
    assert 'name="name" value="second-draft-edited"' in save_err.text
    assert 'name="discover_interval_minutes" value="120"' in save_err.text

    # Successful save redirects with ?access_id=<access2> (not jumping back to access1)
    saved = client.post(
        f"/accesses/{access2}",
        data=draft_payload,
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == f"/platforms?access_id={access2}"
    assert f'formaction="/accesses/{access2}"' in client.get(saved.headers["location"]).text
    assert access1 != access2


# --- P1-9.3: Wizard step 2 CSS mode toggle & step 3 provisioning failure rollback ---


def test_wizard_step2_renders_css_mode_toggle_and_step3_failure_rolls_back_orphan_task(
    client, config, monkeypatch
):
    login(client)
    store = Store(config.data_dir)
    cred_id = store.add_cred("plat-cred", "https", username="admin", secret="tok")
    inst_id = store.add_instance("Forgejo", "forgejo", "https://git.example")
    access_id = store.add_access(inst_id, "team", cred_id)

    step2 = client.post(
        "/tasks/new",
        data={
            "csrf": csrf_of(client, "/tasks/new"),
            "step": "source",
            "name": "plat-wizard-task",
            "source_url": "https://source.example/repo.git",
            "interval_minutes": "60",
            "enabled": "on",
        },
    )
    assert step2.status_code == 200
    assert "mode-platform" in step2.text
    assert "mode-manual" in step2.text
    assert 'name="provision_name"' in step2.text
    assert 'name="target_name"' in step2.text
    assert ".wizard-targets:has(" in step2.text
    assert "<script" not in step2.text.lower()

    # Submit step 2 in platform mode -> reaches step 3 review with provision_name preserved
    step3_preview = client.post(
        "/tasks/new",
        data={
            "csrf": csrf_of(client, "/tasks/new"),
            "step": "targets",
            "mode": "platform",
            "name": "plat-wizard-task",
            "source_url": "https://source.example/repo.git",
            "interval_minutes": "60",
            "enabled": "on",
            "access_id": str(access_id),
            "owner": "ops",
            "provision_name": "repo-backup",
            "target_name": "",
        },
    )
    assert step3_preview.status_code == 200
    assert "ops/repo-backup" in step3_preview.text

    # Step 3 provisioning failure rolls back newly created task and empty archive dir
    def failing_provision(store_arg, data_dir, task_id, *_args):
        archive = data_dir / "archives" / f"{store_arg.get_task(task_id)['archive_uuid']}.git"
        archive.mkdir(parents=True, exist_ok=True)
        raise PlatformError("platform repo already exists")

    monkeypatch.setattr("gitwarden.ui_task.provision_target", failing_provision)
    failed_create = client.post(
        "/tasks/new",
        data={
            "csrf": csrf_of(client, "/tasks/new"),
            "step": "review",
            "mode": "platform",
            "name": "plat-wizard-task",
            "source_url": "https://source.example/repo.git",
            "interval_minutes": "60",
            "enabled": "on",
            "access_id": str(access_id),
            "owner": "ops",
            "provision_name": "repo-backup",
            "confirm_writer": "on",
            "confirm_private": "on",
            "confirm_no_ci": "on",
        },
    )
    assert failed_create.status_code == 400
    assert "platform repo already exists" in failed_create.text
    assert store.list_tasks() == []
    archives_dir = config.data_dir / "archives"
    assert not archives_dir.exists() or list(archives_dir.iterdir()) == []

    # Retrying with the exact same task name succeeds (no orphan collision)
    monkeypatch.setattr("gitwarden.ui_task.provision_target", lambda *_a, **_kw: 1)
    monkeypatch.setattr(client.app.state.scheduler, "trigger", lambda _id: None)
    retry_create = client.post(
        "/tasks/new",
        data={
            "csrf": csrf_of(client, "/tasks/new"),
            "step": "review",
            "mode": "platform",
            "name": "plat-wizard-task",
            "source_url": "https://source.example/repo.git",
            "interval_minutes": "60",
            "enabled": "on",
            "access_id": str(access_id),
            "owner": "ops",
            "provision_name": "repo-backup",
            "confirm_writer": "on",
            "confirm_private": "on",
            "confirm_no_ci": "on",
        },
        follow_redirects=False,
    )
    assert retry_create.status_code == 303
    assert [row["name"] for row in store.list_tasks()] == ["plat-wizard-task"]


# --- P1-9.4: Task detail page platform provisioning dropdown shows instance + access ---


def test_task_detail_platform_dropdown_includes_instance_and_access_names(client, config):
    login(client)
    store = Store(config.data_dir)
    cred_id = store.add_cred("cred", "https", username="bot", secret="tok")
    inst_a = store.add_instance("Forgejo-Prod", "forgejo", "https://prod.example")
    inst_b = store.add_instance("Gitea-DR", "gitea", "https://dr.example")
    store.add_access(inst_a, "shared-ops", cred_id)
    store.add_access(inst_b, "shared-ops", cred_id)
    task_id = store.add_task("demo", "https://source.example/demo.git")

    html = client.get(f"/tasks/{task_id}").text
    assert "Forgejo-Prod · shared-ops" in html
    assert "Gitea-DR · shared-ops" in html


# --- P1-9.5: Platform-managed task edit disables source_url/source_cred_id & preserves values ---


@pytest.mark.parametrize(
    "lang,notice_fragment",
    [
        ("en", "managed by the platform access"),
        ("zh_CN", "所属平台接入自动管理"),
    ],
)
def test_managed_task_edit_ui_is_readonly_and_post_preserves_source_fields(
    client, config, lang, notice_fragment
):
    login(client)
    store = Store(config.data_dir)
    plat_cred = store.add_cred("plat-cred", "https", username="bot", secret="tok1")
    other_cred = store.add_cred("other-cred", "https", username="other", secret="tok2")
    inst_id = store.add_instance("Forgejo", "forgejo", "https://git.example")
    access_id = store.add_access(inst_id, "team", plat_cred)
    task_id = store.add_task(
        "team/app",
        "https://git.example/team/app.git",
        source_cred_id=None,
        instance_id=inst_id,
        access_id=access_id,
        platform_repo_id=42,
    )

    client.cookies.set(LANG_COOKIE, lang)
    detail = client.get(f"/tasks/{task_id}").text
    assert notice_fragment in detail
    assert re.search(r'<input[^>]+id="task-url"[^>]+disabled', detail)
    assert re.search(r'<select[^>]+id="task-cred"[^>]+disabled', detail)
    assert f'<option value="{plat_cred}" selected>' in detail

    # POST with forged/empty source_url & source_cred_id must preserve existing values
    resp = client.post(
        f"/tasks/{task_id}/edit",
        data={
            "csrf": csrf_of(client, f"/tasks/{task_id}"),
            "name": "team/app-renamed",
            "source_url": "https://evil.example/hijack.git",
            "source_cred_id": str(other_cred),
            "interval_minutes": "120",
            "enabled": "0",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    updated = store.get_task(task_id)
    assert updated["name"] == "team/app-renamed"
    assert updated["interval_minutes"] == 120
    assert updated["enabled"] == 0
    assert updated["source_url"] == "https://git.example/team/app.git"
    assert updated["source_cred_id"] is None

    # Validation error keeps edit open and shows real source_url + access credential disabled
    bad = client.post(
        f"/tasks/{task_id}/edit",
        data={
            "csrf": csrf_of(client, f"/tasks/{task_id}"),
            "name": "",
            "interval_minutes": "120",
        },
    )
    assert bad.status_code == 400
    assert "https://git.example/team/app.git" in bad.text
    assert re.search(r'<input[^>]+id="task-url"[^>]+disabled', bad.text)


# --- P1-9.6: Overview status logic (disabled/excluded != attention) & alert pill colors ---


def test_overview_disabled_and_excluded_tasks_not_attention_and_alert_pills_styled(client, config):
    login(client)
    store = Store(config.data_dir)
    cred_id = store.add_cred("plat", "https", username="admin", secret="tok")
    inst_id = store.add_instance("Forgejo", "forgejo", "https://git.example")
    access_id = store.add_access(inst_id, "team", cred_id)

    disabled_id = store.add_task(
        "disabled-no-target",
        "https://source.example/disabled.git",
        enabled=False,
    )
    excluded_id = store.add_task(
        "excluded-repo",
        "https://git.example/org/excluded.git",
        enabled=False,
        instance_id=inst_id,
        access_id=access_id,
        platform_repo_id=7,
        managed_state="excluded",
    )
    healthy_id = store.add_task("healthy-repo", "https://source.example/healthy.git")
    t_ok = store.add_target(
        healthy_id, "nas", "https://backup.example/m.git", "https://backup.example/h.git"
    )
    store.set_bound(t_ok, True)
    store.set_target_status(t_ok, "ok", "ok")
    store.finish_task(healthy_id, "ok")

    for status in ("audit_alert", "conflict", "delete_guard", "busy", "failed"):
        run_id = store.start_run(healthy_id)
        store.finish_run(run_id, status, f"status={status}")

    disabled_summary = task_summary(store, store.get_task(disabled_id))
    excluded_summary = task_summary(store, store.get_task(excluded_id))
    assert disabled_summary["attention"] is False
    assert disabled_summary["status_key"] == "disabled"
    assert excluded_summary["attention"] is False
    assert excluded_summary["status_key"] == "excluded"

    ctx = _overview_context(SimpleNamespace(app=client.app), store)
    assert ctx["attention"] == []
    assert ctx["stats"]["attention"] == 0
    assert ctx["stats"]["healthy"] == 1
    assert ctx["stats"]["disabled"] == 2

    overview_html = client.get("/").text
    for cls in (".audit_alert", ".conflict", ".delete_guard", ".busy", ".failed", ".error"):
        assert cls in overview_html
    assert 'class="pill failed"' in overview_html


# --- P2-2: SCP-style clone_dir derivation ---


@pytest.mark.parametrize(
    ("history_url", "expected_dir"),
    [
        ("git@host:repo.git", "repo"),
        ("git@host:org/repo.git", "repo"),
        ("git@host:repo", "repo"),
        ("git@host:org/sub/repo.git/", "repo"),
        ("https://git.example.com:8443/org/repo.git", "repo"),
        ("ssh://git@host:2222/org/repo.git", "repo"),
        ("/data/mirrors/my-history.git", "my-history"),
    ],
)
def test_p2_2_scp_and_url_clone_dir_derivation(history_url, expected_dir):
    assert _clone_dir(history_url) == expected_dir
    event_view = _event_view(
        {"id": 1, "preserved_ref": "refs/preserved/1"},
        [{"history_url": history_url}],
    )
    assert event_view["commands"][0]["clone_dir"] == expected_dir


def test_task_detail_renders_scp_clone_dir_without_host_prefix(client, config):
    login(client)
    store = Store(config.data_dir)
    task_id = store.add_task("scp-demo", "https://source.example/demo.git")
    store.add_target(
        task_id,
        "ssh-nas",
        "git@backup.lan:demo.git",
        "git@backup.lan:demo-history.git",
    )
    store.add_event(task_id, "refs/heads/main", "force", "aaaa", "bbbb", "refs/preserved/1")
    html = client.get(f"/tasks/{task_id}").text
    assert "cd demo-history\n" in html
    assert "cd git@backup.lan" not in html


# --- P2-4: UI polish ---


def test_p2_4_details_auto_expand_on_form_errors(client, config):
    login(client)
    store = Store(config.data_dir)
    cred_id = store.add_cred("plat", "https", username="admin", secret="tok")
    inst1 = store.add_instance("Inst-One", "forgejo", "https://one.example")
    inst2 = store.add_instance("Inst-Two", "gitea", "https://two.example")
    task_id = store.add_task("task-one", "https://source.example/one.git")

    # Normal GET keeps add-target closed
    detail_ok = client.get(f"/tasks/{task_id}").text
    add_section = detail_ok.split('id="add-target"', 1)[1]
    assert "<details open>" not in add_section

    # Target creation error opens #add-target <details open>
    bad_target = client.post(
        f"/tasks/{task_id}/targets",
        data={
            "csrf": csrf_of(client, f"/tasks/{task_id}"),
            "name": "bad",
            "main_url": "ext::bad",
            "history_url": "https://b.example/h.git",
        },
    )
    assert bad_target.status_code == 400
    assert "<details open>" in bad_target.text.split('id="add-target"', 1)[1]

    # Target provision error opens #add-target <details open>
    bad_prov = client.post(
        f"/tasks/{task_id}/targets/provision",
        data={
            "csrf": csrf_of(client, f"/tasks/{task_id}"),
            "access_id": "999999",
            "owner": "ops",
            "name": "repo",
        },
    )
    assert bad_prov.status_code == 400
    assert "<details open>" in bad_prov.text.split('id="add-target"', 1)[1]

    # Instance creation error opens .instance-add <details open>
    bad_inst = client.post(
        "/instances",
        data={
            "csrf": csrf_of(client, "/platforms"),
            "name": "Broken",
            "kind": "gitea",
            "base_url": "ftp://invalid",
        },
    )
    assert bad_inst.status_code == 400
    assert '<details class="card instance-add" open>' in bad_inst.text

    # Access creation error opens .access-add <details open> ONLY for the targeted instance
    bad_access = client.post(
        "/accesses",
        data={
            "csrf": csrf_of(client, "/platforms"),
            "instance_id": str(inst2),
            "name": "kept-access-name",
            "cred_id": str(cred_id),
            "discover_interval_minutes": "0",
        },
    )
    assert bad_access.status_code == 400
    assert bad_access.text.count('<details class="access-add" open>') == 1
    part1, part2 = bad_access.text.split("Inst-Two", 1)
    assert '<details class="access-add" open>' not in part1
    assert '<details class="access-add" open>' in part2
    assert 'value="kept-access-name"' in part2
    assert inst1 != inst2


def test_p2_4_delete_confirmations_and_cred_single_form_type_toggle(client, config):
    login(client)
    store = Store(config.data_dir)
    cred_id = store.add_cred("plat", "https", username="admin", secret="tok")
    unused_cred = store.add_cred("unused", "https", username="u", secret="s")
    inst_id = store.add_instance("Forgejo", "forgejo", "https://git.example")
    access_id = store.add_access(inst_id, "team", cred_id)

    platforms_html = client.get("/platforms").text
    assert f'action="/accesses/{access_id}/delete"' in platforms_html
    assert 'class="confirm-delete"' in platforms_html

    creds_html = client.get("/creds").text
    assert f'action="/creds/{unused_cred}/delete"' in creds_html
    assert 'class="confirm-delete"' in creds_html
    # Single form with CSS radio toggle preserves Name / Username in-place without reload
    assert 'class="form cred-create-form"' in creds_html
    assert 'type="radio" name="kind" value="https"' in creds_html
    assert 'type="radio" name="kind" value="ssh"' in creds_html
    assert ".cred-create-form:has(" in creds_html


def test_p2_4_logs_categories_and_language_switcher_preserves_query_params(client, config):
    login(client)
    store = Store(config.data_dir)
    cred_id = store.add_cred("plat", "https", username="admin", secret="tok")
    inst_id = store.add_instance("Forgejo", "forgejo", "https://git.example")
    access_id = store.add_access(inst_id, "team", cred_id)

    # Logs page has discovery and logout categories in both languages
    for lang, expected_logout, expected_discovery in (
        ("en", "Sign-out", "Discovery"),
        ("zh_CN", "退出登录", "平台发现"),
    ):
        client.cookies.set(LANG_COOKIE, lang)
        logs_html = client.get("/logs?tab=activity&category=discovery").text
        assert f'<option value="logout">{expected_logout}</option>' in logs_html
        assert f'<option value="discovery" selected>{expected_discovery}</option>' in logs_html

    # Language switcher embeds full path + query string and redirects back with query intact
    client.cookies.set(LANG_COOKIE, "en")
    target_path = f"/platforms?instance_id={inst_id}&access_id={access_id}"
    plat_resp = client.get(target_path)
    next_raw = re.search(r'name="next" value="([^"]+)"', plat_resp.text).group(1)
    next_val = next_raw.replace("&amp;", "&")
    assert next_val == target_path
    lang_csrf = re.search(
        r'action="/lang"[^>]*>\s*<input type="hidden" name="csrf" value="([^"]+)"',
        plat_resp.text,
    ).group(1)

    switched = client.post(
        "/lang",
        data={"csrf": lang_csrf, "lang": "zh_CN", "next": next_val},
        follow_redirects=False,
    )
    assert switched.status_code == 303
    assert switched.headers["location"] == target_path


def test_p2_4_all_templates_have_zero_inline_scripts_or_event_handlers_and_strict_csp(
    client, config
):
    templates_dir = Path(__file__).resolve().parent.parent / "gitwarden" / "templates"
    for tpl in templates_dir.glob("*.html"):
        text = tpl.read_text(encoding="utf-8")
        assert "<script" not in text.lower(), tpl.name
        assert not re.search(r"\son[a-z]+\s*=", text, re.I), tpl.name

    login(client)
    for path in ("/", "/tasks", "/tasks/new", "/logs", "/platforms", "/creds"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "script-src 'none'" in resp.headers["content-security-policy"]
        assert "<script" not in resp.text.lower()
        assert not re.search(r"\son[a-z]+\s*=", resp.text, re.I)
