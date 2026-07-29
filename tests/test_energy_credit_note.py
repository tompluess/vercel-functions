"""Unit tests for EnergyCreditNoteService — detection (`is_energy_credit_note`),
expense/invoice payload shape, quarter-label formatting, VAT-code
resolution, keep-draft paths, draft deletion, error propagation, and the
`SupplierInvoiceOcrService` dispatch point. In-memory fakes for all
collaborators."""

import base64
import io
from datetime import date, timedelta
from urllib import error as urlerror

import pytest

from api.anthropic_ocr_client import EnergyCreditNoteData, InvoiceData
from api.energy_credit_note_service import (
    COMMENTABLE_TYPE_DRAFT,
    EVU_TAG,
    EnergyCreditNoteService,
    is_energy_credit_note,
)
from api.stromproduktion_project_matcher import StromproduktionProjectMatcher
from api.supplier_invoice_ocr_service import SupplierInvoiceOcrService


PDF_BYTES = b"%PDF-1.4 energy-credit-note-test"

# Mirrors the real draft 3143995 (CKW AG statement for Meierhofweg 10) —
# see specs/SPEC_energy_credit_note.md.
DRAFT_BODY = {
    "id": 3143995,
    "title": "260731 CKW Meierhofweg10 Rechnung  600 949 594.pdf",
    "file_url": "https://data.mocoapp.com/objects/fake.pdf?sig=abc",
}

# The matched *supplier* company record — a DIFFERENT Moco company than
# the project's customer (see PROJECTS below), same real-world entity,
# both tagged EVU_TAG. This is the record `MocoSupplierMatcher` links.
COMPANY = {"id": 762378104, "name": "CKW AG (Lieferant)", "tags": [EVU_TAG]}

PROJECTS = [
    {"id": 947264448, "name": "Meierhofweg10_Emmen Contracting/Einspeisung",
     "tags": ["Contracting", "Stromproduktion"],
     "customer": {"id": 762378092, "name": "CKW AG"},
     "billing_address": "CKW AG\nTäschmattstrasse 4\n6015 Luzern\nSchweiz",
     "custom_properties": {"Kommission": None}},
    {"id": 947264459, "name": "Lindershalde_Rengg Contracting/Einspeisung",
     "tags": ["Contracting", "Stromproduktion"],
     "customer": {"id": 762378092, "name": "CKW AG"},
     "billing_address": "CKW AG\nTäschmattstrasse 4\n6015 Luzern\nSchweiz",
     "custom_properties": {"Kommission": None}},
]


def make_credit(**overrides) -> EnergyCreditNoteData:
    base = dict(
        objekt="Produktion PVA HEIV Meierhofweg 10",
        net_amount=3580.58,
        vat_rate=0.081,
        period_from="2026-04-01",
        period_to="2026-06-30",
        invoice_date="2026-07-31",
        invoice_number="600949594",
        confidence=0.93,
    )
    base.update(overrides)
    return EnergyCreditNoteData(**base)


def make_invoice(**overrides) -> InvoiceData:
    base = dict(
        supplier_name="CKW AG (Lieferant)",
        supplier_address=None,
        invoice_date="2026-07-31",
        due_date=None,
        invoice_number="600949594",
        total_amount=3785.65,
        net_amount=None,
        vat_amount=None,
        vat_rate=None,
        currency="CHF",
        iban=None,
        qr_reference=None,
        creditor_reference=None,
        payment_purpose=None,
        description=None,
        is_credit_note=True,
        commission=None,
        delivery_address=None,
        already_paid_by_card=False,
        confidence=0.9,
    )
    base.update(overrides)
    return InvoiceData(**base)


# --- fakes ------------------------------------------------------------------

