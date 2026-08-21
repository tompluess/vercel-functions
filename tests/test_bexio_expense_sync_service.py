"""Unit tests for BexioExpenseSyncService — payload shape, branches, idempotency.

Injects FakeBexioAPI + FakeMocoClient so no HTTP is touched.
"""

import json
from urllib import error as urlerror

import pytest

from api.bexio_expense_sync_service import BexioExpenseSyncService
from tests.conftest import load_fixture


SENDER = {
    "name": "PVcontracting AG", "iban": "CH8580808003633835369",
    "bank_name": "Raiffeisen", "bc_no": "80808",
    "street": "Schachenstrasse", "house_no": "15C",
    "postcode": "6010", "city": "Kriens", "country_code": "CH",
    "bank_account_id": 2,
}


@pytest.fixture(autouse=True)
def _outgoing_payment_sender_env(monkeypatch):
    """All tests run with a configured sender; tests that exercise the
    missing-env branch delete the var explicitly."""
    monkeypatch.setenv("BEXIO_OUTGOING_PAYMENT_SENDER", json.dumps(SENDER))


class FakeBexioAPI:
    def __init__(self):
        self.contacts_by_name: dict[str, list[dict]] = {}
        self.accounts_by_no: dict[str, list[dict]] = {}
        self.bills_envelope: dict = {"data": [], "paging": {"item_count": 0}}
        self.bill_by_id: dict[int, dict] = {}
        self.next_create_contact: dict = {"id": 5001}
        self.next_create_bill: dict = {"id": 9001}
        self.next_update_bill: dict = {"id": 9001}
        self.next_upload: dict = {"uuid": "file-uuid-1"}
        self.next_book_bill: dict = {"status": "BOOKED"}
        self.next_outgoing_payment: dict = {"id": 7001,
                                            "execution_date": "2024-12-30"}
        self.book_bill_error: Exception | None = None
        self.outgoing_payment_error: Exception | None = None
        self.calls: list[tuple] = []

    def search_contact_by_name(self, name):
        self.calls.append(("search_contact_by_name", name))
        return self.contacts_by_name.get(name, [])

    def create_contact(self, payload):
        self.calls.append(("create_contact", payload))
        return self.next_create_contact

    def search_account_by_no(self, account_no):
        self.calls.append(("search_account_by_no", account_no))
        return self.accounts_by_no.get(str(account_no), [])

    def search_bills(self, *, vendor, vendor_ref=None):
        self.calls.append(("search_bills", vendor, vendor_ref))
        return self.bills_envelope

    def get_bill(self, bill_id):
        self.calls.append(("get_bill", bill_id))
        return self.bill_by_id[bill_id]

    def create_bill(self, payload):
        self.calls.append(("create_bill", payload))
        return self.next_create_bill

    def update_bill(self, bill_id, payload):
        self.calls.append(("update_bill", bill_id, payload))
        return self.next_update_bill

    def upload_file(self, *, filename, content, mime_type=None):
        self.calls.append(("upload_file", filename, len(content), mime_type))
        return self.next_upload

    def book_bill(self, bill_id):
        self.calls.append(("book_bill", bill_id))
        if self.book_bill_error:
            raise self.book_bill_error
        return self.next_book_bill

    def create_outgoing_payment(self, payload):
        self.calls.append(("create_outgoing_payment", payload))
        if self.outgoing_payment_error:
            raise self.outgoing_payment_error
        return self.next_outgoing_payment


class FakeMoco:
    def __init__(self):
        self.companies: dict[int, dict] = {}
        self.files: dict[str, bytes] = {}
        self.comments: list[dict] = []
        self.calls: list[tuple] = []

    def get_company(self, company_id):
        self.calls.append(("get_company", company_id))
        return self.companies[company_id]

    def get_project(self, project_id):
        self.calls.append(("get_project", project_id))
        return {}

    def post_comment(self, *, commentable_id, commentable_type, text):
        self.calls.append(("post_comment", commentable_id, commentable_type, text))
        self.comments.append({"id": commentable_id, "type": commentable_type, "text": text})
        return {}

    def download_file(self, url):
        self.calls.append(("download_file", url))
        return self.files.get(url, b"%PDF-1.4 fake")


