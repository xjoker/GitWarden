import asyncio
import inspect
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gitwarden.config import Config
from gitwarden.db import Store
from gitwarden.web import (
    SESSION_ABSOLUTE_TTL,
    SESSION_COOKIE,
    SESSION_IDLE_TTL,
    SESSION_MAX_COUNT,
    LoginRateLimiter,
    create_app,
)

PASSWORD = "pw-test-123"


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "data", admin_password=PASSWORD)


def _login_route_endpoint(app):
    for route in app.routes:
        if getattr(route, "path", None) == "/login" and "POST" in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError("POST /login route not found")


def test_post_login_is_async_and_never_calls_blocking_time_sleep(config, monkeypatch):
    app = create_app(config, start_scheduler=False)
    endpoint = _login_route_endpoint(app)
    assert inspect.iscoroutinefunction(endpoint)

    slept_async = []

    async def fake_async_sleep(seconds):
        slept_async.append(seconds)

    def forbid_blocking_sleep(_seconds):
        raise AssertionError("blocking time.sleep must not be called in POST /login")

    monkeypatch.setattr(asyncio, "sleep", fake_async_sleep)
    monkeypatch.setattr(time, "sleep", forbid_blocking_sleep)

    with TestClient(app) as client:
        resp = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert resp.status_code == 401
        assert slept_async == [1]


def test_login_rate_limiter_per_ip_and_429_response(config, monkeypatch):
    now_box = [100.0]

    async def noop_sleep(_seconds):
        raise AssertionError("rate-limited 429 must return immediately without sleeping")

    app = create_app(config, start_scheduler=False)
    app.state.login_rate_limiter = LoginRateLimiter(
        ip_limit=3,
        global_limit=10,
        window_seconds=60.0,
        clock=lambda: now_box[0],
    )

    with TestClient(app) as client:
        for _ in range(3):
            resp = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
            assert resp.status_code == 303

        monkeypatch.setattr(asyncio, "sleep", noop_sleep)
        client.cookies.set("gw_lang", "en")
        blocked = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers
        assert int(blocked.headers["Retry-After"]) >= 1
        assert "Too many login attempts" in blocked.text

        client.cookies.set("gw_lang", "zh_CN")
        blocked_zh = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert blocked_zh.status_code == 429
        assert "登录尝试过于频繁" in blocked_zh.text

        # Advance past the 60s sliding window -> allowed again.
        now_box[0] += 61.0
        ok = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert ok.status_code == 303


def test_login_rate_limiter_global_cap_and_bounded_ip_memory():
    now_box = [10.0]
    limiter = LoginRateLimiter(
        ip_limit=5,
        global_limit=4,
        window_seconds=60.0,
        max_ips=3,
        clock=lambda: now_box[0],
    )

    # Global cap triggers across different IPs even if per-IP limit (5) is not hit.
    for i in range(4):
        allowed, retry = limiter.allow(f"10.0.0.{i}")
        assert allowed is True and retry == 0.0

    allowed, retry = limiter.allow("10.0.0.99")
    assert allowed is False
    assert retry > 0.0

    # Memory stays bounded to max_ips even under many distinct IPs after window slides.
    limiter.reset()
    limiter.global_limit = 100
    for i in range(10):
        allowed, _ = limiter.allow(f"192.0.2.{i}")
        assert allowed is True
        assert len(limiter._per_ip) <= 3


def test_session_idle_and_absolute_ttl_and_refresh(config):
    now_box = [1000.0]
    app = create_app(config, start_scheduler=False)
    app.state.session_clock = lambda: now_box[0]
    assert app.state.session_idle_ttl == SESSION_IDLE_TTL == 7200
    assert app.state.session_absolute_ttl == SESSION_ABSOLUTE_TTL == 43200

    with TestClient(app) as client:
        resp = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        token = client.cookies.get(SESSION_COOKIE)
        assert token in app.state.sessions
        entry = app.state.sessions[token]
        assert entry["created_at"] == 1000.0
        assert entry["last_seen"] == 1000.0

        # Refresh last_seen at 7000s (still under 7200s idle) -> extends idle deadline.
        now_box[0] = 8000.0
        assert client.get("/").status_code == 200
        assert app.state.sessions[token]["last_seen"] == 8000.0

        # Still alive at 15000s (7000s since last_seen, < 7200s idle, < 43200s absolute).
        now_box[0] = 15000.0
        assert client.get("/").status_code == 200
        assert app.state.sessions[token]["last_seen"] == 15000.0

        # Advance 7200s past last_seen -> idle timeout triggers.
        now_box[0] = 15000.0 + SESSION_IDLE_TTL
        expired_get = client.get("/", follow_redirects=False)
        assert expired_get.status_code == 303
        assert expired_get.headers["location"] == "/login"
        assert token not in app.state.sessions

        # Login again and verify absolute TTL expires even with continuous activity.
        resp = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        token2 = client.cookies.get(SESSION_COOKIE)
        created2 = app.state.sessions[token2]["created_at"]
        for step in range(1, 7):
            now_box[0] = created2 + step * 7000.0
            assert client.get("/").status_code == 200
        now_box[0] = created2 + SESSION_ABSOLUTE_TTL
        abs_expired = client.post("/creds", data={"name": "x"}, follow_redirects=False)
        assert abs_expired.status_code == 401
        assert token2 not in app.state.sessions


