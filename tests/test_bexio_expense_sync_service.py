"""Unit tests for BexioExpenseSyncService — payload shape, branches, idempotency.

Injects FakeBexioAPI + FakeSourceMocoClient so no HTTP is touched.
"""

import pytest

from api.bexio_expense_sync_service import BexioExpenseSyncService
from tests.conftest import load_fixture


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


class FakeSourceMoco:
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
    return FakeSourceMoco()


@pytest.fixture
def service(bexio, source):
    return BexioExpenseSyncService(bexio=bexio, source_moco=source,
                                   source_account_url="solar")


def test_skips_when_no_company(service, bexio):
    body = load_fixture("purchase_no_company.json")
    result = service.sync(body)
    assert result == {"skipped": "no_company"}
    assert bexio.calls == []  # no Bexio calls at all


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

    assert result == {"action": "created", "bill_id": 9001, "contact_id": 5001}

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
    # Comment posted back to Moco
    assert source.comments == [{
        "id": 2413131, "type": "Purchase",
        "text": "Lieferantenrechnung in Bexio erstellt: "
                "https://office.bexio.com/index.php/kb_bill/list#/show/9001"
    }]


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

    assert result == {"action": "updated", "bill_id": 8889, "contact_id": 5001}
    update_call = next(c for c in bexio.calls if c[0] == "update_bill")
    assert update_call[1] == 8889
    payload = update_call[2]
    assert payload["document_no"] == "DOC-42"
    assert payload["manual_amount"] is True
    assert payload["split_into_line_items"] is False
    # Existing attachment_ids on the bill -> service does NOT re-upload.
    assert not any(c[0] == "upload_file" for c in bexio.calls)


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
                      "contact_id": 5200}
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
    return BexioExpenseSyncService(bexio=bexio, source_moco=source,
                                   source_account_url="solar",
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