class FakeTelegram:
    def __init__(self):
        self.messages: list[str] = []

    def notify(self, text):
        self.messages.append(text)
        return True


@pytest.fixture
def bexio():
    return FakeBexioAPI()


@pytest.fixture
def telegram():
    return FakeTelegram()


@pytest.fixture
def source():
    return FakeMoco()


@pytest.fixture
def service(bexio, source):
    return BexioExpenseSyncService(bexio=bexio, moco=source,
                                   subdomain="solar")


def test_skips_when_no_company(service, bexio):
    body = load_fixture("purchase_no_company.json")
    result = service.sync(body)
    assert result == {"skipped": "no_company"}
    assert bexio.calls == []  # no Bexio calls at all


def test_skips_silently_when_purchase_carries_review_pending_tag(service, bexio,
                                                                  source):
    """Purchases tagged 'Review pending' come from the OCR auto-create
    flow and aren't validated by a human yet. Sync would propagate OCR
    mistakes (wrong supplier, wrong amount). Skip first thing, before
    ANY Bexio or source-Moco I/O fires."""
    body = {**load_fixture("purchase_with_iban.json"),
            "tags": ["OCR", "Review pending"]}
    result = service.sync(body)
    assert result == {"skipped": "review_pending"}
    assert bexio.calls == []
    assert source.calls == []   # no source Moco reads either


def test_review_pending_tag_match_is_case_insensitive(service, bexio):
    """Operators might re-create the tag manually with different casing
    ('review pending', 'REVIEW PENDING'). Match liberally so a hand-typed
    variant still triggers the gate."""
    for label in ["review pending", "Review Pending", "REVIEW PENDING",
                  "  Review pending  "]:
        body = {**load_fixture("purchase_with_iban.json"), "tags": [label]}
        assert service.sync(body) == {"skipped": "review_pending"}, label
        assert bexio.calls == []


def test_other_tags_do_not_trigger_review_pending_skip(service, bexio, source):
    """Only 'Review pending' is the gate — a purchase that's been
    reviewed (tag stripped) but still carries the 'OCR' tag must sync
    normally."""
    body = {**load_fixture("purchase_with_iban.json"),
            "tags": ["OCR", "Approved"]}
    # Mirror the happy-path test's fakes so sync can proceed end-to-end.
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [
        {"id": 5001, "street_name": "Kasernenstrasse",
         "house_number": "1", "postcode": "8004", "city": "Zürich"}
    ]
    bexio.accounts_by_no["6600"] = [{"id": 7700, "tax_id": 42}]
    result = service.sync(body)
    # Sync proceeds (no "skipped" key on success); a Bexio bill is created.
    assert "skipped" not in result
    assert any(c[0] == "create_bill" for c in bexio.calls)


def test_review_pending_skip_works_when_tags_field_missing():
    """Defensive: a Moco purchase without `tags` at all must NOT crash
    the matcher (tags is optional in the API)."""
    from api.bexio_expense_sync_service import _has_review_pending_tag
    assert _has_review_pending_tag({}) is False
    assert _has_review_pending_tag({"tags": None}) is False
    assert _has_review_pending_tag({"tags": []}) is False
    assert _has_review_pending_tag({"tags": "Review pending"}) is False  # not a list


def test_skips_when_no_account(service, bexio):
    """The Moco expense must carry items[0].category.credit_account (or
    supplier_credit_number) to know which Bexio account to book against."""
    bexio.contacts_by_name["Misc Vendor"] = [{"id": 5050}]
    body = load_fixture("purchase_no_account.json")

    result = service.sync(body)

    assert result == {"skipped": "no_account", "contact_id": 5050}
    # Contact lookup ran, but no bill / account / upload calls happened.
    methods = [c[0] for c in bexio.calls]
    assert methods == ["search_contact_by_name"]


