"""MocoAPI — typed wrapper around the Moco REST API used by the sync service."""

import json
import logging
from urllib import request as urlrequest

logger = logging.getLogger("moco_sync")


class MocoAPI:
    """Thin HTTP wrapper around the Moco Projects/Activities endpoints.

    Owns base-URL construction, auth headers, and urllib transport so callers
    can treat it as a typed Python interface and substitute a fake in tests
    without monkeypatching urlopen.
    """

    HTTP_TIMEOUT_SECONDS = 10

    def __init__(self, *, subdomain: str, api_key: str, company_id: str):
        self._base_url = f"https://{subdomain}.mocoapp.com/api/v1"
        self._auth_headers = {
            "Authorization": f"Token token={api_key}",
            "Accept": "application/json",
        }
        self._company_id = company_id

    def list_projects(self) -> list[dict]:
        url = f"{self._base_url}/projects?company_id={self._company_id}"
        req = urlrequest.Request(url, headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    def list_activities(self, *, date_from: str, date_to: str) -> list[dict]:
        """Return activities in [date_from, date_to].

        Logs X-Total alongside the returned count so pagination boundaries are
        visible in logs — we don't follow pagination, so `returned < X-Total`
        is a silent-miss warning for callers.
        """
        url = f"{self._base_url}/activities?from={date_from}&to={date_to}"
        req = urlrequest.Request(url, headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            total = resp.headers.get("X-Total")
            activities = json.loads(resp.read())
        logger.info("activities lookup: from=%s to=%s X-Total=%s returned=%s",
                    date_from, date_to, total, len(activities))
        return activities

    def create_activity(self, payload: dict) -> dict:
        url = f"{self._base_url}/activities"
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        req = urlrequest.Request(url, data=json.dumps(payload).encode(),
                                 method="POST", headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    def update_activity(self, activity_id: int, payload: dict) -> dict:
        url = f"{self._base_url}/activities/{activity_id}"
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        req = urlrequest.Request(url, data=json.dumps(payload).encode(),
                                 method="PUT", headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    def delete_activity(self, activity_id: int) -> None:
        url = f"{self._base_url}/activities/{activity_id}"
        req = urlrequest.Request(url, method="DELETE", headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS):
            pass
