"""Unit tests for MocoSyncService — project/task resolution and payload shape.

These inject a FakeMocoAPI directly into the service, so no HTTP transport is
involved. URL/header/X-Total concerns are covered in test_moco_api.py.
"""

import pytest

from api.moco_sync_service import MocoSyncService, TargetNotFoundError
from tests.conftest import load_fixture


DEFAULTS = dict(
    default_project_id=947156885,
    default_task_id=25339113,
    source_account_url="solar",
)


class FakeMocoAPI:
    """In-memory stand-in for api.moco_api.MocoAPI. Records every call so tests
    can assert which method was invoked with which arguments."""

    def __init__(self):
        self.projects: list[dict] = []
        self.activities_lookup: list[dict] = []
        self.next_create_response: dict = {"id": 99999999}
        self.next_update_response: dict = {"id": 88888881}
        self.calls: list[tuple] = []

    def list_projects(self) -> list[dict]:
        self.calls.append(("list_projects",))
        return self.projects

    def list_activities(self, *, date_from: str, date_to: str) -> list[dict]:
        self.calls.append(("list_activities", date_from, date_to))
        return self.activities_lookup

    def create_activity(self, payload: dict) -> dict:
        self.calls.append(("create_activity", payload))
        return self.next_create_response

    def update_activity(self, activity_id: int, payload: dict) -> dict:
        self.calls.append(("update_activity", activity_id, payload))
        return self.next_update_response

    def delete_activity(self, activity_id: int) -> None:
        self.calls.append(("delete_activity", activity_id))


@pytest.fixture
def api():
    fake = FakeMocoAPI()
    fake.projects = load_fixture("target_projects.json")
    return fake


def test_matched_activity_resolves_to_named_project_and_task(api):
    source = load_fixture("activity_create_matched.json")
    service = MocoSyncService(api=api, **DEFAULTS)

    result = service.sync_create(source)

    assert result == {"created_id": 99999999,
                      "project_id": 1234500001,
                      "task_id": 1234500002}

    assert [c[0] for c in api.calls] == ["list_projects", "create_activity"]
    create_payload = api.calls[1][1]
    assert create_payload == {
        "date": "2025-01-10",
        "description": "Implement webhook receiver",
        "project_id": 1234500001,
        "task_id": 1234500002,
        "seconds": 900,
        "remote_service": "",
        "remote_id": "solar:1064823757",
        "remote_url": "https://github.com/example/repo/pull/42",
        "tag": "backend",
    }


def test_unmatched_activity_falls_back_to_defaults(api):
    source = load_fixture("activity_create_unmatched.json")
    service = MocoSyncService(api=api, **DEFAULTS)

    result = service.sync_create(source)

    assert result["project_id"] == 947156885
    assert result["task_id"] == 25339113

    create_payload = api.calls[1][1]
    assert create_payload["project_id"] == 947156885
    assert create_payload["task_id"] == 25339113
    # Source ID is namespaced into remote_id; remote_service is left empty
    # because Moco rejects non-enum values server-side.
    assert create_payload["remote_id"] == "solar:1064823758"
    assert create_payload["remote_service"] == ""
    # null remote_url is coerced to empty string.
    assert create_payload["remote_url"] == ""


def test_after_project_fallback_task_name_is_resolved_against_default_project(api):
    """When the source project doesn't match by name, the service falls back
    to the default project — and *then* searches that project's task list for
    the source task name. A matching task wins over the default task id."""
    source = load_fixture("activity_create_unmatched.json")
    api.projects[0]["tasks"].append({"id": 77777777, "name": "Phantom Task",
                                     "billable": True, "active": True})

    service = MocoSyncService(api=api, **DEFAULTS)
    result = service.sync_create(source)

    assert result["project_id"] == 947156885
    assert result["task_id"] == 77777777


def test_payload_uses_empty_strings_for_missing_optional_fields(api):
    """`tag`, `remote_url`, and `description` are coerced to "" when null/missing.
    `remote_id` is always our namespaced string; `remote_service` is always ""."""
    source = {
        "id": 12345,
        "date": "2025-03-01",
        "description": None,
        "seconds": 600,
        "project": {"name": "Webseite und IT Services"},
        "task": {"name": "Development"},
    }
    service = MocoSyncService(api=api, **DEFAULTS)
    service.sync_create(source)

    create_payload = api.calls[1][1]
    assert create_payload["description"] == ""
    assert create_payload["tag"] == ""
    assert create_payload["remote_url"] == ""
    assert create_payload["remote_id"] == "solar:12345"
    assert create_payload["remote_service"] == ""


