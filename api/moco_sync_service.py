"""MocoSyncService — replicates a Moco Activity into a target Moco account."""

import json
from typing import Any
from urllib import request as urlrequest


class MocoSyncService:
    """Replicates a Moco Activity from a source account into a target account.

    Maps the source project/task onto the target account by name; falls back
    to configured defaults if no matching project or task exists on the target.
    Tracks the source-target link statelessly via the target activity's
    `remote_id`, encoded as `{source_account_url}:{source.id}`. Moco's
    `remote_service` field is enum-validated server-side (github / trello /
    jira / …), so we cannot use it to namespace and leave it blank.
    """

    HTTP_TIMEOUT_SECONDS = 10

    def __init__(self, *, target_subdomain: str, target_api_key: str,
                 target_company_id: str, default_project_id: int,
                 default_task_id: int, source_account_url: str):
        self._base_url = f"https://{target_subdomain}.mocoapp.com/api/v1"
        self._auth_headers = {
            "Authorization": f"Token token={target_api_key}",
            "Accept": "application/json",
        }
        self._company_id = target_company_id
        self._default_project_id = default_project_id
        self._default_task_id = default_task_id
        self._source_account_url = source_account_url

    def sync_create(self, source: dict) -> dict[str, Any]:
        project_id, task_id = self._resolve_project_and_task(source)
        payload = self._build_payload(source, project_id, task_id)
        created = self._post_activity(payload)
        return {"created_id": created.get("id"),
                "project_id": project_id, "task_id": task_id}

    def sync_update(self, source: dict) -> dict[str, Any]:
        """Find the existing target activity by remote_id and PUT the new payload.
        If no target activity is found, fall through to create (upsert)."""
        existing = self._find_target_by_remote_id(
            date=source.get("date") or "",
            namespaced_id=self._namespaced_id(source),
        )
        project_id, task_id = self._resolve_project_and_task(source)
        payload = self._build_payload(source, project_id, task_id)
        if existing is None:
            created = self._post_activity(payload)
            return {"created_id": created.get("id"),
                    "project_id": project_id, "task_id": task_id,
                    "upserted": True}
        updated = self._put_activity(existing["id"], payload)
        return {"updated_id": updated.get("id"),
                "project_id": project_id, "task_id": task_id}

    def sync_delete(self, source: dict) -> dict[str, Any]:
        """Find the existing target activity by remote_id and DELETE it.
        If no target activity is found, the desired state already holds —
        return a skip marker so the caller sees an explicit no-op."""
        existing = self._find_target_by_remote_id(
            date=source.get("date") or "",
            namespaced_id=self._namespaced_id(source),
        )
        if existing is None:
            return {"skipped": "target_not_found"}
        self._delete_activity(existing["id"])
        return {"deleted_id": existing["id"]}

    def _namespaced_id(self, source: dict) -> str:
        return f"{self._source_account_url}:{source.get('id') or ''}"

    def _resolve_project_and_task(self, source: dict) -> tuple[int, int]:
        projects = self._get_projects()
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

    def _get_projects(self) -> list[dict]:
        url = f"{self._base_url}/projects?company_id={self._company_id}"
        req = urlrequest.Request(url, headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    def _find_target_by_remote_id(self, *, date: str, namespaced_id: str) -> dict | None:
        """Locate a previously-synced target activity by namespaced remote_id.

        Scans activities for the given date — Moco's `/activities` listing is
        already scoped to the API token's user, so no further user filtering
        is needed.
        """
        url = f"{self._base_url}/activities?from={date}&to={date}"
        req = urlrequest.Request(url, headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            activities = json.loads(resp.read())
        return next(
            (a for a in activities
             if str(a.get("remote_id") or "") == namespaced_id),
            None,
        )

    def _post_activity(self, payload: dict) -> dict:
        url = f"{self._base_url}/activities"
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        req = urlrequest.Request(url, data=json.dumps(payload).encode(),
                                 method="POST", headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    def _put_activity(self, activity_id: int, payload: dict) -> dict:
        url = f"{self._base_url}/activities/{activity_id}"
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        req = urlrequest.Request(url, data=json.dumps(payload).encode(),
                                 method="PUT", headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    def _delete_activity(self, activity_id: int) -> None:
        url = f"{self._base_url}/activities/{activity_id}"
        req = urlrequest.Request(url, method="DELETE", headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS):
            pass