def test_session_capacity_cap_evicts_oldest_and_supports_legacy_string_sessions(config):
    now_box = [100.0]
    app = create_app(config, start_scheduler=False)
    app.state.session_clock = lambda: now_box[0]
    assert app.state.session_max_count == SESSION_MAX_COUNT == 128

    # Bypass rate limiter to exercise full default 128-session cap.
    app.state.login_rate_limiter = LoginRateLimiter(ip_limit=500, global_limit=500)

    tokens = []
    with TestClient(app) as client:
        for i in range(SESSION_MAX_COUNT + 3):
            now_box[0] = 100.0 + i
            resp = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
            assert resp.status_code == 303
            tokens.append(resp.cookies.get(SESSION_COOKIE))

        assert len(app.state.sessions) == SESSION_MAX_COUNT
        # Oldest 3 sessions were evicted; newest 128 remain.
        for old_tok in tokens[:3]:
            assert old_tok not in app.state.sessions
        for new_tok in tokens[3:]:
            assert new_tok in app.state.sessions

        # Legacy string session values in app.state.sessions remain compatible.
        app.state.sessions["legacy-token"] = "legacy-csrf"
        client.cookies.set(SESSION_COOKIE, "legacy-token")
        assert client.get("/").status_code == 200


@pytest.mark.parametrize(
    "env_value,expected_secure",
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
    ],
)
def test_gw_cookie_secure_flag_on_session_cookie(config, monkeypatch, env_value, expected_secure):
    if env_value is None:
        monkeypatch.delenv("GW_COOKIE_SECURE", raising=False)
    else:
        monkeypatch.setenv("GW_COOKIE_SECURE", env_value)

    app = create_app(config, start_scheduler=False)
    base_url = "https://testserver" if expected_secure else "http://testserver"
    with TestClient(app, base_url=base_url) as client:
        resp = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        set_cookies = resp.headers.get_list("set-cookie")
        session_headers = [h for h in set_cookies if h.startswith(f"{SESSION_COOKIE}=")]
        assert len(session_headers) == 1
        cookie_header = session_headers[0].lower()
        assert "httponly" in cookie_header
        assert "samesite=strict" in cookie_header
        assert ("secure" in cookie_header) is expected_secure
        assert client.get("/").status_code == 200


def test_security_headers_and_strict_csp_without_inline_handlers(config):
    app = create_app(config, start_scheduler=False)
    with TestClient(app) as client:
        for resp in (
            client.get("/login"),
            client.get("/health"),
            client.get("/favicon.svg"),
            client.get("/", follow_redirects=False),
        ):
            assert resp.headers["x-frame-options"] == "DENY"
            assert resp.headers["x-content-type-options"] == "nosniff"
            assert resp.headers["referrer-policy"] == "same-origin"
            csp = resp.headers["content-security-policy"]
            assert "default-src 'self'" in csp
            assert "script-src 'none'" in csp
            assert "style-src 'self' 'unsafe-inline'" in csp
            assert "frame-ancestors 'none'" in csp

        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        task_id = Store(config.data_dir).add_task("demo", "https://example.com/org/demo.git")
        detail = client.get(f"/tasks/{task_id}")
        assert detail.status_code == 200
        assert not re.search(r"\son[a-z]+\s*=", detail.text, re.I)
        assert "<script" not in detail.text.lower()
        assert 'href="#edit"' in detail.text
        assert 'href="#add-target"' in detail.text


def test_lifespan_cleans_up_stale_work_entries_and_ignores_missing(config):
    # First: startup with no work/ directory should succeed cleanly.
    app_no_work = create_app(config, start_scheduler=False)
    with TestClient(app_no_work):
        assert not (config.data_dir / "work").exists()

    # Create stale ssh-* and bundle-* dirs/files + an unrelated directory.
    work_dir = config.data_dir / "work"
    ssh_dir = work_dir / "ssh-crashed-1"
    ssh_dir.mkdir(parents=True)
    key_file = ssh_dir / "id"
    key_file.write_text("SECRET-KEY", encoding="utf-8")
    key_file.chmod(0o400)

    bundle_dir = work_dir / "bundle-crashed-2"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "restore.bundle").write_bytes(b"bundle-bytes")

    stale_file = work_dir / "ssh-leftover-file"
    stale_file.write_text("leftover", encoding="utf-8")

    keep_dir = work_dir / "keep-other"
    keep_dir.mkdir(parents=True)
    (keep_dir / "note.txt").write_text("keep", encoding="utf-8")

    app = create_app(config, start_scheduler=False)
    with TestClient(app):
        assert not ssh_dir.exists()
        assert not bundle_dir.exists()
        assert not stale_file.exists()
        assert (keep_dir / "note.txt").read_text(encoding="utf-8") == "keep"


def test_i18n_keys_stay_identical():
    i18n_dir = Path(__file__).resolve().parent.parent / "gitwarden" / "i18n"
    en = json.loads((i18n_dir / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((i18n_dir / "zh_CN.json").read_text(encoding="utf-8"))
    assert set(en) == set(zh)