def test_creates_bill_with_iban_payment_block(service, bexio, source):
    """IBAN present -> payment.type is QR (because reference is also present)
    and the bill goes via the default bank_account_id. Attachment uploaded
    from file_url and referenced by uuid."""
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [
        {"id": 5001, "street_name": "Kasernenstrasse",
         "house_number": "1", "postcode": "8004", "city": "Zürich"}
    ]
    bexio.accounts_by_no["6600"] = [{"id": 7700, "tax_id": 42}]
    body = load_fixture("purchase_with_iban.json")

    result = service.sync(body)

    assert result == {"action": "created", "bill_id": 9001, "contact_id": 5001,
                      "payment_id": 7001}

    create_call = next(c for c in bexio.calls if c[0] == "create_bill")
    payload = create_call[1]
    assert payload["supplier_id"] == 5001
    assert payload["vendor_ref"] == "CH240067780"
    assert payload["bill_date"] == "2024-12-09"
    assert payload["due_date"] == "2024-12-30"
    assert payload["amount_man"] == 67.43
    assert payload["amount_calc"] == 67.43
    assert payload["manual_amount"] is False
    assert payload["currency_code"] == "CHF"
    assert payload["line_items"] == [{
        "position": 0,
        "title": "Visitenkarten und Flyer",
        "amount": 67.43,
        "booking_account_id": 7700,
        "tax_id": 42,
    }]
    # IBAN + reference -> QR payment
    assert payload["payment"]["type"] == "QR"
    assert payload["payment"]["iban"] == "CH7708836121049112006"
    assert payload["payment"]["reference_no"] == "CH240067780"
    assert payload["payment"]["bank_account_id"] == 2  # BANK_ACCOUNT_ID
    # Attachment uploaded
    upload_call = next(c for c in bexio.calls if c[0] == "upload_file")
    assert upload_call[1] == "2024-12-09 FLYERALARM - RatePAY GmbH CH240067780.pdf"
    assert payload["attachment_ids"] == ["file-uuid-1"]
    # Two comments posted back to Moco: the creation comment, and the
    # "booked + Zahlungsausgang" comment after the book+pay step.
    assert source.comments == [
        {
            "id": 2413131, "type": "Purchase",
            "text": "Lieferantenrechnung in Bexio erstellt: "
                    "https://office.bexio.com/index.php/kb_bill/list#/show/9001",
        },
        {
            "id": 2413131, "type": "Purchase",
            "text": ("Lieferantenrechnung in Bexio auf <strong>gebucht</strong> "
                     "gesetzt und Zahlungsausgang erstellt per 2024-12-30: "
                     "https://office.bexio.com/index.php/kb_bill/list#/show/9001"),
        },
    ]


def test_create_without_iban_uses_manual_payment_and_user_bank(
    service, bexio, source, monkeypatch
):
    """No IBAN -> payment.type MANUAL and bank_account_id resolved from
    BEXIO_MANUAL_BANK_MAP env var by the user's first name."""
    monkeypatch.setenv("BEXIO_MANUAL_BANK_MAP",
                       '{"default": 3, "Tom": 9, "Other": 7}')
    bexio.contacts_by_name["Restaurant Beispiel"] = [{"id": 5060}]
    bexio.accounts_by_no["6500"] = [{"id": 7800, "tax_id": 33}]
    body = load_fixture("purchase_no_iban.json")

    result = service.sync(body)

    # MANUAL bills (no IBAN) skip the book+pay step entirely — Bexio's
    # outgoing-payments endpoint rejects MANUAL payloads, and routine cash
    # bills don't benefit from auto-booking anyway.
    assert result == {"action": "created", "bill_id": 9001, "contact_id": 5060}
    payload = next(c for c in bexio.calls if c[0] == "create_bill")[1]
    assert payload["payment"]["type"] == "MANUAL"
    # Tom is mapped to 9 in BEXIO_MANUAL_BANK_MAP.
    assert payload["payment"]["bank_account_id"] == 9
    # No IBAN means no QR-bill information and no file_url => empty attachments.
    assert "qr_bill_information" not in payload
    assert payload["attachment_ids"] == []
    # No file_url in fixture => no upload call.
    assert not any(c[0] == "upload_file" for c in bexio.calls)
    # MANUAL -> no book / no outgoing-payment calls.
    assert not any(c[0] in ("book_bill", "create_outgoing_payment")
                   for c in bexio.calls)


