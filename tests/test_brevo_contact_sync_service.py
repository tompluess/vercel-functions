"""Unit tests for BrevoContactSyncService — empty-email skip, create vs update
branching, phone normalization, list add, and Moco cross-comment."""

import io
import logging
from urllib import error as urlerror

import pytest

from api.brevo_contact_sync_service import BrevoContactSyncService, _normalize_phone
from tests.conftest import load_fixture


class FakeBrevoAPI:
    def __init__(self):
        self.contacts_by_email: dict[str, dict] = {}
        self.next_create_response = {"id": 9001}
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()  # method names that should raise

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"{name} boom")

    def get_contact(self, identifier):
        self.calls.append(("get_contact", identifier))
        self._maybe_fail("get_contact")
        return self.contacts_by_email.get(identifier)

    def create_contact(self, payload):
        self.calls.append(("create_contact", payload))
        self._maybe_fail("create_contact")
        return self.next_create_response

    def update_contact(self, identifier, payload):
        self.calls.append(("update_contact", identifier, payload))
        self._maybe_fail("update_contact")
        return {}

    def add_to_list(self, list_id, emails):
        self.calls.append(("add_to_list", list_id, emails))
        self._maybe_fail("add_to_list")
        return {"contacts": {"success": emails, "failure": []}}


class FakeSourceMoco:
    def __init__(self):
        self.comments: list[dict] = []
        self.calls: list[tuple] = []

    def post_comment(self, *, commentable_id, commentable_type, text):
        self.calls.append(("post_comment", commentable_id, commentable_type, text))
        self.comments.append({"id": commentable_id, "type": commentable_type,
                              "text": text})
        return {}


@pytest.fixture
def brevo():
    return FakeBrevoAPI()


@pytest.fixture
def source():
    return FakeSourceMoco()


@pytest.fixture
def service(brevo, source):
    return BrevoContactSyncService(brevo=brevo, source_moco=source,
                                   source_account_url="solar", list_id=5)


def test_skips_when_work_email_is_empty(service, brevo, source):
    body = load_fixture("contact_no_email.json")
    result = service.sync(body)
    assert result == {"skipped": "no_work_email"}
    # No Brevo and no Moco calls at all — gated upfront.
    assert brevo.calls == []
    assert source.calls == []


def test_creates_contact_when_brevo_lookup_returns_none(service, brevo, source):
    """Happy create path: lookup miss -> POST /contacts, comment to Moco, then
    SMS update and list add (the same two convergence steps update also runs)."""
    body = load_fixture("contact_create.json")

    result = service.sync(body)

    assert result["action"] == "created"
    assert result["brevo_id"] == 9001
    assert result["email"] == "max@muster.example"

    methods = [c[0] for c in brevo.calls]
    assert methods == ["get_contact", "create_contact", "update_contact", "add_to_list"]

    create_payload = next(c for c in brevo.calls if c[0] == "create_contact")[1]
    assert create_payload["email"] == "max@muster.example"
    assert create_payload["attributes"]["VORNAME"] == "Max"
    assert create_payload["attributes"]["NACHNAME"] == "Muster"
    # ADDITIONAL_INFO contains today's date AND the Moco contact URL.
    info = create_payload["attributes"]["ADDITIONAL_INFO"]
    assert "https://solar.mocoapp.com/contacts/1763367" in info
    assert "Added from Moco" in info
    # updateEnabled=False: don't silently overwrite on a race.
    assert create_payload["updateEnabled"] is False

    # SMS update uses the normalized phone (E.164-already, just trim spaces).
    sms_call = next(c for c in brevo.calls if c[0] == "update_contact")
    assert sms_call[1] == "max@muster.example"
    assert sms_call[2] == {"attributes": {"SMS": "+41777777777"}}

    # List add: hits configured list_id with the new email.
    list_call = next(c for c in brevo.calls if c[0] == "add_to_list")
    assert list_call[1] == 5
    assert list_call[2] == ["max@muster.example"]

    # Comment back to Moco with the Brevo URL.
    assert source.comments == [{
        "id": 1763367, "type": "Contact",
        "text": "Contact added to Brevo: https://app.brevo.com/contact/index/9001",
    }]


def test_updates_contact_when_brevo_lookup_finds_existing(service, brevo, source):
    """Lookup hit -> PUT /contacts/{email} with VORNAME, NACHNAME,
    RESPONSIBLE_PERSON and JOB_TITLE. NO comment is posted back to Moco on
    update (we only comment on create, matching the n8n branch)."""
    brevo.contacts_by_email["max@muster.example"] = {
        "id": 9001, "email": "max@muster.example",
    }
    body = load_fixture("contact_update.json")

    result = service.sync(body)

    assert result == {"action": "updated", "email": "max@muster.example"}

    update_calls = [c for c in brevo.calls if c[0] == "update_contact"]
    # Two updates: one for the main attributes, one for SMS — in that order.
    assert len(update_calls) == 2
    main = update_calls[0]
    assert main[1] == "max@muster.example"
    assert main[2]["attributes"]["VORNAME"] == "Max"
    assert main[2]["attributes"]["NACHNAME"] == "Muster"
    # RESPONSIBLE_PERSON is "<user.firstname> <user.lastname>" of the Moco owner.
    assert main[2]["attributes"]["RESPONSIBLE_PERSON"] == "Anna Beispiel"
    assert main[2]["attributes"]["JOB_TITLE"] == "CEO"

    # SMS update — n8n normalization drops a single leading zero from
    # national-format phone numbers and strips whitespace ("077 777 77 77"
    # -> "777777777", 9 sevens).
    sms = update_calls[1]
    assert sms[2] == {"attributes": {"SMS": "777777777"}}

    # No comment back to Moco on update.
    assert source.comments == []


