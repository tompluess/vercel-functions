"""
Sync Moco work-log activities from a source account to a target account.

Triggered by a Moco webhook (Activity:create) on the source account, this
function verifies the signature, filters by user, maps the source project/task
onto the target account by name (with configured defaults as fallback), and
creates a matching activity on the target account.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler
from urllib import error as urlerror
from urllib import request as urlrequest

WEBHOOK_SECRET = os.environ.get("MOCO_WEBHOOK_SECRET", "")
SOURCE_ACCOUNT_URL = os.environ.get("MOCO_SOURCE_ACCOUNT_URL", "")
USER_ID_FILTER = os.environ.get("MOCO_USER_ID_FILTER", "")
TARGET_SUBDOMAIN = os.environ.get("MOCO_TARGET_SUBDOMAIN", "")
TARGET_API_KEY = os.environ.get("MOCO_TARGET_API_KEY", "")
TARGET_COMPANY_ID = os.environ.get("MOCO_TARGET_COMPANY_ID", "")
DEFAULT_PROJECT_ID = os.environ.get("MOCO_TARGET_DEFAULT_PROJECT_ID", "")
DEFAULT_TASK_ID = os.environ.get("MOCO_TARGET_DEFAULT_TASK_ID", "")

MAX_BODY_BYTES = 64 * 1024
TIMESTAMP_WINDOW_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 10

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if body is not None:
            self.wfile.write(json.dumps(body).encode())

    def do_POST(self) -> None:
        if not all([
            WEBHOOK_SECRET, SOURCE_ACCOUNT_URL, USER_ID_FILTER,
            TARGET_SUBDOMAIN, TARGET_API_KEY, TARGET_COMPANY_ID,
            DEFAULT_PROJECT_ID, DEFAULT_TASK_ID,
        ]):
            logger.error("missing required env vars")
            return self._reply(500, {"error": "server_misconfigured"})

        length = int(self.headers.get("content-length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return self._reply(413, {"error": "invalid_content_length"})
        raw = self.rfile.read(length)

        sig = self.headers.get("x-moco-signature", "")
        expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not sig or not hmac.compare_digest(expected, sig):
            logger.warning("signature mismatch")
            return self._reply(401, {"error": "invalid_signature"})

        ts_header = self.headers.get("x-moco-timestamp", "")
        try:
            ts_ms = int(ts_header)
        except ValueError:
            return self._reply(400, {"error": "invalid_timestamp"})
        if abs(int(time.time() * 1000) - ts_ms) > TIMESTAMP_WINDOW_SECONDS * 1000:
            logger.warning("timestamp outside window: %s", ts_header)
            return self._reply(401, {"error": "timestamp_out_of_window"})

        if self.headers.get("x-moco-account-url") != SOURCE_ACCOUNT_URL:
            logger.warning("wrong source account")
            return self._reply(401, {"error": "wrong_source_account"})
        if self.headers.get("x-moco-event") != "create":
            return self._reply(200, {"skipped": "not_create_event"})
        if self.headers.get("x-moco-target") != "Activity":
            return self._reply(200, {"skipped": "not_activity_target"})

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return self._reply(400, {"error": "invalid_json"})

        user_id = (body.get("user") or {}).get("id")
        if str(user_id) != USER_ID_FILTER:
            return self._reply(200, {"skipped": "user_filter"})

        try:
            project_id, task_id = self._map_project_and_task(body)
        except (urlerror.HTTPError, urlerror.URLError) as e:
            logger.exception("fetching target projects failed")
            return self._reply(502, {"error": "target_fetch_failed", "detail": str(e)})

        payload = {
            "date": body.get("date"),
            "description": body.get("description") or "",
            "project_id": project_id,
            "task_id": task_id,
            "seconds": body.get("seconds"),
            "remote_service": body.get("remote_service") or "",
            "remote_id": body.get("remote_id") or "",
            "remote_url": body.get("remote_url") or "",
            "tag": body.get("tag") or "",
        }
        try:
            created = self._post_activity(payload)
        except urlerror.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            logger.error("target POST failed: %s %s", e.code, err_body)
            return self._reply(502, {"error": "target_post_failed", "status": e.code, "body": err_body})
        except urlerror.URLError as e:
            logger.error("target unreachable: %s", e)
            return self._reply(502, {"error": "target_unreachable"})

        logger.info("created target activity id=%s", created.get("id"))
        return self._reply(200, {"ok": True, "created_id": created.get("id"),
                                  "project_id": project_id, "task_id": task_id})

    def _map_project_and_task(self, source_body: dict) -> tuple[int, int]:
        url = (f"https://{TARGET_SUBDOMAIN}.mocoapp.com/api/v1/projects"
               f"?company_id={TARGET_COMPANY_ID}")
        req = urlrequest.Request(url, headers={
            "Authorization": f"Token token={TARGET_API_KEY}",
            "Accept": "application/json",
        })
        with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            projects = json.loads(resp.read())

        project_name = (source_body.get("project") or {}).get("name")
        task_name = (source_body.get("task") or {}).get("name")
        default_project_id = int(DEFAULT_PROJECT_ID)
        default_task_id = int(DEFAULT_TASK_ID)

        project = next((p for p in projects if p.get("name") == project_name), None)
        if project is None:
            project = next((p for p in projects if p.get("id") == default_project_id), None)

        project_id = project["id"] if project else default_project_id
        task_id = default_task_id
        if project:
            task = next((t for t in project.get("tasks") or []
                          if t.get("name") == task_name), None)
            if task:
                task_id = task["id"]
        return project_id, task_id

    def _post_activity(self, payload: dict) -> dict:
        url = f"https://{TARGET_SUBDOMAIN}.mocoapp.com/api/v1/activities"
        req = urlrequest.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={
                "Authorization": f"Token token={TARGET_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())