def test_creates_contact_when_missing_using_moco_company_data(
    service, bexio, source
):
    """First Bexio search returns []; the service fetches the source-Moco
    company and POSTs to /contact. After creation, the bill payload's
    supplier_id is the newly-created contact id."""
    bexio.contacts_by_name = {}  # no match
    bexio.accounts_by_no["6500"] = [{"id": 7800, "tax_id": 33}]
    source.companies[762314140] = {
        "id": 762314140, "name": "Restaurant Beispiel",
        "address": "Restaurant Beispiel\nBahnhofstrasse 5\n8001 Zürich\nSchweiz",
        "email": "info@example.com", "phone": "+41 44 000 00 00",
        "website": "https://example.com",
    }
    body = load_fixture("purchase_no_iban.json")

    service.sync(body)

    create_contact_call = next(c for c in bexio.calls if c[0] == "create_contact")
    contact_payload = create_contact_call[1]
    assert contact_payload["name_1"] == "Restaurant Beispiel"
    assert contact_payload["street_name"] == "Bahnhofstrasse"
    assert contact_payload["house_number"] == "5"
    assert contact_payload["postcode"] == "8001"
    assert contact_payload["city"] == "Zürich"
    # The created contact id (5001 from FakeBexioAPI.next_create_contact)
    # becomes supplier_id on the bill.
    bill_payload = next(c for c in bexio.calls if c[0] == "create_bill")[1]
    assert bill_payload["supplier_id"] == 5001


def test_skips_when_existing_bill_is_not_draft(service, bexio):
    """If Bexio already has a non-DRAFT bill for this vendor + ref, skip the
    update — we don't mutate booked bills, just signal the situation."""
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [{"id": 5001}]
    bexio.accounts_by_no["6600"] = [{"id": 7700, "tax_id": 42}]
    bexio.bills_envelope = {"data": [{"id": 8888, "status": "BOOKED"}],
                            "paging": {"item_count": 1}}
    bexio.bill_by_id[8888] = {"id": 8888, "status": "BOOKED"}
    body = load_fixture("purchase_with_iban.json")

    result = service.sync(body)

    assert result == {"skipped": "bill_not_draft", "bill_id": 8888,
                      "status": "BOOKED"}
    # No create/update call was made.
    assert not any(c[0] in ("create_bill", "update_bill") for c in bexio.calls)


def test_updates_draft_bill_when_found(service, bexio):
    """Draft bill exists -> PUT instead of POST; updated payload sets
    manual_amount=True and preserves document_no + split_into_line_items
    from the existing bill (matches n8n's update node)."""
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [{"id": 5001}]
    bexio.accounts_by_no["6600"] = [{"id": 7700, "tax_id": 42}]
    bexio.bills_envelope = {"data": [{"id": 8889, "status": "DRAFT"}],
                            "paging": {"item_count": 1}}
    bexio.bill_by_id[8889] = {
        "id": 8889, "status": "DRAFT", "document_no": "DOC-42",
        "split_into_line_items": False, "attachment_ids": ["existing-uuid"],
    }
    body = load_fixture("purchase_with_iban.json")

    result = service.sync(body)

    assert result == {"action": "updated", "bill_id": 8889, "contact_id": 5001,
                      "payment_id": 7001}
    update_call = next(c for c in bexio.calls if c[0] == "update_bill")
    assert update_call[1] == 8889
    payload = update_call[2]
    assert payload["document_no"] == "DOC-42"
    assert payload["manual_amount"] is True
    assert payload["split_into_line_items"] is False
    # Existing attachment_ids on the bill -> service does NOT re-upload.
    assert not any(c[0] == "upload_file" for c in bexio.calls)
    # ...and they must be preserved in the PUT payload — Bexio replaces
    # attachment_ids on update, so sending [] would detach the file.
    assert payload["attachment_ids"] == ["existing-uuid"]


def test_account_fallback_when_lookup_misses(service, bexio):
    """Unknown booking account number falls back to DEFAULT_BOOKING_ACCOUNT_NO
    (4000). If the fallback also misses, line_item tax_id falls back to the
    constant default (10) without raising."""
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [{"id": 5001}]
    # Neither 6600 nor the fallback 4000 returns results.
    body = load_fixture("purchase_with_iban.json")

    service.sync(body)

    # search_account_by_no was called twice: first for 6600, then for 4000.
    account_calls = [c for c in bexio.calls if c[0] == "search_account_by_no"]
    assert [c[1] for c in account_calls] == ["6600", "4000"]
    bill_payload = next(c for c in bexio.calls if c[0] == "create_bill")[1]
    # Both lookups missed -> line_item has booking_account_id None and tax_id 10.
    assert bill_payload["line_items"][0]["tax_id"] == 10
    assert bill_payload["line_items"][0]["booking_account_id"] is None