class FakeMoco:
    def __init__(self):
        self.expenses: list[tuple[int, dict]] = []
        self.expense_error: Exception | None = None
        self.next_expense_id = 5187500
        self.comments: list[dict] = []
        self.comment_error: Exception | None = None

    def create_project_expense(self, project_id: int, payload: dict) -> dict:
        if self.expense_error:
            raise self.expense_error
        self.expenses.append((project_id, payload))
        return {"id": self.next_expense_id, **payload}

    def post_comment(self, *, commentable_id: int, commentable_type: str,
                     text: str) -> dict:
        if self.comment_error:
            raise self.comment_error
        self.comments.append({"commentable_id": commentable_id,
                              "commentable_type": commentable_type,
                              "text": text})
        return {"id": 1}


class FakeMocoInvoices:
    def __init__(self):
        # Mirrors the real /vat_code_sales shape pulled live from the account.
        self.vat_codes: list[dict] = [
            {"id": 107816, "tax": 8.1, "active": True},
            {"id": 107817, "tax": 2.6, "active": True},
            {"id": 107819, "tax": 0.0, "active": True},
        ]
        self.vat_codes_error: Exception | None = None
        self.invoices: list[dict] = []
        self.next_invoice_id = 7900001
        self.create_invoice_error: Exception | None = None
        self.attachments: list[tuple[int, str, str]] = []
        self.add_attachment_error: Exception | None = None

    def list_vat_code_sales(self) -> list[dict]:
        if self.vat_codes_error:
            raise self.vat_codes_error
        return self.vat_codes

    def create_invoice(self, payload: dict) -> dict:
        if self.create_invoice_error:
            raise self.create_invoice_error
        self.invoices.append(payload)
        invoice_id = self.next_invoice_id
        self.next_invoice_id += 1
        return {"id": invoice_id, **payload}

    def add_attachment(self, invoice_id: int, *, filename: str,
                       base64_content: str) -> dict:
        if self.add_attachment_error:
            raise self.add_attachment_error
        self.attachments.append((invoice_id, filename, base64_content))
        return {"id": 1}


class FakePurchaseClient:
    def __init__(self):
        self.deleted_drafts: list[int] = []
        self.delete_draft_error: Exception | None = None

    def delete_purchase_draft(self, draft_id: int) -> None:
        if self.delete_draft_error:
            raise self.delete_draft_error
        self.deleted_drafts.append(draft_id)


