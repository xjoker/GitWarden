"""总览和任务列表页面。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from gitwarden.db import Store
from gitwarden.scheduler import effective_interval
from gitwarden.web import CsrfToken, _page, _store

_PAGE_SIZE = 20
_UNSET = object()

_STATUS_LABELS = {
    "healthy": "tasks.status.healthy",
    "incomplete": "tasks.status.incomplete",
    "needs_attention": "tasks.status.needs_attention",
    "failing": "tasks.status.failing",
    "not_visible": "tasks.status.not_visible",
    "excluded": "tasks.status.excluded",
    "source_gone": "tasks.status.source_gone",
    "disabled": "tasks.status.disabled",
}
_STATUS_CLASSES = {
    "healthy": "ok",
    "incomplete": "warn",
    "needs_attention": "warn",
    "failing": "error",
    "not_visible": "",
    "excluded": "excluded",
    "source_gone": "error",
    "disabled": "",
}
_EVENT_LABELS = {
    "create": "common.kind.create",
    "fast-forward": "common.kind.fast_forward",
    "force": "common.kind.force",
    "tag-rewrite": "common.kind.tag_rewrite",
    "delete": "common.kind.delete",
}
_RUN_LABELS = {
    "ok": "common.status.success",
    "failed": "common.status.failed",
    "busy": "common.status.busy",
    "partial": "tasks.run.partial",
    "delete_guard": "tasks.run.delete_guard",
    "audit_alert": "common.status.audit_alert",
    "gate_blocked": "common.status.gate_blocked",
    "conflict": "common.status.conflict",
    "history_conflict": "common.status.history_conflict",
    "running": "tasks.run.running",
}


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def source_failing(task: dict[str, Any]) -> bool:
    """最近一次运行因来源采集失败（404 / 401 / 仓库不存在等）而失败。

    采集成功但所有目标都失败时也是 failed，此时目标本身已不是 ok，不受影响。
    """
    return task.get("last_status") == "failed"


def _target_summary(target: dict[str, Any], task: dict[str, Any] | None = None) -> dict[str, Any]:
    if not target.get("bound"):
        status_key = "tasks.target.unbound"
        status_class = ""
    elif target.get("last_status") == "ok" and task is not None and source_failing(task):
        status_key = "tasks.target.source_failing"
        status_class = "warn"
    elif target.get("last_status") == "ok":
        status_key = "tasks.target.healthy"
        status_class = "ok"
    elif target.get("last_status"):
        status_key = "tasks.target.failed"
        status_class = "error"
    else:
        status_key = "tasks.target.not_synced"
        status_class = "warn"
    return {
        "id": target["id"],
        "name": target["name"],
        "status_key": status_key,
        "status_class": status_class,
        "last_status": target.get("last_status"),
        "bound": bool(target.get("bound")),
    }


def _managed_summary(
    store: Store,
    task: dict[str, Any],
    *,
    accesses: dict[int, dict[str, Any]] | None = None,
    instances: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    access_id = task.get("access_id")
    if not access_id:
        return {
            "access_id": None,
            "instance_name": None,
            "access_name": None,
            "label_key": "tasks.managed.manual",
        }
    aid = int(access_id)
    access = accesses.get(aid) if accesses is not None else store.get_access(aid)
    instance = (
        (
            instances.get(int(access["instance_id"]))
            if instances is not None
            else store.get_instance(access["instance_id"])
        )
        if access
        else None
    )
    if not access or not instance:
        return {
            "access_id": aid,
            "instance_name": None,
            "access_name": None,
            "label_key": "tasks.managed.unknown",
        }
    return {
        "access_id": aid,
        "instance_name": instance["name"],
        "access_name": access["name"],
        "label_key": None,
    }


def _overdue_seconds(task: dict[str, Any], interval: float) -> float:
    now = datetime.now(UTC)
    due = _datetime(task.get("next_run_at"))
    if due is None:
        last_run = _datetime(task.get("last_run_at"))
        if last_run is None:
            return 0.0
        due = last_run + timedelta(minutes=interval)
    threshold = max(interval, 1.0) * 2 * 60
    return max(0.0, (now - due).total_seconds() - threshold)


def task_summary(
    store: Store,
    task: dict[str, Any],
    scheduler: object | None = None,
    *,
    targets_rows: Any = _UNSET,
    latest_run: Any = _UNSET,
    accesses: dict[int, dict[str, Any]] | None = None,
    instances: dict[int, dict[str, Any]] | None = None,
    preserved_count: int | None = None,
) -> dict[str, Any]:
    """汇总任务的备份、调度和管理状态。"""

    raw_targets = store.list_targets(task["id"]) if targets_rows is _UNSET else targets_rows
    targets = [_target_summary(row, task) for row in raw_targets]
    incomplete = (
        not targets
        or any(not target["bound"] or target["last_status"] != "ok" for target in targets)
        or bool(task.get("external_note"))
    )
    interval = effective_interval(task)
    if scheduler is not None and hasattr(scheduler, "effective_interval"):
        interval = float(scheduler.effective_interval(task))
    configured = float(task.get("interval_minutes") or 0)
    is_enabled = bool(task.get("enabled"))
    state = str(task.get("managed_state") or "active")
    overdue = _overdue_seconds(task, interval) if is_enabled else 0.0
    missing_targets = not targets
    target_attention = any(target["status_key"] != "tasks.target.healthy" for target in targets)
    # 仅在来源/整轮失败（last_status == 'failed'）时标记为 failing；
    # partial / delete_guard / audit_alert / busy 归为 needs_attention。
    last_status = task.get("last_status")
    source_failure = last_status == "failed"
    non_ok_status = bool(last_status and last_status != "ok")
    managed_state_attention = state in {"not_visible", "source_gone"}
    if not is_enabled or state == "excluded":
        attention = False
    else:
        attention = bool(
            overdue
            or missing_targets
            or target_attention
            or task.get("external_note")
            or source_failure
            or non_ok_status
            or managed_state_attention
        )

    if latest_run is _UNSET:
        latest_runs = store.list_runs(task["id"], limit=1)
        latest_run = latest_runs[0] if latest_runs else None
    run_running = bool(latest_run and not latest_run.get("finished_at"))
    run_status = (latest_run.get("status") if latest_run else None) or (
        "running" if run_running else None
    )

    managed = _managed_summary(store, task, accesses=accesses, instances=instances)
    if state != "active":
        status_key = state if state in _STATUS_LABELS else "needs_attention"
    elif not is_enabled:
        status_key = "disabled"
    elif source_failure:
        status_key = "failing"
    elif attention:
        status_key = "needs_attention"
    else:
        status_key = "healthy"

    durations = [float(value) for value in (task.get("recent_durations") or [])[-3:]]
    extension_kind = None
    duration_minutes = None
    failure_count = int(task.get("consecutive_failures") or 0)
    if interval > configured + 0.01:
        if failure_count > 1:
            extension_kind = "backoff"
        elif durations:
            extension_kind = "duration"
            duration_minutes = max(1, round(sum(durations) / len(durations) / 60))
        else:
            extension_kind = "duration"

    preserved = (
        store.count_preserved_events(task["id"]) if preserved_count is None else preserved_count
    )
    return {
        **task,
        "targets": targets,
        "target_count": len(targets),
        "managed": managed,
        "complete": not incomplete,
        "backup_key": "complete" if not incomplete else "incomplete",
        "attention": attention,
        "overdue": overdue > 0,
        "overdue_minutes": max(1, round(overdue / 60)) if overdue else 0,
        "status_key": status_key,
        "status_label_key": _STATUS_LABELS[status_key],
        "status_class": _STATUS_CLASSES[status_key],
        "preserved_count": preserved,
        "effective_interval_minutes": max(1, round(interval)),
        "interval_extended": extension_kind is not None,
        "interval_extension_kind": extension_kind,
        "duration_minutes": duration_minutes,
        "failure_count": failure_count,
        "latest_run": latest_run,
        "run_running": run_running,
        "run_status": run_status,
        "run_progress": latest_run.get("progress") if latest_run else None,
        "initial_backup": bool(latest_run and latest_run.get("initial_backup")),
    }


def _parse_page(raw: str | None) -> int:
    try:
        return max(1, int(raw or "1"))
    except ValueError:
        return 1


def _matches_status(row: dict[str, Any], status: str) -> bool:
    if not status or status in {"any", "all"}:
        return True
    if status == "active":
        return bool(row["enabled"] and row["managed_state"] == "active")
    if status == "disabled":
        return not row["enabled"]
    if status in {"attention", "needs_attention"}:
        return bool(row["attention"])
    return row["status_key"] == status


def _matches_managed(row: dict[str, Any], managed: str) -> bool:
    if not managed or managed in {"all", "any"}:
        return True
    if managed == "manual":
        return row["managed"]["access_id"] is None
    if managed.startswith("access:"):
        managed = managed.removeprefix("access:")
    try:
        return row["managed"]["access_id"] == int(managed)
    except ValueError:
        return False


def _filter_tasks(
    rows: list[dict[str, Any]], search: str, status: str, managed: str, backup: str
) -> list[dict[str, Any]]:
    needle = search.casefold().strip()
    return [
        row
        for row in rows
        if (
            not needle or needle in row["name"].casefold() or needle in row["source_url"].casefold()
        )
        and _matches_status(row, status.casefold())
        and _matches_managed(row, managed.casefold())
        and (not backup or backup in {"any", "all"} or row["backup_key"] == backup)
    ]


def _query_url(request: Request, page: int) -> str:
    params = [(key, value) for key, value in request.query_params.multi_items() if key != "page"]
    params.insert(0, ("page", str(page)))
    return f"/tasks?{urlencode(params)}"


def _access_options(
    store: Store,
    *,
    accesses: dict[int, dict[str, Any]] | None = None,
    instances: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    options = [{"value": "manual", "label_key": "tasks.managed.manual"}]
    access_rows = list(accesses.values()) if accesses is not None else store.list_accesses()
    for access in access_rows:
        instance = (
            instances.get(int(access["instance_id"]))
            if instances is not None
            else store.get_instance(access["instance_id"])
        )
        if not instance:
            continue
        options.append(
            {
                "value": f"access:{access['id']}",
                "label": f"{instance['name']} · {access['name']}",
            }
        )
    return options


def _format_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**event, "kind_key": _EVENT_LABELS.get(event["kind"], "common.empty")} for event in events
    ]


def _format_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **run,
            "status_key": _RUN_LABELS.get(run.get("status") or "running", "common.status.error"),
        }
        for run in runs
    ]


def _summaries_from_snapshot(
    store: Store, snapshot: dict[str, Any], scheduler: object | None
) -> list[dict[str, Any]]:
    targets_by_task = snapshot["targets_by_task"]
    latest_runs = snapshot["latest_runs"]
    accesses = snapshot["accesses"]
    instances = snapshot["instances"]
    preserved_counts = snapshot["preserved_counts"]
    return [
        task_summary(
            store,
            task,
            scheduler,
            targets_rows=targets_by_task.get(int(task["id"]), []),
            latest_run=latest_runs.get(int(task["id"])),
            accesses=accesses,
            instances=instances,
            preserved_count=preserved_counts.get(int(task["id"]), 0),
        )
        for task in snapshot["tasks"]
    ]


def _overview_context(request: Request, store: Store) -> dict[str, Any]:
    scheduler = request.app.state.scheduler
    snapshot = store.overview_snapshot(include_feed=True, feed_limit=5)
    summaries = _summaries_from_snapshot(store, snapshot, scheduler)
    attention = [row for row in summaries if row["attention"]]
    return {
        "tasks": summaries[:10],
        "attention": attention[:10],
        "events": _format_events(snapshot["recent_events"]),
        "runs": _format_runs(snapshot["recent_runs"]),
        "stats": {
            "active": sum(1 for row in summaries if row["enabled"]),
            "disabled": sum(1 for row in summaries if not row["enabled"]),
            "healthy": sum(
                1
                for row in summaries
                if row["enabled"] and row["complete"] and not row["attention"]
            ),
            "attention": len(attention),
            "incomplete": sum(1 for row in summaries if not row["complete"]),
        },
    }


def register(app: FastAPI) -> None:
    """Register the two page routes after the shared app helpers are initialized."""

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, csrf: CsrfToken):
        return _page(request, "index.html", csrf, **_overview_context(request, _store(request)))

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks_page(request: Request, csrf: CsrfToken):
        store = _store(request)
        query = request.query_params
        search = str(query.get("search", query.get("q", "")))
        status = str(query.get("status", ""))
        managed = str(query.get("managed", ""))
        backup = str(query.get("backup", ""))
        page = _parse_page(query.get("page"))
        snapshot = store.overview_snapshot(include_feed=False)
        rows = _summaries_from_snapshot(store, snapshot, request.app.state.scheduler)
        filtered = _filter_tasks(rows, search, status, managed, backup)
        total = len(filtered)
        pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = min(page, pages)
        start_index = (page - 1) * _PAGE_SIZE
        visible = filtered[start_index : start_index + _PAGE_SIZE]
        return _page(
            request,
            "tasks.html",
            csrf,
            rows=visible,
            total=total,
            page=page,
            pages=pages,
            page_start=start_index + 1 if visible else 0,
            page_end=start_index + len(visible),
            previous_url=_query_url(request, page - 1) if page > 1 else None,
            next_url=_query_url(request, page + 1) if page < pages else None,
            search=search,
            selected_status=status,
            managed=managed,
            backup=backup,
            managed_options=_access_options(
                store, accesses=snapshot["accesses"], instances=snapshot["instances"]
            ),
        )

    return app
