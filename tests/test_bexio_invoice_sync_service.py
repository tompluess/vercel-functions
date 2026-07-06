"""Unit tests for BexioInvoiceSyncService — status gating, label routing,
payload shape, and state-transition behavior."""

import pytest

from api.bexio_config import resolve_revenue_account_no
from api.bexio_invoice_sync_service import BexioInvoiceSyncService
from tests.conftest import load_fixture


class FakeBexioAPI:
    def __init__(self):
        self.contacts_by_name: dict[str, list[dict]] = {}
        self.accounts_by_no: dict[str, list[dict]] = {}
        self.templates: list[dict] = []
        self.next_create_contact = {"id": 6001}
        self.next_create_invoice = {"id": 12345}
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

    def list_document_templates(self):
        self.calls.append(("list_document_templates",))
        return self.templates

    def create_invoice(self, payload):
        self.calls.append(("create_invoice", payload))
        return self.next_create_invoice

    def issue_invoice(self, invoice_id):
        self.calls.append(("issue_invoice", invoice_id))
        return {}

    def comment_invoice(self, invoice_id, payload):
        self.calls.append(("comment_invoice", invoice_id, payload))
        return {}


class FakeMoco:
    def __init__(self):
        self.companies: dict[int, dict] = {}
        self.projects: dict[int, dict] = {}
        self.comments: list[dict] = []
        self.calls: list[tuple] = []

    def get_company(self, company_id):
        self.calls.append(("get_company", company_id))
        return self.companies[company_id]

    def get_project(self, project_id):
        self.calls.append(("get_project", project_id))
        return self.projects[project_id]

    def post_comment(self, *, commentable_id, commentable_type, text):
        self.calls.append(("post_comment", commentable_id, commentable_type, text))
        self.comments.append({"id": commentable_id, "type": commentable_type, "text": text})
        return {}

    def download_file(self, url):
        return b""


@pytest.fixture
def bexio():
    api = FakeBexioAPI()
    api.templates = [{"template_slug": "default-de"}]
    return api


@pytest.fixture
def source():
    src = FakeMoco()
    src.projects[947168988] = {
        "id": 947168988,
        "customer": {"id": 762286146, "name": "Muster AG"},
        # Only Stromproduktion: a single label cleanly resolves to 3010.
        # Resolution order when multiple labels match is exercised in
        # test_resolve_revenue_account_orders_match_n8n_chain.
        "labels": ["Stromproduktion"],
        "billing_address": "Muster AG\nMusterstrasse 123\n8000 Zürich",
        "billing_email_to": "billing@muster.example",
    }
    return src


class FakeTelegram:
    def __init__(self):
        self.messages: list[str] = []

    def notify(self, text):
        self.messages.append(text)
        return True


@pytest.fixture
def telegram():
    return FakeTelegram()


@pytest.fixture
def service(bexio, source):
    return BexioInvoiceSyncService(bexio=bexio, moco=source,
                                   subdomain="solar")


@pytest.fixture
def service_tg(bexio, source, telegram):
    return BexioInvoiceSyncService(bexio=bexio, moco=source,
                                   subdomain="solar",
                                   telegram=telegram)


def test_skips_when_status_is_not_sent(service, bexio, source):
    body = load_fixture("invoice_draft.json")
    result = service.sync(body)
    assert result == {"skipped": "status_not_sent", "status": "draft"}
    # No Bexio or source-Moco-project calls at all.
    assert bexio.calls == []
    assert source.calls == []


def test_status_not_sent_does_not_notify_telegram(service_tg, source, telegram):
    """The draft gate fires on every Moco edit — it must stay silent or it
    would spam the chat. Only no_customer (a genuine sync failure) notifies."""
    service_tg.sync(load_fixture("invoice_draft.json"))
    assert telegram.messages == []