class FakeOcr:
    def __init__(self, result: EnergyCreditNoteData | None = None,
                 error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[bytes] = []

    def extract_energy_credit_note(self, pdf_bytes: bytes) -> EnergyCreditNoteData:
        self.calls.append(pdf_bytes)
        if self.error:
            raise self.error
        return self.result


class FakeTelegram:
    def __init__(self):
        self.messages: list[str] = []

    def notify(self, text: str) -> bool:
        self.messages.append(text)
        return True


def build_service(*, moco=None, moco_invoices=None, purchases=None, ocr=None,
                  telegram=None, projects=None, subdomain="solar"):
    return EnergyCreditNoteService(
        moco=moco or FakeMoco(),
        moco_invoices=moco_invoices or FakeMocoInvoices(),
        purchase_client=purchases or FakePurchaseClient(),
        ocr=ocr or FakeOcr(result=make_credit()),
        matcher=StromproduktionProjectMatcher(
            PROJECTS if projects is None else projects),
        subdomain=subdomain,
        telegram=telegram,
    )


def _http_error(code: int, body: bytes = b"boom") -> urlerror.HTTPError:
    return urlerror.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


# --- detection ---------------------------------------------------------------

def test_is_energy_credit_note_true_when_credit_note_and_evu_tagged():
    assert is_energy_credit_note(make_invoice(is_credit_note=True), COMPANY) is True


def test_is_energy_credit_note_false_when_not_credit_note():
    assert is_energy_credit_note(make_invoice(is_credit_note=False), COMPANY) is False


def test_is_energy_credit_note_false_when_no_company():
    assert is_energy_credit_note(make_invoice(is_credit_note=True), None) is False


def test_is_energy_credit_note_false_when_company_not_evu_tagged():
    company = {"id": 1, "name": "Irgendein Lieferant", "tags": ["Sonstiges"]}
    assert is_energy_credit_note(make_invoice(is_credit_note=True), company) is False


def test_is_energy_credit_note_tag_check_is_case_insensitive():
    company = {"id": 1, "name": "X",
               "tags": ["lokaler energieversorger (evu)"]}
    assert is_energy_credit_note(make_invoice(is_credit_note=True), company) is True


def test_is_energy_credit_note_true_despite_different_company_id_than_project_customer():
    """The real CKW case: the matched *supplier* company record (id
    762378104) differs from the project's *customer* record (id
    762378092) — detection only cares about the supplier's own tags, not
    the eventual project match."""
    assert is_energy_credit_note(make_invoice(is_credit_note=True), COMPANY) is True


# --- happy path ---------------------------------------------------------------

def test_happy_path_creates_expense_and_invoice():
    moco = FakeMoco()
    moco_invoices = FakeMocoInvoices()
    purchases = FakePurchaseClient()
    tg = FakeTelegram()
    s = build_service(moco=moco, moco_invoices=moco_invoices,
                      purchases=purchases, telegram=tg)

    result = s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(),
                       company=COMPANY, draft_id=3143995, body=DRAFT_BODY)

    assert result["energy_credit_note"] is True
    assert result["expense_id"] == 5187500
    assert result["project_id"] == 947264448
    assert result["project_name"] == "Meierhofweg10_Emmen Contracting/Einspeisung"
    assert result["leistungszeitraum"] == "2026/Q2"
    assert result["net_amount"] == 3580.58

    project_id, expense_payload = moco.expenses[0]
    assert project_id == 947264448
    assert expense_payload["title"] == "Stromproduktion 2026/Q2"
    assert expense_payload["quantity"] == 1
    assert expense_payload["unit"] == "x"
    assert expense_payload["unit_price"] == 3580.58
    assert expense_payload["unit_cost"] == 0
    assert expense_payload["billable"] is True
    assert expense_payload["budget_relevant"] is False
    assert expense_payload["service_period_from"] == "2026-04-01"
    assert expense_payload["service_period_to"] == "2026-06-30"
    assert base64.b64decode(expense_payload["file"]["base64"]) == PDF_BYTES

    invoice_payload = moco_invoices.invoices[0]
    assert invoice_payload["status"] == "created"
    assert invoice_payload["customer_id"] == 762378092
    assert invoice_payload["project_id"] == 947264448
    assert invoice_payload["recipient_address"] == (
        "CKW AG\nTäschmattstrasse 4\n6015 Luzern\nSchweiz")
    assert invoice_payload["title"] == (
        "Stromproduktion 2026/Q2 – Meierhofweg10_Emmen Contracting/Einspeisung")
    assert invoice_payload["currency"] == "CHF"
    assert invoice_payload["tags"] == ["Stromproduktion"]
    assert invoice_payload["vat_code_id"] == 107816
    item = invoice_payload["items"][0]
    assert item["type"] == "item"
    assert item["title"] == "Stromproduktion 2026/Q2 (04 – 06/2026)"
    assert item["quantity"] == 1
    assert item["unit"] == "x"
    assert item["unit_price"] == 3580.58
    assert item["expense_ids"] == [5187500]
    d = date.fromisoformat(invoice_payload["date"])
    assert date.fromisoformat(invoice_payload["due_date"]) == d + timedelta(days=30)

    assert moco_invoices.attachments[0][0] == result["invoice_id"]
    assert base64.b64decode(moco_invoices.attachments[0][2]) == PDF_BYTES

    # Invoice stays at "created" — never transitioned to "sent" (decision D2).
    assert "sent" not in {invoice_payload.get("status")}

    assert purchases.deleted_drafts == [3143995]
    assert len(tg.messages) == 1
    assert "verbucht" in tg.messages[0]
    assert "versendet" in tg.messages[0]
    assert moco.comments == []