def test_real_digitec_payload_handles_list_file_upload_response(
    service, bexio, source
):
    """Regression: Bexio's POST /3.0/files returns a LIST of file records
    (one per uploaded file), not a single object — so `uploaded.get("uuid")`
    raised `AttributeError: 'list' object has no attribute 'get'` in prod for
    this real Moco payload (E260107 / Digitec Galaxus, IBAN + file_url +
    booking account 6500). The service must accept both shapes."""
    bexio.contacts_by_name["Digitec Galaxus"] = [
        {"id": 5200, "street_name": "Pfingstweidstrasse",
         "house_number": "60b", "postcode": "8005", "city": "Zürich"}
    ]
    bexio.accounts_by_no["6500"] = [{"id": 7705, "tax_id": 33}]
    # The real Bexio /3.0/files response is a list, not a dict.
    bexio.next_upload = [{
        "id": 201, "uuid": "actual-bexio-uuid",
        "name": "2026-05-27 Digitec Galaxus 80572997",
        "mime_type": "application/pdf",
    }]

    body = load_fixture("purchase_digitec_real.json")
    result = service.sync(body)

    assert result == {"action": "created", "bill_id": 9001,
                      "contact_id": 5200, "payment_id": 7001}
    # The list's first uuid must end up on the bill's attachment_ids.
    bill_payload = next(c for c in bexio.calls if c[0] == "create_bill")[1]
    assert bill_payload["attachment_ids"] == ["actual-bexio-uuid"]
    # Sanity-check: IBAN + reference -> QR payment with the real reference.
    assert bill_payload["payment"]["type"] == "QR"
    assert bill_payload["payment"]["reference_no"] == \
        "940000201026332300805729978"
    assert bill_payload["vendor_ref"] == "80572997"


def test_moco_comment_failure_does_not_fail_sync(service, bexio, source):
    """Bill is already created in Bexio; if the courtesy comment back to Moco
    fails, the sync still returns success — losing the comment is recoverable,
    rolling back the bill is not."""
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [{"id": 5001}]
    bexio.accounts_by_no["6600"] = [{"id": 7700, "tax_id": 42}]

    def boom(**kwargs):
        raise RuntimeError("moco unreachable")
    source.post_comment = boom

    body = load_fixture("purchase_with_iban.json")
    result = service.sync(body)
    assert result["action"] == "created"
    assert result["bill_id"] == 9001


# --- telegram skip notifications --------------------------------------------

@pytest.fixture
def service_tg(bexio, source, telegram):
    return BexioExpenseSyncService(bexio=bexio, moco=source,
                                   subdomain="solar",
                                   telegram=telegram)


def test_notifies_telegram_on_no_company_skip(service_tg, telegram):
    service_tg.sync(load_fixture("purchase_no_company.json"))
    assert len(telegram.messages) == 1
    msg = telegram.messages[0]
    assert "not synced to Bexio" in msg
    assert "Reason: No company given" in msg
    # Entity context carries the Moco purchase deep-link on the source account.
    assert "https://solar.mocoapp.com/purchases/" in msg


def test_notifies_telegram_on_no_account_skip(service_tg, bexio, telegram):
    bexio.contacts_by_name["Misc Vendor"] = [{"id": 5050}]
    service_tg.sync(load_fixture("purchase_no_account.json"))
    assert len(telegram.messages) == 1
    assert "Reason: No account given" in telegram.messages[0]


def test_notifies_telegram_on_bill_closed_skip(service_tg, bexio, telegram):
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [{"id": 5001}]
    bexio.accounts_by_no["6600"] = [{"id": 7700, "tax_id": 42}]
    bexio.bills_envelope = {"data": [{"id": 8888, "status": "BOOKED"}],
                            "paging": {"item_count": 1}}
    bexio.bill_by_id[8888] = {"id": 8888, "status": "BOOKED"}

    service_tg.sync(load_fixture("purchase_with_iban.json"))

    assert len(telegram.messages) == 1
    msg = telegram.messages[0]
    assert "Reason: Bill is closed." in msg
    assert "Bill-Id might not be unique" in msg