def test_no_customer_skip_notifies_telegram(service_tg, source, telegram):
    """A sent invoice whose project has no resolvable customer is the invoice
    analogue of the expense no_company skip — it pings Telegram with the Moco
    invoice link."""
    body = load_fixture("invoice_sent.json")
    # Project resolves but carries no customer name.
    source.projects[body["project_id"]] = {"customer": {}, "labels": [],
                                            "billing_address": ""}

    result = service_tg.sync(body)

    assert result == {"skipped": "no_customer"}
    assert len(telegram.messages) == 1
    msg = telegram.messages[0]
    assert "Reason: No customer given" in msg
    assert f"solar.mocoapp.com/invoices/{body['id']}" in msg


def test_creates_invoice_and_transitions_to_open(service, bexio, source):
    """Happy path: contact found, account resolved from project labels,
    template fetched, invoice created, then issued."""
    bexio.contacts_by_name["Muster AG"] = [{"id": 6500}]
    # "Stromproduktion" wins over "Auftrag" in the iteration order (later wins).
    bexio.accounts_by_no["3010"] = [{"id": 4400, "tax_id": 21}]
    body = load_fixture("invoice_sent.json")

    result = service.sync(body)

    assert result == {"action": "created", "invoice_id": 12345,
                      "contact_id": 6500, "revenue_account_no": "3010"}

    methods = [c[0] for c in bexio.calls]
    # Order: search_contact -> search_account -> list_templates ->
    # create_invoice -> comment_invoice -> issue. `/set_pending` is NOT
    # called: Bexio's /issue alone moves DRAFT -> pending (Open), and the
    # `/2.0/kb_invoice/{id}/set_pending` endpoint 404s once the invoice
    # is no longer DRAFT (observed in prod).
    assert methods == [
        "search_contact_by_name", "search_account_by_no",
        "list_document_templates", "create_invoice", "comment_invoice",
        "issue_invoice",
    ]
    # Explicit: set_invoice_pending must NOT be called.
    assert not any(c[0] == "set_invoice_pending" for c in bexio.calls)

    create_call = next(c for c in bexio.calls if c[0] == "create_invoice")
    payload = create_call[1]
    assert payload["document_nr"] == "R240003"
    assert payload["contact_id"] == 6500
    assert payload["template_slug"] == "default-de"
    assert payload["api_reference"] == "R240003"
    assert payload["is_valid_from"] == "2024-12-04"
    assert payload["is_valid_to"] == "2025-01-03"
    # Newline in recipient address must be normalized to ", " (Bexio's manual
    # address field doesn't render newlines).
    assert "\n" not in payload["contact_address_manual"]
    assert payload["contact_address_manual"] == \
        "Muster AG, Musterstrasse 123, 12345 Musterstadt"
    # Header includes the Moco invoice URL for traceability.
    assert "solar.mocoapp.com/invoices/5064456" in payload["header"]
    # Single position with the resolved account + tax_id and net_total as
    # the unit price.
    assert payload["positions"] == [{
        "amount": "1", "unit_id": 1, "account_id": 4400, "tax_id": 21,
        "text": "R240003: Akonto – Muster-PVA 20kWp",
        "unit_price": "5000", "discount_in_percent": "0.000000",
        "type": "KbPositionCustom",
    }]

    # Cross-link comments on both sides.
    assert source.comments == [{
        "id": 5064456, "type": "Invoice",
        "text": "Rechnung in Bexio erstellt: "
                "https://office.bexio.com/index.php/kb_invoice/show/id/12345"
    }]
    comment_call = next(c for c in bexio.calls if c[0] == "comment_invoice")
    assert comment_call[1] == 12345
    assert "solar.mocoapp.com/invoices/5064456" in comment_call[2]["text"]