def test_low_confidence_success_flags_review_in_telegram():
    tg = FakeTelegram()
    ocr = FakeOcr(result=make_credit(confidence=0.4))
    s = build_service(ocr=ocr, telegram=tg)
    s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(), company=COMPANY,
             draft_id=3143995, body=DRAFT_BODY)
    assert "⚠️" in tg.messages[0]
    assert "bitte prüfen" in tg.messages[0]


# --- quarter-label formatting -------------------------------------------------

@pytest.mark.parametrize("period_from,period_to,expected", [
    ("2026-01-15", "2026-03-31", "2026/Q1"),
    ("2026-04-01", "2026-06-30", "2026/Q2"),
    ("2026-07-01", "2026-09-30", "2026/Q3"),
    ("2026-10-01", "2026-12-31", "2026/Q4"),
])
def test_leistungszeitraum_quarter_boundaries(period_from, period_to, expected):
    ocr = FakeOcr(result=make_credit(period_from=period_from,
                                     period_to=period_to))
    s = build_service(ocr=ocr)
    result = s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(),
                       company=COMPANY, draft_id=1, body=DRAFT_BODY)
    assert result["leistungszeitraum"] == expected


# --- keep-draft paths ---------------------------------------------------------

def test_no_matching_project_keeps_draft():
    moco = FakeMoco()
    moco_invoices = FakeMocoInvoices()
    purchases = FakePurchaseClient()
    tg = FakeTelegram()
    ocr = FakeOcr(result=make_credit(objekt="Solarpark Zermatt"))
    s = build_service(moco=moco, moco_invoices=moco_invoices,
                      purchases=purchases, ocr=ocr, telegram=tg)

    result = s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(),
                       company=COMPANY, draft_id=3143995, body=DRAFT_BODY)

    assert result["skipped"] == "energy_credit_note_project_no_match"
    assert moco.expenses == []
    assert moco_invoices.invoices == []
    assert purchases.deleted_drafts == []
    assert len(tg.messages) == 1
    assert "nicht verbucht" in tg.messages[0]
    comment = moco.comments[0]
    assert comment["commentable_id"] == 3143995
    assert comment["commentable_type"] == COMMENTABLE_TYPE_DRAFT


def test_unrelated_supplier_never_falls_back_to_other_evus_project():
    """A supplier with no Stromproduktion project of its own must never
    be routed onto an unrelated EVU's project."""
    ocr = FakeOcr(result=make_credit())
    invoice = make_invoice(supplier_name="Irgendein Anderer EVU AG")
    s = build_service(ocr=ocr)
    result = s.process(pdf_bytes=PDF_BYTES, invoice=invoice, company=COMPANY,
                       draft_id=1, body=DRAFT_BODY)
    assert result["skipped"] == "energy_credit_note_project_no_match"


def test_ambiguous_project_reports_candidate_count():
    projects = [
        {"id": 1, "name": "Blumenrain 1 Contracting/Einspeisung",
         "tags": ["Stromproduktion"], "customer": {"id": 1, "name": "CKW AG"}},
        {"id": 2, "name": "Blumenrain 3 Contracting/Einspeisung",
         "tags": ["Stromproduktion"], "customer": {"id": 1, "name": "CKW AG"}},
    ]
    tg = FakeTelegram()
    ocr = FakeOcr(result=make_credit(objekt="Produktion Blumenrain"))
    s = build_service(ocr=ocr, projects=projects, telegram=tg)
    result = s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(),
                       company=COMPANY, draft_id=1, body=DRAFT_BODY)
    assert result["skipped"] == "energy_credit_note_project_ambiguous"
    assert result["match_status"] == "ambiguous"
    assert "2 Projekte" in tg.messages[0]