def test_no_telegram_message_on_successful_sync(service_tg, bexio, telegram):
    """A clean create must not ping the chat — notifications are for skips
    (and, at the endpoint layer, 5xx errors) only."""
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [{"id": 5001}]
    bexio.accounts_by_no["6600"] = [{"id": 7700, "tax_id": 42}]

    service_tg.sync(load_fixture("purchase_with_iban.json"))

    assert telegram.messages == []


def test_skip_works_without_telegram_configured(service, bexio):
    """telegram is optional — the skip branch must still return its dict when
    no notifier was injected (service-layer unit tests omit it)."""
    result = service.sync(load_fixture("purchase_no_company.json"))
    assert result == {"skipped": "no_company"}


# --- book + outgoing payment ------------------------------------------------

def test_book_and_pay_payload_for_qr_bill(service, bexio):
    """IBAN + reference -> QR payment with NO fee_type, reference_no set,
    no message field, sender info from BEXIO_OUTGOING_PAYMENT_SENDER."""
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [
        {"id": 5001, "street_name": "Kasernenstrasse",
         "house_number": "1", "postcode": "8004", "city": "Zürich"}
    ]
    bexio.accounts_by_no["6600"] = [{"id": 7700, "tax_id": 42}]

    service.sync(load_fixture("purchase_with_iban.json"))

    book_call = next(c for c in bexio.calls if c[0] == "book_bill")
    assert book_call[1] == 9001  # bill id
    pay_call = next(c for c in bexio.calls
                    if c[0] == "create_outgoing_payment")
    p = pay_call[1]
    assert p["bill_id"] == "9001"
    assert p["payment_type"] == "QR"
    assert p["amount"] == 67.43
    assert p["currency_code"] == "CHF"
    assert p["execution_date"] == "2024-12-30"
    assert p["receiver_iban"] == "CH7708836121049112006"
    assert p["receiver_name"] == "FLYERALARM - RatePAY GmbH"
    assert p["receiver_street"] == "Kasernenstrasse"
    assert p["receiver_house_no"] == "1"
    assert p["receiver_postcode"] == "8004"
    assert p["receiver_city"] == "Zürich"
    assert p["reference_no"] == "CH240067780"
    assert p["booking_text"] == "CH240067780"
    assert "message" not in p
    assert "fee_type" not in p  # QR -> no NO_FEE override
    # Sender block populated from the env var.
    assert p["sender_iban"] == SENDER["iban"]
    assert p["sender_name"] == SENDER["name"]
    assert p["sender_bank_account_id"] == SENDER["bank_account_id"]


def test_book_and_pay_payload_for_iban_without_reference(
    service, bexio, monkeypatch
):
    """IBAN without reference -> IBAN payment with fee_type=NO_FEE and a
    `message` field (since there's no reference_no for the recipient)."""
    bexio.contacts_by_name["Vendor"] = [{"id": 5500}]
    bexio.accounts_by_no["6500"] = [{"id": 7800, "tax_id": 10}]
    body = {
        "id": 42, "date": "2024-12-01", "due_date": "2024-12-20",
        "title": "T", "gross_total": 100,
        "iban": "CH0000000000000000000",  # IBAN but no reference
        "receipt_identifier": "INV-1",
        "company": {"name": "Vendor"},
        "items": [{"title": "x", "gross_total": 100,
                   "category": {"credit_account": "6500"}}],
    }

    service.sync(body)

    p = next(c for c in bexio.calls
             if c[0] == "create_outgoing_payment")[1]
    assert p["payment_type"] == "IBAN"
    assert p["fee_type"] == "NO_FEE"
    assert p["message"] == "INV-1"
    assert "reference_no" not in p