def test_creates_contact_from_project_billing_address_when_missing(
    service, bexio, source
):
    """No matching Bexio contact -> fetch source Moco company, build a
    contact payload that parses street/city from the project's
    billing_address (not the company address — n8n quirk we preserve)."""
    bexio.contacts_by_name = {}
    bexio.accounts_by_no["3010"] = [{"id": 4400, "tax_id": 21}]
    source.companies[762286146] = {
        "id": 762286146, "name": "Muster AG", "address": "Different head office\nOther 1\n9999 Otherton",
        "email": "hello@muster.example", "phone": "+41", "website": "https://muster.example",
    }
    body = load_fixture("invoice_sent.json")

    service.sync(body)

    create_contact_call = next(c for c in bexio.calls if c[0] == "create_contact")
    payload = create_contact_call[1]
    # Address parsed from PROJECT billing_address, not the company head office.
    assert payload["street_name"] == "Musterstrasse"
    assert payload["house_number"] == "123"
    assert payload["postcode"] == "8000"
    assert payload["city"] == "Zürich"
    # billing_email_to on project wins over company email.
    assert payload["mail"] == "billing@muster.example"


def test_state_transition_failure_does_not_fail_the_sync(service, bexio, source):
    """If /issue fails after a successful create, the invoice is still
    created — the failure is logged but the response stays 200 so Moco
    doesn't keep retrying and creating duplicates."""
    bexio.contacts_by_name["Muster AG"] = [{"id": 6500}]
    bexio.accounts_by_no["3010"] = [{"id": 4400, "tax_id": 21}]

    def boom(*_):
        raise RuntimeError("bexio /issue is being moody")
    bexio.issue_invoice = boom

    body = load_fixture("invoice_sent.json")
    result = service.sync(body)
    assert result["action"] == "created"
    assert result["invoice_id"] == 12345


def test_resolve_revenue_account_orders_match_n8n_chain():
    """Later labels in INVOICE_REVENUE_ACCOUNT_BY_LABEL win over earlier ones,
    mirroring n8n's sequential `if` chain. With both Auftrag and
    Stromproduktion present, n8n's chain ends up at "Auftrag" because
    Stromproduktion comes before Auftrag in the if-cascade. Verify our
    iteration produces the same result."""
    # In our list order, "Auftrag" appears AFTER "Stromproduktion", so it
    # wins, matching n8n.
    assert resolve_revenue_account_no(["Stromproduktion", "Auftrag"]) == "3210"
    # Specific labels still beat the default.
    assert resolve_revenue_account_no(["Wartung"]) == "3450"
    # No matches -> default revenue account.
    assert resolve_revenue_account_no(["unknown"]) == "3210"


def test_invoice_payload_omits_fields_bexio_rejects(service, bexio, source):
    """Regression: Bexio's POST /2.0/kb_invoice strict-rejects unknown form
    fields with 422 "Unexpected extra form field named …". The n8n workflow
    only sends `user_id`; `owner_id` (which we set on the contact payload)
    is NOT a valid kb_invoice field and must stay out of the create payload."""
    bexio.contacts_by_name["Muster AG"] = [{"id": 6500}]
    bexio.accounts_by_no["3010"] = [{"id": 4400, "tax_id": 21}]
    body = load_fixture("invoice_sent.json")

    service.sync(body)

    payload = next(c for c in bexio.calls if c[0] == "create_invoice")[1]
    assert "owner_id" not in payload
    # The valid user field is still set.
    assert payload["user_id"] == 2


def test_account_lookup_miss_falls_back_to_default_tax_id(service, bexio, source):
    """Bexio returns no match for the chosen revenue account -> the invoice
    still gets created using a None account_id and the DEFAULT_TAX_ID
    constant (10), surfacing the issue without blocking the sync."""
    bexio.contacts_by_name["Muster AG"] = [{"id": 6500}]
    body = load_fixture("invoice_sent.json")

    service.sync(body)

    payload = next(c for c in bexio.calls if c[0] == "create_invoice")[1]
    assert payload["positions"][0]["account_id"] is None
    assert payload["positions"][0]["tax_id"] == 10
