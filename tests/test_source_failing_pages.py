"""源连续失败（404 / 401 / 不存在）时，网页任何地方都不再把目标显示为 healthy。"""

import re

import pytest
from fastapi.testclient import TestClient

from gitwarden.config import Config
from gitwarden.db import Store
from gitwarden.web import create_app

PASSWORD = "pw-source-failing-test"


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "data", admin_password=PASSWORD)


@pytest.fixture
def client(config):
    app = create_app(config, start_scheduler=False)
    with TestClient(app) as test_client:
        response = test_client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert response.status_code == 303
        yield test_client


def _seed(config, task_status):
    store = Store(config.data_dir)
    task_id = store.add_task("gone-src", "http://127.0.0.1:1/missing.git")
    target_id = store.add_target(
        task_id, "NAS", "https://backup.example/main.git", "https://backup.example/history.git"
    )
    store.set_bound(target_id, True)
    store.set_target_status(target_id, "ok", "Sync completed")
    store.finish_task(task_id, task_status)
    return task_id


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


@pytest.mark.parametrize("path", ["/", "/tasks"])
def test_lists_do_not_show_healthy_target_while_source_fails(client, config, path):
    _seed(config, "failed")
    text = _text(client.get(path).text)
    assert "NAS · healthy" not in text
    assert "NAS · source failing" in text


def test_task_detail_does_not_show_healthy_target_while_source_fails(client, config):
    task_id = _seed(config, "failed")
    text = _text(client.get(f"/tasks/{task_id}").text)
    assert " Healthy " not in text
    assert "Source failing" in text


def test_healthy_target_still_shown_when_source_ok(client, config):
    task_id = _seed(config, "ok")
    assert "NAS · healthy" in _text(client.get("/tasks").text)
    assert " Healthy " in _text(client.get(f"/tasks/{task_id}").text)


def test_delete_guard_shows_needs_attention_not_failing(client, config):
    store = Store(config.data_dir)
    task_id = store.add_task("emptied", "http://127.0.0.1:1/emptied.git")
    target_id = store.add_target(
        task_id, "NAS", "https://backup.example/main.git", "https://backup.example/history.git"
    )
    store.set_bound(target_id, True)
    store.set_target_status(target_id, "delete_guard", "held")
    run_id = store.start_run(task_id)
    store.finish_run(run_id, "delete_guard", "held")
    store.finish_task(task_id, "delete_guard")
    text = _text(client.get("/tasks").text)
    assert "NAS · healthy" not in text
    # 任务状态列是“需要处理”，不是“Failing”。
    assert "Added by hand Needs attention NAS · needs attention" in text
    logs = _text(client.get("/logs").text)
    assert "Mass deletion held" in logs