def test_list_add_failure_does_not_fail_the_sync(service, brevo, source):
    """If Brevo's list-add call errors (rate limit, transient 5xx), the
    contact has already been created/updated — we don't want Moco to retry the
    entire webhook and possibly duplicate work. Failure is logged + swallowed."""
    brevo.contacts_by_email["max@muster.example"] = {"id": 9001}
    brevo.fail_on = {"add_to_list"}
    body = load_fixture("contact_update.json")

    result = service.sync(body)  # must not raise
    assert result["action"] == "updated"


def test_sms_update_failure_does_not_fail_the_sync(service, brevo):
    """SMS normalization edge cases (invalid characters etc.) can cause Brevo
    to 400; the main contact mutation already succeeded so we keep going."""
    body = load_fixture("contact_create.json")

    # Only the SECOND update_contact (SMS) should fail; the first one is
    # actually create_contact, so we can fail update_contact unconditionally.
    brevo.fail_on = {"update_contact"}
    result = service.sync(body)
    assert result["action"] == "created"


def test_create_lookup_404_then_create_flow(service, brevo, source):
    """Regression: in production the lookup miss is signaled by `None` (the
    BrevoAPI maps 404 -> None). The service must accept that, not require an
    exception."""
    # Default FakeBrevoAPI returns None for unseen emails — same as real 404.
    assert "max@muster.example" not in brevo.contacts_by_email
    body = load_fixture("contact_create.json")
    result = service.sync(body)
    assert result["action"] == "created"


def test_list_add_http_401_logs_warning_not_traceback(service, brevo, source,
                                                      caplog):
    """Regression: in prod the API key did not have list-management
    permission for list_id=8, so add_to_list returned 401. The sync still
    succeeds (the contact mutation is the authoritative outcome), and the
    log line must be a clean warning carrying the upstream status+body —
    NOT a logger.exception traceback that misleads the operator into
    looking for a code bug. Saved as a feedback memory pattern:
    side-effect HTTP errors are upstream-shape issues, not bugs."""
    brevo.contacts_by_email["max@muster.example"] = {"id": 9001}

    def boom(*_):
        raise urlerror.HTTPError(
            "https://api.brevo.com/v3/contacts/lists/8/contacts/add",
            401, "Unauthorized", {},
            fp=io.BytesIO(b'{"code":"unauthorized","message":"Key not '
                          b'authorized for this resource"}'),
        )
    brevo.add_to_list = boom

    body = load_fixture("contact_update.json")
    with caplog.at_level(logging.DEBUG, logger="moco_sync"):
        result = service.sync(body)  # must not raise

    assert result["action"] == "updated"
    matching = [r for r in caplog.records
                if "list add" in r.message and "status=401" in r.message]
    assert matching, f"expected a warning log for the 401, got: {[r.message for r in caplog.records]}"
    # The single matching record must be a warning, not an error (no traceback).
    assert all(r.levelno == logging.WARNING for r in matching)
    assert all(r.exc_info is None for r in matching)


def test_sms_update_http_400_logs_warning_not_traceback(service, brevo, source,
                                                       caplog):
    """Same pattern for the SMS update step: an upstream 4xx must produce a
    tidy warning, not a stack trace. The MAIN update (which carries
    VORNAME/NACHNAME/…) must succeed; only the SMS-only follow-up errors."""
    brevo.contacts_by_email["max@muster.example"] = {"id": 9001}
    real_update = brevo.update_contact

    def selective_boom(identifier, payload):
        # Only the SMS-only call should fail; the main attributes call goes
        # through normally so we reach the SMS step.
        attrs = (payload or {}).get("attributes") or {}
        if set(attrs.keys()) == {"SMS"}:
            raise urlerror.HTTPError(
                f"https://api.brevo.com/v3/contacts/{identifier}", 400, "Bad",
                {}, fp=io.BytesIO(b'{"code":"invalid_parameter"}'),
            )
        return real_update(identifier, payload)
    brevo.update_contact = selective_boom

    body = load_fixture("contact_update.json")
    with caplog.at_level(logging.DEBUG, logger="moco_sync"):
        result = service.sync(body)  # must not raise

    assert result["action"] == "updated"
    matching = [r for r in caplog.records
                if "SMS update" in r.message and "status=400" in r.message]
    assert matching, f"expected a warning log for the SMS 400, got: {[r.message for r in caplog.records]}"
    assert all(r.levelno == logging.WARNING for r in matching)
    assert all(r.exc_info is None for r in matching)


@pytest.mark.parametrize("raw,expected", [
    ("", ""),
    ("+41 77 777 77 77", "+41777777777"),
    ("0041 77 777 77 77", "0041777777777"),
    ("0 77 777 77 77", "777777777"),
    ("077 777 77 77", "777777777"),
    # Anything not starting with +/00/0 is left as-is (n8n behavior).
    ("177 777", "177 777"),
])
def test_normalize_phone(raw, expected):
    assert _normalize_phone(raw) == expected