def test_update_finds_existing_target_and_puts(api):
    """Update path: list activities for the source date, find by namespaced
    remote_id, then update the matched target activity."""
    source = load_fixture("activity_create_matched.json")
    api.activities_lookup = load_fixture("target_activities_for_date.json")

    service = MocoSyncService(api=api, **DEFAULTS)
    result = service.sync_update(source)

    assert result == {"updated_id": 88888881,
                      "project_id": 1234500001,
                      "task_id": 1234500002}

    assert [c[0] for c in api.calls] == [
        "list_activities", "list_projects", "update_activity",
    ]
    lookup_call = api.calls[0]
    assert lookup_call == ("list_activities", "2025-01-10", "2025-01-10")
    update_call = api.calls[2]
    assert update_call[1] == 88888881  # activity id
    # The updated payload carries the new description from the source.
    assert update_call[2]["description"] == "Implement webhook receiver"
    assert update_call[2]["remote_id"] == "solar:1064823757"
    assert update_call[2]["remote_service"] == ""


def test_update_upserts_when_no_target_activity_matches(api):
    """If no existing target activity matches the source remote_id, fall
    through to create — the function is stateless and can't distinguish a
    missed create-webhook from a real new activity."""
    source = load_fixture("activity_create_matched.json")
    api.activities_lookup = []

    service = MocoSyncService(api=api, **DEFAULTS)
    result = service.sync_update(source)

    assert result == {"created_id": 99999999,
                      "project_id": 1234500001,
                      "task_id": 1234500002,
                      "upserted": True}
    assert [c[0] for c in api.calls] == [
        "list_activities", "list_projects", "create_activity",
    ]


def test_update_ignores_activities_from_other_source_namespaces(api):
    """Activities whose remote_id is namespaced to a different source account
    must NOT be matched — otherwise unrelated integrations would collide."""
    source = load_fixture("activity_create_matched.json")
    activities = load_fixture("target_activities_for_date.json")
    # Drop our "solar:..." entry so only the "different-account:..." entry
    # with the same numeric ID survives. Service should treat as not-found
    # and upsert.
    api.activities_lookup = [a for a in activities
                             if not str(a.get("remote_id") or "").startswith("solar:")]

    service = MocoSyncService(api=api, **DEFAULTS)
    result = service.sync_update(source)

    assert "created_id" in result
    assert result.get("upserted") is True


def test_delete_finds_existing_target_and_issues_delete(api):
    """Delete path: list activities for the source date, find by namespaced
    remote_id, then delete the matched target activity. No project mapping
    required."""
    source = load_fixture("activity_create_matched.json")
    api.activities_lookup = load_fixture("target_activities_for_date.json")

    service = MocoSyncService(api=api, **DEFAULTS)
    result = service.sync_delete(source)

    assert result == {"deleted_id": 88888881}
    assert [c[0] for c in api.calls] == ["list_activities", "delete_activity"]
    assert api.calls[1] == ("delete_activity", 88888881)


def test_delete_raises_when_no_target_found(api):
    """If the source activity was never synced (or already removed), the
    service raises TargetNotFoundError carrying the namespaced id so the
    caller can decide how to surface it (HTTP 404 in our case)."""
    source = load_fixture("activity_create_matched.json")
    api.activities_lookup = []

    service = MocoSyncService(api=api, **DEFAULTS)
    with pytest.raises(TargetNotFoundError) as exc_info:
        service.sync_delete(source)

    assert str(exc_info.value) == "solar:1064823757"
    assert [c[0] for c in api.calls] == ["list_activities"]


def test_delete_with_dateless_source_widens_the_lookup_window(api):
    """Moco's Activity:delete webhook ships only `{id}`. When the source dict
    has no `date`, the lookup must scan a window ending today instead of a
    single missing day."""
    import datetime as dt
    source = {"id": 1064823757}
    api.activities_lookup = load_fixture("target_activities_for_date.json")

    service = MocoSyncService(api=api, **DEFAULTS)
    result = service.sync_delete(source)

    assert result == {"deleted_id": 88888881}
    _, date_from, date_to = api.calls[0]
    today = dt.date.today().isoformat()
    expected_from = (dt.date.today()
                     - dt.timedelta(days=service.DATELESS_LOOKUP_DAYS)).isoformat()
    assert date_to == today
    assert date_from == expected_from


def test_delete_does_not_match_other_source_namespaces(api):
    """Same numeric ID under a different source-namespace must not be deleted."""
    source = load_fixture("activity_create_matched.json")
    activities = load_fixture("target_activities_for_date.json")
    api.activities_lookup = [a for a in activities
                             if not str(a.get("remote_id") or "").startswith("solar:")]

    service = MocoSyncService(api=api, **DEFAULTS)
    with pytest.raises(TargetNotFoundError):
        service.sync_delete(source)
    assert [c[0] for c in api.calls] == ["list_activities"]