def test_manual_bill_skips_book_and_pay(service, bexio, source):
    """MANUAL bills (no IBAN) skip booking and outgoing-payment entirely.

    Bexio rejects MANUAL outgoing-payment payloads when `message` /
    `booking_text` / `reference_no` are set (400 "message is not allowed
    for [MANUAL] payment type"), so we skip the step rather than try to
    strip the fields. Bill is still created; only the booking comment
    is suppressed."""
    bexio.contacts_by_name["Restaurant Beispiel"] = [{"id": 5060}]
    bexio.accounts_by_no["6500"] = [{"id": 7800, "tax_id": 33}]

    result = service.sync(load_fixture("purchase_no_iban.json"))

    assert result == {"action": "created", "bill_id": 9001,
                      "contact_id": 5060}
    methods = [c[0] for c in bexio.calls]
    assert "book_bill" not in methods
    assert "create_outgoing_payment" not in methods
    # The "Lieferantenrechnung erstellt" comment is posted; the "gebucht +
    # Zahlungsausgang" comment is NOT (no payment was created).
    assert len(source.comments) == 1
    assert "gebucht" not in source.comments[0]["text"]


def test_book_and_pay_normalizes_iban_with_spaces(service, bexio):
    """IBAN pasted with spaces (the way humans copy them) is normalized to
    a contiguous uppercase string before being sent to Bexio — otherwise
    Bexio's strict /4.0/payment/outgoing-payments validator rejects it
    with `400 IBAN contains illegal characters`. Bill creation succeeds
    either way (its IBAN field is permissive)."""
    bexio.contacts_by_name["Vendor"] = [{"id": 5500}]
    bexio.accounts_by_no["6500"] = [{"id": 7800, "tax_id": 10}]
    body = {
        "id": 42, "date": "2024-12-01", "due_date": "2024-12-20",
        "title": "T", "gross_total": 100,
        "iban": " ch77  0883 6121 0491 1200 6 ",
        "receipt_identifier": "INV-1",
        "company": {"name": "Vendor"},
        "items": [{"title": "x", "gross_total": 100,
                   "category": {"credit_account": "6500"}}],
    }

    service.sync(body)

    bill_payload = next(c for c in bexio.calls if c[0] == "create_bill")[1]
    assert bill_payload["payment"]["iban"] == "CH7708836121049112006"
    p = next(c for c in bexio.calls
             if c[0] == "create_outgoing_payment")[1]
    assert p["receiver_iban"] == "CH7708836121049112006"


def test_book_failure_notifies_telegram_and_keeps_sync_ok(
    service_tg, bexio, telegram
):
    """book_bill 4xx (e.g. payment already exists on replay) -> sync still
    succeeds and the failure is announced on Telegram with bill context."""
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [{"id": 5001}]
    bexio.accounts_by_no["6600"] = [{"id": 7700, "tax_id": 42}]
    bexio.book_bill_error = urlerror.HTTPError(
        "url", 422, "Bill not in DRAFT", {},
        fp=_io_bytes(b'{"message":"Cannot book non-draft bill"}'),
    )

    result = service_tg.sync(load_fixture("purchase_with_iban.json"))

    assert result["action"] == "created"
    assert result["bill_id"] == 9001
    assert "payment_id" not in result
    assert not any(c[0] == "create_outgoing_payment" for c in bexio.calls)
    assert len(telegram.messages) == 1
    msg = telegram.messages[0]
    assert "booking/payment failed" in msg
    assert "Cannot book non-draft bill" in msg
    assert "https://office.bexio.com/index.php/kb_bill/list#/show/9001" in msg
    assert "https://solar.mocoapp.com/purchases/" in msg


def test_outgoing_payment_failure_notifies_telegram(
    service_tg, bexio, telegram
):
    """Bill books fine but create_outgoing_payment fails (e.g. payment
    already exists for the bill) -> Telegram alert, no booking comment in
    Moco, sync still returns success."""
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [{"id": 5001}]
    bexio.accounts_by_no["6600"] = [{"id": 7700, "tax_id": 42}]
    bexio.outgoing_payment_error = urlerror.HTTPError(
        "url", 409, "Payment already exists", {},
        fp=_io_bytes(b'{"message":"Payment already exists for bill"}'),
    )

    result = service_tg.sync(load_fixture("purchase_with_iban.json"))

    assert result["action"] == "created"
    assert "payment_id" not in result
    assert len(telegram.messages) == 1
    assert "Payment already exists" in telegram.messages[0]


