"""MocoSyncService — replicates a Moco Activity into a target Moco account."""

import datetime as dt
from typing import Any

from api.moco_api import MocoAPI


class TargetNotFoundError(LookupError):
    """Raised by sync_delete when no target activity matches the source's
    namespaced remote_id. Caller decides how to surface it (e.g. HTTP 404)."""


class MocoSyncService:
    """Replicates a Moco Activity from a source account into a target account.

    Maps the source project/task onto the target account by name; falls back
    to configured defaults if no matching project or task exists on the target.
    Tracks the source-target link statelessly via the target activity's
    `remote_id`, encoded as `{source_account_url}:{source.id}`. Moco's
    `remote_service` field is enum-validated server-side (github / trello /
    jira / …), so we cannot use it to namespace and leave it blank.

    HTTP transport is delegated to a `MocoAPI` collaborator so this class
    contains only business logic and can be unit-tested with a fake.
    """

    # Moco's delete webhook ships only {id}, no date — scan this many days
    # back from today when looking up the target activity. Kept tight to
    # stay within a single Moco /activities page (default 100): wider
    # windows risk silent misses on page 2+. The trade-off is that deletes
    # of activities older than this won't find their target — acceptable
    # because old time entries are rarely deleted.
    DATELESS_LOOKUP_DAYS = 14

    def __init__(self, *, api: MocoAPI, default_project_id: int,
                 default_task_id: int, source_account_url: str):
        self._api = api
        self._default_project_id = default_project_id
        self._default_task_id = default_task_id
        self._source_account_url = source_account_url

    def sync_create(self, source: dict) -> dict[str, Any]:
        project_id, task_id = self._resolve_project_and_task(source)
        payload = self._build_payload(source, project_id, task_id)
        created = self._api.create_activity(payload)
        return {"created_id": created.get("id"),
                "project_id": project_id, "task_id": task_id}

    def sync_update(self, source: dict) -> dict[str, Any]:
        """Find the existing target activity by remote_id and PUT the new payload.
        If no target activity is found, fall through to create (upsert)."""
        existing = self._find_target_by_remote_id(
            date=source.get("date"),
            namespaced_id=self._namespaced_id(source),
        )
        project_id, task_id = self._resolve_project_and_task(source)
        payload = self._build_payload(source, project_id, task_id)
        if existing is None:
            created = self._api.create_activity(payload)
            return {"created_id": created.get("id"),
                    "project_id": project_id, "task_id": task_id,
                    "upserted": True}
        updated = self._api.update_activity(existing["id"], payload)
        return {"updated_id": updated.get("id"),
                "project_id": project_id, "task_id": task_id}

    def sync_delete(self, source: dict) -> dict[str, Any]:
        """Find the existing target activity by remote_id and DELETE it.
        Raises TargetNotFoundError if no matching activity is found in the
        configured lookup window."""
        namespaced_id = self._namespaced_id(source)
        existing = self._find_target_by_remote_id(
            date=source.get("date"),
            namespaced_id=namespaced_id,
        )
        if existing is None:
            raise TargetNotFoundError(namespaced_id)
        self._api.delete_activity(existing["id"])
        return {"deleted_id": existing["id"]}

    def _namespaced_id(self, source: dict) -> str:
        return f"{self._source_account_url}:{source.get('id') or ''}"

    def _resolve_project_and_task(self, source: dict) -> tuple[int, int]:
        projects = self._api.list_projects()
        project_name = (source.get("project") or {}).get("name")
        task_name = (source.get("task") or {}).get("name")

        project = next((p for p in projects if p.get("name") == project_name), None)
        if project is None:
            project = next((p for p in projects
                            if p.get("id") == self._default_project_id), None)

        project_id = project["id"] if project else self._default_project_id
        task_id = self._default_task_id
        if project:
            task = next((t for t in project.get("tasks") or []
                         if t.get("name") == task_name), None)
            if task:
                task_id = task["id"]
        return project_id, task_id

    def _build_payload(self, source: dict, project_id: int,
                       task_id: int) -> dict[str, Any]:
        return {
            "date": source.get("date"),
            "description": source.get("description") or "",
            "project_id": project_id,
            "task_id": task_id,
            "seconds": source.get("seconds"),
            # remote_service is enum-validated server-side by Moco — leave blank.
            "remote_service": "",
            "remote_id": self._namespaced_id(source),
            "remote_url": source.get("remote_url") or "",
            "tag": source.get("tag") or "",
        }

    def _find_target_by_remote_id(self, *, date: str | None,
                                  namespaced_id: str) -> dict | None:
        """Locate a previously-synced target activity by namespaced remote_id.

        When the source webhook carries a date (create/update), the lookup is
        scoped to that single day. When it doesn't (delete: body is just
        `{id}`), fall back to a `DATELESS_LOOKUP_DAYS`-window ending today.
        Moco's `/activities` listing is already scoped to the API token's
        user, so no further user filtering is needed.
        """
        if date:
            date_from = date_to = date
        else:
            today = dt.date.today()
            date_from = (today - dt.timedelta(days=self.DATELESS_LOOKUP_DAYS)).isoformat()
            date_to = today.isoformat()
        activities = self._api.list_activities(date_from=date_from, date_to=date_to)
        return next(
            (a for a in activities
             if str(a.get("remote_id") or "") == namespaced_id),
            None,
        )