def test_missing_net_amount_keeps_draft():
    moco = FakeMoco()
    tg = FakeTelegram()
    ocr = FakeOcr(result=make_credit(net_amount=None))
    s = build_service(moco=moco, ocr=ocr, telegram=tg)
    result = s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(),
                       company=COMPANY, draft_id=1, body=DRAFT_BODY)
    assert result["skipped"] == "energy_credit_note_no_net_amount"
    assert moco.expenses == []
    assert "Netto-Betrag" in tg.messages[0]


def test_missing_period_keeps_draft():
    moco = FakeMoco()
    tg = FakeTelegram()
    ocr = FakeOcr(result=make_credit(period_to=None))
    s = build_service(moco=moco, ocr=ocr, telegram=tg)
    result = s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(),
                       company=COMPANY, draft_id=1, body=DRAFT_BODY)
    assert result["skipped"] == "energy_credit_note_no_period"
    assert moco.expenses == []


def test_unparseable_period_from_keeps_draft():
    moco = FakeMoco()
    ocr = FakeOcr(result=make_credit(period_from="not-a-date"))
    s = build_service(moco=moco, ocr=ocr)
    result = s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(),
                       company=COMPANY, draft_id=1, body=DRAFT_BODY)
    assert result["skipped"] == "energy_credit_note_no_period"
    assert moco.expenses == []


def test_failed_draft_comment_is_swallowed():
    moco = FakeMoco()
    moco.comment_error = _http_error(422)
    tg = FakeTelegram()
    ocr = FakeOcr(result=make_credit(objekt="Solarpark Zermatt"))
    s = build_service(moco=moco, ocr=ocr, telegram=tg)
    result = s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(),
                       company=COMPANY, draft_id=1, body=DRAFT_BODY)
    assert result["skipped"] == "energy_credit_note_project_no_match"
    assert len(tg.messages) == 1


# --- VAT-code resolution -------------------------------------------------------

def test_vat_code_resolved_from_ocr_rate():
    moco_invoices = FakeMocoInvoices()
    ocr = FakeOcr(result=make_credit(vat_rate=0.026))
    s = build_service(moco_invoices=moco_invoices, ocr=ocr)
    s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(), company=COMPANY,
             draft_id=1, body=DRAFT_BODY)
    assert moco_invoices.invoices[0]["vat_code_id"] == 107817


def test_vat_code_falls_back_to_account_standard_when_ocr_rate_missing():
    moco_invoices = FakeMocoInvoices()
    ocr = FakeOcr(result=make_credit(vat_rate=None))
    s = build_service(moco_invoices=moco_invoices, ocr=ocr)
    s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(), company=COMPANY,
             draft_id=1, body=DRAFT_BODY)
    assert moco_invoices.invoices[0]["vat_code_id"] == 107816


def test_vat_code_falls_back_to_first_active_when_no_standard_rate_present():
    moco_invoices = FakeMocoInvoices()
    moco_invoices.vat_codes = [{"id": 999, "tax": 3.7, "active": True}]
    ocr = FakeOcr(result=make_credit(vat_rate=None))
    s = build_service(moco_invoices=moco_invoices, ocr=ocr)
    s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(), company=COMPANY,
             draft_id=1, body=DRAFT_BODY)
    assert moco_invoices.invoices[0]["vat_code_id"] == 999


def test_vat_code_omitted_when_list_fetch_fails():
    moco_invoices = FakeMocoInvoices()
    moco_invoices.vat_codes_error = _http_error(500)
    ocr = FakeOcr(result=make_credit())
    s = build_service(moco_invoices=moco_invoices, ocr=ocr)
    s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(), company=COMPANY,
             draft_id=1, body=DRAFT_BODY)
    assert "vat_code_id" not in moco_invoices.invoices[0]


# --- error propagation ---------------------------------------------------------

def test_expense_create_http_error_propagates():
    moco = FakeMoco()
    moco.expense_error = _http_error(422, b'{"base":["error"]}')
    purchases = FakePurchaseClient()
    s = build_service(moco=moco, purchases=purchases)
    with pytest.raises(urlerror.HTTPError):
        s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(), company=COMPANY,
                 draft_id=1, body=DRAFT_BODY)
    assert purchases.deleted_drafts == []