def test_missing_sender_env_notifies_and_skips_book_pay(
    service_tg, bexio, telegram, monkeypatch
):
    """When BEXIO_OUTGOING_PAYMENT_SENDER is missing, we don't crash — we
    skip the book+pay step and surface the misconfiguration on Telegram."""
    monkeypatch.delenv("BEXIO_OUTGOING_PAYMENT_SENDER", raising=False)
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [{"id": 5001}]
    bexio.accounts_by_no["6600"] = [{"id": 7700, "tax_id": 42}]

    result = service_tg.sync(load_fixture("purchase_with_iban.json"))

    assert result["action"] == "created"
    assert "payment_id" not in result
    assert not any(c[0] in ("book_bill", "create_outgoing_payment")
                   for c in bexio.calls)
    assert len(telegram.messages) == 1
    assert "BEXIO_OUTGOING_PAYMENT_SENDER" in telegram.messages[0]


def _io_bytes(data: bytes):
    import io
    return io.BytesIO(data)


# --- payment remark (payment.note) ------------------------------------------
#
# Nothing asserted `note` before, and both branches produced text nobody
# would want to read: the IBAN/QR one emitted a bare "-" whenever Moco's
# `info` was empty (which is most invoices), and the MANUAL one left
# dangling separators around empty parts. See
# `specs/SPEC_manual_upload_subject.md` D6.

def test_qr_payment_remark_is_the_purchase_title(service, bexio):
    """Regression: this used to be `info or "-"`, and `info` (the QR-bill
    Zahlungszweck) is empty on both live IBAN fixtures — so every QR
    payment reached Bexio carrying a bare dash."""
    bexio.contacts_by_name["FLYERALARM - RatePAY GmbH"] = [{"id": 5001}]
    bexio.accounts_by_no["6600"] = [{"id": 7700, "tax_id": 42}]

    service.sync(load_fixture("purchase_with_iban.json"))

    payload = next(c[1] for c in bexio.calls if c[0] == "create_bill")
    assert payload["payment"]["type"] == "QR"
    assert payload["payment"]["note"] == "Visitenkarten und Flyer CH240067780"


def test_manual_payment_remark_is_the_purchase_title(service, bexio):
    """Same rule for both payment types — one remark, one code path."""
    bexio.contacts_by_name["Restaurant Beispiel"] = [{"id": 5002}]
    bexio.accounts_by_no["6640"] = [{"id": 7701, "tax_id": 42}]

    service.sync(load_fixture("purchase_no_iban.json"))

    payload = next(c[1] for c in bexio.calls if c[0] == "create_bill")
    assert payload["payment"]["type"] == "MANUAL"
    assert payload["payment"]["note"] == "Lunch meeting"


def test_remark_falls_back_to_supplier_receipt_and_purpose():
    from api.bexio_expense_sync_service import _payment_note
    assert _payment_note({
        "company": {"name": "Restaurant Beispiel"},
        "receipt_identifier": "R-99999",
        "info": "Geschäftsessen mit Kunde",
    }) == "Restaurant Beispiel - R-99999 - Geschäftsessen mit Kunde"


def test_remark_fallback_drops_empty_parts():
    """Regression on `' - 000047 - '`: the old join filtered on
    `is not None`, but its parts were `x or ""` and so never None. This is
    the exact shape an OCR'd card receipt produces — no matched supplier,
    no Zahlungszweck."""
    from api.bexio_expense_sync_service import _payment_note
    note = _payment_note({"receipt_identifier": "000047"})
    assert note == "000047"
    assert not note.startswith(" - ")
    assert not note.endswith(" - ")


def test_remark_fallback_drops_whitespace_only_parts():
    from api.bexio_expense_sync_service import _payment_note
    assert _payment_note({"company": {"name": "Ligu Lehm"},
                          "receipt_identifier": "  ",
                          "info": ""}) == "Ligu Lehm"


def test_remark_is_truncated_to_the_bexio_budget():
    from api.bexio_expense_sync_service import (
        PAYMENT_NOTE_MAX_CHARS, _payment_note,
    )
    note = _payment_note({"title": "M" * 200})
    assert len(note) == PAYMENT_NOTE_MAX_CHARS


def test_remark_falls_back_to_a_dash_when_there_is_nothing_to_say():
    """Kept from the n8n original — a non-empty note may be required by
    Bexio, and this isn't the change to find that out on."""
    from api.bexio_expense_sync_service import _payment_note
    assert _payment_note({}) == "-"