def test_invoice_create_http_error_propagates():
    moco_invoices = FakeMocoInvoices()
    moco_invoices.create_invoice_error = _http_error(422)
    purchases = FakePurchaseClient()
    s = build_service(moco_invoices=moco_invoices, purchases=purchases)
    with pytest.raises(urlerror.HTTPError):
        s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(), company=COMPANY,
                 draft_id=1, body=DRAFT_BODY)
    assert purchases.deleted_drafts == []


def test_attachment_http_error_propagates():
    moco_invoices = FakeMocoInvoices()
    moco_invoices.add_attachment_error = _http_error(422)
    purchases = FakePurchaseClient()
    s = build_service(moco_invoices=moco_invoices, purchases=purchases)
    with pytest.raises(urlerror.HTTPError):
        s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(), company=COMPANY,
                 draft_id=1, body=DRAFT_BODY)
    assert purchases.deleted_drafts == []


# --- draft deletion edge cases --------------------------------------------------

def test_draft_delete_404_is_silent():
    purchases = FakePurchaseClient()
    purchases.delete_draft_error = _http_error(404)
    tg = FakeTelegram()
    s = build_service(purchases=purchases, telegram=tg)
    result = s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(),
                       company=COMPANY, draft_id=1, body=DRAFT_BODY)
    assert result["invoice_id"] is not None
    assert len(tg.messages) == 1
    assert "verbucht" in tg.messages[0]


def test_draft_delete_failure_alerts_but_result_stays_ok():
    purchases = FakePurchaseClient()
    purchases.delete_draft_error = _http_error(500, b"oops")
    tg = FakeTelegram()
    s = build_service(purchases=purchases, telegram=tg)
    result = s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(),
                       company=COMPANY, draft_id=1, body=DRAFT_BODY)
    assert result["invoice_id"] is not None
    delete_alerts = [m for m in tg.messages if "nicht gelöscht" in m]
    assert len(delete_alerts) == 1
    assert "invoices/" in delete_alerts[0]


def test_works_without_telegram():
    """telegram=None (unit-test convenience + defensive prod default)."""
    s = build_service(telegram=None)
    result = s.process(pdf_bytes=PDF_BYTES, invoice=make_invoice(),
                       company=COMPANY, draft_id=1, body=DRAFT_BODY)
    assert result["invoice_id"] is not None


# --- SupplierInvoiceOcrService dispatch ---------------------------------------

class DispatchFakeMoco:
    def __init__(self, pdf_bytes: bytes = PDF_BYTES):
        self.pdf_bytes = pdf_bytes
        self.suppliers: list[dict] = []
        self.companies: dict[int, dict] = {}

    def download_file(self, signed_url: str) -> bytes:
        return self.pdf_bytes

    def list_suppliers(self, *, limit: int = 1000) -> list[dict]:
        return self.suppliers

    def get_company(self, company_id: int) -> dict:
        return self.companies.get(company_id, {"id": company_id})


class DispatchFakePurchaseClient:
    def __init__(self):
        self.creates: list[dict] = []
        self.deleted_drafts: list[int] = []

    def list_vat_codes(self) -> list[dict]:
        return []

    def create_purchase(self, payload: dict) -> dict:
        self.creates.append(payload)
        return {"id": 4001234, "items": []}

    def delete_purchase_draft(self, draft_id: int) -> None:
        self.deleted_drafts.append(draft_id)

    def post_comment(self, purchase_id: int, text: str) -> dict:
        return {"id": 1}

    def assign_item_to_project(self, *args, **kwargs) -> dict:
        return {"id": 1}


class DispatchFakeOcr:
    def __init__(self, result: InvoiceData):
        self.result = result
        self.calls: list[bytes] = []

    def extract(self, pdf_bytes: bytes) -> InvoiceData:
        self.calls.append(pdf_bytes)
        return self.result


class FakeEnergyCreditNoteService:
    def __init__(self, *, http_error: Exception | None = None):
        self.calls: list[dict] = []
        self.http_error = http_error

    def process(self, *, pdf_bytes, invoice, company, draft_id, body) -> dict:
        self.calls.append({"pdf_bytes": pdf_bytes, "invoice": invoice,
                           "company": company, "draft_id": draft_id,
                           "body": body})
        if self.http_error:
            raise self.http_error
        return {"energy_credit_note": True, "draft_id": draft_id,
                "invoice_id": 7900001}


def test_energy_credit_note_draft_is_delegated_not_purchased():
    moco = DispatchFakeMoco()
    moco.suppliers = [COMPANY]
    moco.companies = {COMPANY["id"]: COMPANY}
    purchases = DispatchFakePurchaseClient()
    ocr = DispatchFakeOcr(make_invoice(supplier_name="CKW AG (Lieferant)",
                                       is_credit_note=True))
    ecn = FakeEnergyCreditNoteService()
    s = SupplierInvoiceOcrService(
        moco=moco, purchase_client=purchases, ocr=ocr, subdomain="solar",
        energy_credit_note=ecn)

    result = s.process("create", DRAFT_BODY)

    assert result == {"energy_credit_note": True, "draft_id": 3143995,
                      "invoice_id": 7900001}
    assert len(ecn.calls) == 1
    assert ecn.calls[0]["draft_id"] == 3143995
    assert ecn.calls[0]["company"]["id"] == COMPANY["id"]
    # The generic OCR→purchase path never ran.
    assert purchases.creates == []


def test_non_energy_credit_note_falls_through_to_generic_purchase_path():
    moco = DispatchFakeMoco()
    purchases = DispatchFakePurchaseClient()
    ocr = DispatchFakeOcr(make_invoice(supplier_name="FLYERALARM",
                                       is_credit_note=False))
    ecn = FakeEnergyCreditNoteService()
    s = SupplierInvoiceOcrService(
        moco=moco, purchase_client=purchases, ocr=ocr, subdomain="solar",
        energy_credit_note=ecn)

    s.process("create", DRAFT_BODY)

    assert ecn.calls == []
    assert len(purchases.creates) == 1


def test_energy_credit_note_without_service_falls_through():
    """energy_credit_note=None (default) — legacy behavior on the same body."""
    moco = DispatchFakeMoco()
    moco.suppliers = [COMPANY]
    moco.companies = {COMPANY["id"]: COMPANY}
    purchases = DispatchFakePurchaseClient()
    ocr = DispatchFakeOcr(make_invoice(supplier_name="CKW AG (Lieferant)",
                                       is_credit_note=True))
    s = SupplierInvoiceOcrService(
        moco=moco, purchase_client=purchases, ocr=ocr, subdomain="solar")

    result = s.process("create", DRAFT_BODY)

    assert "energy_credit_note" not in result
    assert len(purchases.creates) == 1


def test_energy_credit_note_4xx_propagates_not_swallowed_as_moco_rejected():
    """The energy-credit-note branch's own HTTPErrors must reach
    index.py's standard 4xx/5xx mapping, not this function's
    purchase-specific duplicate-receipt swallow (see the
    `in_energy_credit_note_branch` flag in supplier_invoice_ocr_service.py)."""
    moco = DispatchFakeMoco()
    moco.suppliers = [COMPANY]
    moco.companies = {COMPANY["id"]: COMPANY}
    purchases = DispatchFakePurchaseClient()
    ocr = DispatchFakeOcr(make_invoice(supplier_name="CKW AG (Lieferant)",
                                       is_credit_note=True))
    ecn = FakeEnergyCreditNoteService(http_error=_http_error(422))
    s = SupplierInvoiceOcrService(
        moco=moco, purchase_client=purchases, ocr=ocr, subdomain="solar",
        energy_credit_note=ecn)

    with pytest.raises(urlerror.HTTPError):
        s.process("create", DRAFT_BODY)
