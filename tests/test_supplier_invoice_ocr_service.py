"""Unit tests for SupplierInvoiceOcrService — payload shape, skip gates,
supplier lookup branches, comment text, and confidence-routed Telegram
alerts. In-memory fakes for all collaborators."""

import base64
import io
import pathlib
from urllib import error as urlerror

import pytest

from api.anthropic_ocr_client import AnthropicOcrError, InvoiceData
from api.moco_category_resolver import MocoCategoryResolver
from api.moco_project_resolver import MocoProjectResolver
from api.purchase_review_gate import OCR_TAG, REVIEW_PENDING_TAG
from api.supplier_invoice_ocr_service import (
    CONFIDENCE_THRESHOLD,
    SupplierInvoiceOcrService,
    _format_manual_upload_comment,
    _format_ocr_comment,
)


def make_invoice(**overrides) -> InvoiceData:
    """Reasonable defaults so each test overrides only the relevant fields."""
    base = dict(
        supplier_name="FLYERALARM",
        supplier_address="Alfred-Nobel-Str. 18, 97080 Würzburg",
        invoice_date="2026-05-12",
        due_date="2026-06-11",
        invoice_number="R-2026-042",
        total_amount=1234.50,
        net_amount=1142.00,
        vat_amount=92.50,
        vat_rate=0.081,
        currency="CHF",
        # QR-IBAN — IID 31999 in positions 5-9, so the QR-ESR payment
        # method branch fires. Non-QR-IBANs are exercised in dedicated tests.
        iban="CH4431999123000889012",
        qr_reference="210000000003139471430009017",
        creditor_reference=None,
        payment_purpose="Rechnung Mai 2026",
        description="Solarmodule und Montage",
        position_title=None,
        is_credit_note=False,
        commission=None,
        delivery_address=None,
        already_paid_by_card=False,
        confidence=0.92,
    )
    base.update(overrides)
    return InvoiceData(**base)


# --- fakes ------------------------------------------------------------------

class FakeMoco:
    def __init__(self, pdf_bytes: bytes = b"%PDF-fake"):
        self.pdf_bytes = pdf_bytes
        self.downloads: list[str] = []
        self.download_error: Exception | None = None
        self.supplier_list_calls: int = 0
        self.suppliers: list[dict] = []
        self.suppliers_error: Exception | None = None
        # get_company is now also exercised — for the VAT-supplier-default
        # fallback path. Default: keyed by id, returns whatever was set.
        self.companies: dict[int, dict] = {}
        self.get_company_error: Exception | None = None

    def download_file(self, signed_url: str) -> bytes:
        self.downloads.append(signed_url)
        if self.download_error:
            raise self.download_error
        return self.pdf_bytes

    def list_suppliers(self, *, limit: int = 1000) -> list[dict]:
        self.supplier_list_calls += 1
        if self.suppliers_error:
            raise self.suppliers_error
        return self.suppliers

    def get_company(self, company_id: int) -> dict:
        if self.get_company_error:
            raise self.get_company_error
        return self.companies.get(company_id, {"id": company_id})


class FakePurchaseClient:
    def __init__(self):
        self.creates: list[dict] = []
        # Bodies handed back by `get_purchase`, keyed by id — the Bexio
        # hand-off re-fetches rather than reusing the create response.
        self.purchases_by_id: dict[int, dict] = {}
        self.get_purchase_error: Exception | None = None
        self.next_create_id: int = 4001234
        self.next_item_id: int = 311936153
        self.create_error: Exception | None = None
        self.comments: list[tuple[int, str]] = []
        self.comment_error: Exception | None = None
        # Per-call failures, consumed in order — one entry per
        # `post_comment`, None meaning "this one succeeds". Lets a test
        # fail the provenance comment while the OCR summary still posts,
        # which `comment_error` (all-or-nothing) can't express.
        self.comment_errors: list[Exception | None] = []
        self.deleted_drafts: list[int] = []
        self.delete_draft_error: Exception | None = None
        # Each entry: dict of kwargs passed to assign_item_to_project.
        self.assigns: list[dict] = []
        # When set, the assign call raises this. Use a list so individual
        # per-item assignments can fail independently (one entry per call,
        # consumed in order; None means succeed). When the list is empty
        # every call succeeds.
        self.assign_errors: list[Exception | None] = []
        # Default vat-code list covers the typical Swiss rates so most
        # tests don't need to override it. Shape mirrors the real Moco
        # /vat_code_purchases response: id, tax (in percent), code, active.
        self.vat_codes: list[dict] = [
            {"id": 11, "tax": 8.1, "code": "1", "active": True},
            {"id": 12, "tax": 2.6, "code": "2", "active": True},
            {"id": 13, "tax": 0.0, "code": "0", "active": True},
        ]
        self.vat_codes_error: Exception | None = None
        # Each entry: kwargs passed to create_payment.
        self.payments: list[dict] = []
        self.payment_error: Exception | None = None
        self.next_payment_id: int = 90001
        # When set, the create response carries this `gross_total`, the way
        # Moco recomputes it from the line item + VAT code. Left None by
        # default so the OCR-total fallback is what most tests exercise.
        self.create_gross_total: float | None = None

    def get_purchase(self, purchase_id: int) -> dict:
        if self.get_purchase_error:
            raise self.get_purchase_error
        return self.purchases_by_id.get(purchase_id,
                                        {"id": purchase_id, "tags": ["Auto"]})

    def list_vat_codes(self) -> list[dict]:
        if self.vat_codes_error:
            raise self.vat_codes_error
        return self.vat_codes

    def create_purchase(self, payload: dict) -> dict:
        if self.create_error:
            raise self.create_error
        self.creates.append(payload)
        # Moco returns each item with a server-assigned id. Mirror that:
        # the service's project-assign step iterates `items` and reads
        # `item.id`, so the fake has to supply realistic ids on create.
        echoed_items: list[dict] = []
        for raw_item in payload.get("items") or []:
            echoed = dict(raw_item)
            echoed["id"] = self.next_item_id
            self.next_item_id += 1
            echoed_items.append(echoed)
        echo = {**payload, "items": echoed_items}
        created = {"id": self.next_create_id, **echo}
        if self.create_gross_total is not None:
            created["gross_total"] = self.create_gross_total
        return created

    def create_payment(self, *, purchase_id: int, date: str,
                       total: float) -> dict:
        if self.payment_error:
            raise self.payment_error
        self.payments.append({"purchase_id": purchase_id, "date": date,
                              "total": total})
        payment_id = self.next_payment_id
        self.next_payment_id += 1
        return {"id": payment_id}

    def delete_purchase_draft(self, draft_id: int) -> None:
        if self.delete_draft_error:
            raise self.delete_draft_error
        self.deleted_drafts.append(draft_id)

    def post_comment(self, purchase_id: int, text: str) -> dict:
        if self.comment_error:
            raise self.comment_error
        if self.comment_errors:
            err = self.comment_errors.pop(0)
            if err is not None:
                raise err
        self.comments.append((purchase_id, text))
        return {"id": 1}

    def assign_item_to_project(self, purchase_id: int, item_id: int, *,
                                project_id: int, notify_project_leader: bool,
                                billable: bool, budget_relevant: bool,
                                surcharge: bool,
                                expense_id: int | None = None) -> dict:
        # Pop one error (if any) from the queue per call so tests can fail
        # individual items selectively. An empty queue → all succeed.
        err = self.assign_errors.pop(0) if self.assign_errors else None
        self.assigns.append({
            "purchase_id": purchase_id, "item_id": item_id,
            "project_id": project_id,
            "notify_project_leader": notify_project_leader,
            "billable": billable, "budget_relevant": budget_relevant,
            "surcharge": surcharge, "expense_id": expense_id,
        })
        if err is not None:
            raise err
        return {"id": 7655423}


class FakeOcr:
    def __init__(self, result: InvoiceData | None = None,
                 error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[bytes] = []
        # Operator subjects seen per `extract` call, so tests can assert
        # the manual-upload subject actually reaches the model.
        self.subjects: list[str | None] = []

    def extract(self, pdf_bytes: bytes, *,
                subject: str | None = None) -> InvoiceData:
        self.calls.append(pdf_bytes)
        self.subjects.append(subject)
        if self.error:
            raise self.error
        return self.result


class FakeTelegram:
    def __init__(self):
        self.messages: list[str] = []

    def notify(self, text: str) -> bool:
        self.messages.append(text)
        return True


class FakeBexioExpense:
    """Stands in for `BexioExpenseSyncService` in the hand-off."""

    def __init__(self, result=None, error: Exception | None = None):
        self.result = result if result is not None else {
            "action": "created", "bill_id": 9001, "contact_id": 5001}
        self.error = error
        self.synced: list[dict] = []

    def sync(self, body: dict) -> dict:
        self.synced.append(body)
        if self.error:
            raise self.error
        return self.result


def build_service(*, moco=None, purchases=None, ocr=None,
                  telegram=None, subdomain="solar",
                  project_resolver=None, category_resolver=None,
                  bexio_expense=None):
    return SupplierInvoiceOcrService(
        moco=moco or FakeMoco(),
        purchase_client=purchases or FakePurchaseClient(),
        ocr=ocr or FakeOcr(result=make_invoice()),
        subdomain=subdomain,
        telegram=telegram,
        project_resolver=project_resolver,
        category_resolver=category_resolver,
        bexio_expense=bexio_expense,
    )


# --- skip gates -------------------------------------------------------------

def test_process_skips_non_create_events():
    """Only Purchase:create triggers OCR — update/delete are no-ops."""
    ocr = FakeOcr(result=make_invoice())
    purchases = FakePurchaseClient()
    s = build_service(ocr=ocr, purchases=purchases)
    result = s.process("update", {"id": 1, "file_url": "https://x/y.pdf"})
    assert result == {"skipped": "event_not_create"}
    assert ocr.calls == []
    assert purchases.creates == []


def test_process_skips_when_draft_id_missing():
    tg = FakeTelegram()
    ocr = FakeOcr(result=make_invoice())
    s = build_service(ocr=ocr, telegram=tg)
    result = s.process("create", {"file_url": "https://x/y.pdf"})
    assert result == {"skipped": "no_purchase_id"}
    assert ocr.calls == []
    assert len(tg.messages) == 1


def test_process_skips_when_file_url_missing_and_telegram_alerts():
    tg = FakeTelegram()
    purchases = FakePurchaseClient()
    s = build_service(telegram=tg, purchases=purchases)
    result = s.process("create", {"id": 3001069})
    assert result == {"skipped": "no_file_url", "draft_id": 3001069}
    assert purchases.creates == []
    assert len(tg.messages) == 1
    assert "purchases/drafts/3001069" in tg.messages[0]


def test_no_file_url_alert_includes_subject_and_sender():
    """The ⚠️ skip notification carries Betreff + Absender so the
    operator can recognize the draft without opening the deep-link."""
    tg = FakeTelegram()
    s = build_service(telegram=tg)
    s.process("create", {
        "id": 3001069,
        "title": "Rechnung Nr. 80572997",
        "email_from": "rechnung@sonepar.ch",
    })
    msg = tg.messages[0]
    assert "Betreff: Rechnung Nr. 80572997" in msg
    assert "Absender: rechnung@sonepar.ch" in msg


def test_no_file_url_alert_omits_missing_context_lines():
    """Manually-uploaded drafts with no email_from / no title produce
    no Betreff/Absender lines (rather than `Betreff: —` noise)."""
    tg = FakeTelegram()
    s = build_service(telegram=tg)
    s.process("create", {"id": 3001069})
    msg = tg.messages[0]
    assert "Betreff" not in msg
    assert "Absender" not in msg


@pytest.mark.parametrize("title", [
    "Sicherheitshinweis zu Ihrem Konto",
    "WG: Zustellungshinweis Paket 4711",
    "ZUSTELLUNGSHINWEIS",
])
def test_no_file_url_notification_subject_deletes_draft_silently(title):
    """Attachment-less drafts whose subject marks a notification email
    are deleted without any Telegram message."""
    tg = FakeTelegram()
    purchases = FakePurchaseClient()
    s = build_service(telegram=tg, purchases=purchases)
    result = s.process("create", {"id": 3001069, "title": title})
    assert result == {"skipped": "notification_draft_deleted",
                      "draft_id": 3001069}
    assert purchases.deleted_drafts == [3001069]
    assert purchases.creates == []
    assert tg.messages == []


def test_no_file_url_notification_delete_failure_stays_silent():
    """A failed delete logs a warning but never reaches Telegram — the
    stale draft is self-surfacing in Moco's draft list."""
    tg = FakeTelegram()
    purchases = FakePurchaseClient()
    purchases.delete_draft_error = urlerror.HTTPError(
        "https://x", 500, "boom", None, None)
    s = build_service(telegram=tg, purchases=purchases)
    result = s.process("create",
                       {"id": 3001069, "title": "Sicherheitshinweis"})
    assert result == {"skipped": "notification_draft_deleted",
                      "draft_id": 3001069}
    assert tg.messages == []


def test_no_file_url_without_notification_subject_still_alerts():
    """A normal invoice subject keeps the existing no-attachment path:
    Telegram alert, draft NOT deleted."""
    tg = FakeTelegram()
    purchases = FakePurchaseClient()
    s = build_service(telegram=tg, purchases=purchases)
    result = s.process("create",
                       {"id": 3001069, "title": "Rechnung Nr. 80572997"})
    assert result == {"skipped": "no_file_url", "draft_id": 3001069}
    assert purchases.deleted_drafts == []
    assert len(tg.messages) == 1


def test_notification_subject_with_attachment_is_processed_normally():
    """The keyword check only guards the no-attachment branch — a draft
    WITH a file_url goes through OCR even if the subject matches."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {"id": 42, "title": "Sicherheitshinweis",
                         "file_url": "https://x/y.pdf"})
    assert len(purchases.creates) == 1


def test_no_draft_id_alert_includes_context_when_present():
    """Even on the malformed-webhook path (no id) the body may still
    carry a title/email_from — surface them so a misconfigured
    integration can be diagnosed without checking Vercel logs."""
    tg = FakeTelegram()
    s = build_service(telegram=tg)
    s.process("create", {
        "title": "Aircondition - Rechnung 80572997",
        "email_from": "info@digitec.ch",
    })
    msg = tg.messages[0]
    assert "Betreff: Aircondition - Rechnung 80572997" in msg
    assert "Absender: info@digitec.ch" in msg


def test_process_runs_when_no_telegram_configured():
    """Telegram is optional; service still creates the purchase."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases, telegram=None)
    s.process("create", {"id": 42, "file_url": "https://x/y.pdf"})
    assert len(purchases.creates) == 1


# --- happy path -------------------------------------------------------------

def test_process_happy_path_creates_purchase_with_full_payload():
    """End-to-end: download PDF → OCR → company lookup → POST /purchases →
    comment on the NEW id → ✅ Telegram."""
    tg = FakeTelegram()
    pdf = b"%PDF-real"
    source = FakeMoco(pdf_bytes=pdf)
    ocr = FakeOcr(result=make_invoice())
    purchases = FakePurchaseClient()
    purchases.next_create_id = 4001234
    source.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    s = build_service(moco=source, purchases=purchases,
                      ocr=ocr, telegram=tg)

    result = s.process("create", {"id": 3001069,
                                  "file_url": "https://x/y.pdf"})

    assert source.downloads == ["https://x/y.pdf"]
    assert ocr.calls == [pdf]
    assert source.supplier_list_calls == 1
    assert len(purchases.creates) == 1
    payload = purchases.creates[0]

    # Required fields are present and correctly mapped.
    assert payload["date"] == "2026-05-12"
    assert payload["currency"] == "CHF"
    # QR-reference + IBAN present → Swiss QR-bill payment method.
    assert payload["payment_method"] == "bank_transfer_swiss_qr_esr"
    # Tags mark this as an OCR-imported purchase for the human reviewer.
    assert payload["tags"] == [OCR_TAG, REVIEW_PENDING_TAG]
    # Single line item with gross total, tax_included, and the vat code
    # resolved by matching OCR's vat_rate=0.081 to FakePurchaseClient's
    # default vat_codes list (id=11, value=8.1).
    assert payload["items"] == [{
        "title": "Solarmodule und Montage",
        "total": 1234.50,
        "tax_included": True,
        "vat_code_id": 11,
    }]
    # PDF goes in as base64 under file.
    assert payload["file"]["base64"] == base64.b64encode(pdf).decode("ascii")
    assert payload["file"]["filename"].endswith(".pdf")
    # Optional fields are filled when OCR found them.
    assert payload["due_date"] == "2026-06-11"
    assert payload["receipt_identifier"] == "R-2026-042"
    assert payload["iban"] == "CH4431999123000889012"
    assert payload["reference"] == "210000000003139471430009017"
    assert payload["info"] == "Rechnung Mai 2026"
    # Supplier lookup hit → company_id linked.
    assert payload["company_id"] == 555

    # Comment fired on the NEW purchase id (not the draft id).
    assert len(purchases.comments) == 1
    assert purchases.comments[0][0] == 4001234
    # ✅ Telegram (high confidence) with link to the new purchase.
    assert tg.messages[0].startswith("✅")
    assert "purchases/4001234" in tg.messages[0]

    assert result["draft_id"] == 3001069
    assert result["purchase_id"] == 4001234
    assert result["company_id"] == 555
    assert result["confidence"] == pytest.approx(0.92)


def test_payment_method_defaults_to_bank_transfer_without_qr():
    """IBAN-only / nothing → plain bank_transfer (the n8n flow's default)."""
    purchases = FakePurchaseClient()
    invoice = make_invoice(qr_reference=None)   # IBAN still present
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["payment_method"] == "bank_transfer"


def test_payment_method_falls_back_when_iban_is_not_qr_iban():
    """Moco 422s `payment_method=bank_transfer_swiss_qr_esr` with a regular
    IBAN ("ist keine QR-IBAN"). When OCR extracts a QR-reference but the
    IBAN doesn't have an IID in the 30000–31999 range, fall through to
    plain bank_transfer and DROP the reference field (it's QR-bill only;
    a stray 27-digit numeric reference on a plain transfer would either
    422 or get filed as junk metadata)."""
    # Same shape as the happy path but with a non-QR-IBAN (IID 00762).
    invoice = make_invoice(iban="CH9300762011623852957")
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    payload = purchases.creates[0]
    assert payload["payment_method"] == "bank_transfer"
    assert payload["iban"] == "CH9300762011623852957"
    assert "reference" not in payload   # dropped — would 422 otherwise


def test_draft_user_id_propagates_to_created_purchase():
    """When the draft webhook body carries `{"user": {"id": N}}` (the
    standard Moco shape), the created purchase's `user_id` is set to N
    so per-user reports + 'Mein Aufwand' filtering stay correct."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {"id": 3001069, "file_url": "https://x/y.pdf",
                          "user": {"id": 933719334, "firstname": "Tom"}})
    assert purchases.creates[0]["user_id"] == 933719334


def test_draft_without_user_omits_user_id():
    """No user object on the draft → omit the field entirely so Moco
    falls back to whatever default it assigns to API-created purchases.
    Sending `null` or a junk int would push wrong data."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {"id": 3001069, "file_url": "https://x/y.pdf"})
    assert "user_id" not in purchases.creates[0]


def test_already_paid_by_card_sets_credit_card_and_drops_payment_fields():
    """OCR-detected card / POS payment routes to `payment_method=credit_card`
    and suppresses the open-bill payment fields (iban, reference, due_date)
    — the bill is settled, there's no outbound transfer to schedule, and
    surfacing an IBAN on a closed bill would mislead anyone scanning the
    Moco UI. The Zahlungszweck stays in `info` as reviewer context."""
    invoice = make_invoice(
        already_paid_by_card=True,
        # All three of these would normally land on the payload — they
        # MUST be suppressed on the credit_card branch.
        iban="CH4431999123000889012",
        qr_reference="210000000003139471430009017",
        creditor_reference="RF43R0032202606070",
        due_date="2026-06-11",
        payment_purpose="Tankstelle / Visa",
    )
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    payload = purchases.creates[0]

    assert payload["payment_method"] == "credit_card"
    assert "iban" not in payload
    assert "reference" not in payload
    assert "due_date" not in payload
    # Zahlungszweck still useful as context for the reviewer.
    assert payload["info"] == "Tankstelle / Visa"
    # Other unrelated fields untouched.
    assert payload["currency"] == "CHF"
    assert payload["receipt_identifier"] == "R-2026-042"


def test_creditor_reference_lands_in_purchase_reference_field():
    """ISO 11649 SCOR (RF…) gets routed to the purchase's `reference` field
    even on plain bank_transfer — Moco's reference column accepts both QR
    and SCOR formats. Previously the SCOR ended up echoed in the info
    column because the QR-only branch dropped it."""
    invoice = make_invoice(
        # Non-QR IBAN — plain bank_transfer path.
        iban="CH9300762011623852957",
        qr_reference=None,
        creditor_reference="RF87R0032202606070000000",
        payment_purpose="Rechnung Mai 2026",
    )
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    payload = purchases.creates[0]
    assert payload["payment_method"] == "bank_transfer"
    assert payload["reference"] == "RF87R0032202606070000000"
    # info still carries the human-readable Zahlungszweck, untouched.
    assert payload["info"] == "Rechnung Mai 2026"


def test_creditor_reference_in_payment_purpose_is_stripped_from_info():
    """If the SCOR slipped into payment_purpose (older OCR runs) AND a
    creditor_reference was independently extracted, the info field is
    cleaned so the reviewer doesn't see the reference twice (once in the
    dedicated reference field, once in info)."""
    invoice = make_invoice(
        iban="CH9300762011623852957",
        qr_reference=None,
        creditor_reference="RF87R0032202606070000000",
        payment_purpose="RF87 R003 2202 6060 7000 0000 Rechnung Mai",
    )
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    payload = purchases.creates[0]
    assert payload["reference"] == "RF87R0032202606070000000"
    assert payload["info"] == "Rechnung Mai"


def test_qr_reference_takes_priority_over_creditor_reference_on_qr_iban():
    """When both are extracted, the QR-bill path wins — QR-IBAN + 27-digit
    reference is the canonical Swiss QR-bill signal."""
    invoice = make_invoice(
        # QR-IBAN from the happy-path fixture.
        creditor_reference="RF87R0032202606070000000",
    )
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    payload = purchases.creates[0]
    assert payload["payment_method"] == "bank_transfer_swiss_qr_esr"
    assert payload["reference"] == "210000000003139471430009017"


def test_draft_reference_with_scor_overrides_ocr_creditor_reference():
    """Same precedence rule as the QR-reference override: Moco's email-import
    parser is authoritative for the Zahlteil. A SCOR string sitting in the
    draft's `reference` field replaces whatever OCR guessed."""
    invoice = make_invoice(
        iban="CH9300762011623852957",
        qr_reference=None,
        # OCR guessed a different valid SCOR — should get overridden.
        creditor_reference="RF18539007547034",
    )
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf",
                          "reference": "RF87 R003 2202 6060 7000 0000"})
    payload = purchases.creates[0]
    assert payload["reference"] == "RF87R0032202606070000000"


def test_qr_iban_with_qr_reference_uses_qr_esr_payment_method():
    """Explicit check that QR-IBAN (IID in 30000-31999) + qr_reference
    together produce bank_transfer_swiss_qr_esr with the reference set."""
    # IID 30005 → QR-IBAN, also passes mod-97 (account 4248006030AB).
    invoice = make_invoice(iban="CH56300054248006030AB")
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    payload = purchases.creates[0]
    assert payload["payment_method"] == "bank_transfer_swiss_qr_esr"
    assert payload["reference"] == "210000000003139471430009017"


def test_draft_iban_overrides_ocr_iban():
    """Moco's email-import populates `iban` on the draft from its
    QR-bill parser, which is more reliable than vision-OCR — observed:
    Sonnet mangles alphanumeric Swiss IBANs (the real
    'CH22 3000 00DE 1611 6572 0' read as 'CH3909000000161165720').
    The draft's IBAN takes precedence."""
    # OCR returns an IBAN that happens to pass mod-97 but is wrong.
    invoice = make_invoice(iban="CH3909000000161165720")
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf",
                          "iban": "CH22300000DE161165720"})   # real QR-IBAN
    payload = purchases.creates[0]
    assert payload["iban"] == "CH22300000DE161165720"
    # Draft's QR-IBAN + OCR's qr_reference → QR-ESR payment method
    assert payload["payment_method"] == "bank_transfer_swiss_qr_esr"


def test_draft_iban_with_spaces_is_normalized():
    """Moco usually returns the IBAN stripped, but be defensive: a
    space-separated value still gets normalized + checksum-validated."""
    invoice = make_invoice(iban=None)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf",
                          "iban": "CH22 3000 00DE 1611 6572 0"})
    assert purchases.creates[0]["iban"] == "CH22300000DE161165720"


def test_invalid_draft_iban_does_not_override_ocr():
    """Defensive: a malformed draft IBAN (failing mod-97) is dropped by
    the same `_normalize_iban` used on OCR output, so a bad draft value
    doesn't silently overwrite a good OCR value."""
    invoice = make_invoice(iban="CH4431999123000889012")   # OCR has a valid one
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    # The draft IBAN below has a wrong check digit (00 instead of 22).
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf",
                          "iban": "CH0030000 0DE161165720"})
    assert purchases.creates[0]["iban"] == "CH4431999123000889012"


def test_draft_reference_overrides_ocr_qr_reference():
    """Same precedence rule for QR-reference — Moco's parser is the
    source of truth when present."""
    # Built as InvoiceData directly, so `_normalize_qr_reference` never
    # sees it — it only stands in for "whatever OCR produced" here.
    invoice = make_invoice(qr_reference="999999999999999999999999999")
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf",
                          "reference": "21 00000 00003 13947 14300 09017"})
    # Draft's reference wins, stripped to digits-only by _normalize_qr_reference.
    assert purchases.creates[0]["reference"] == "210000000003139471430009017"


def test_non_swiss_iban_uses_bank_transfer_even_with_qr_reference():
    """Defensive: a German IBAN (DE prefix) is obviously not a QR-IBAN,
    so we don't try to QR-ESR even if OCR happens to also report a
    qr_reference (paranoid OCR mode)."""
    invoice = make_invoice(iban="DE89370400440532013000")
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["payment_method"] == "bank_transfer"
    assert "reference" not in purchases.creates[0]


def test_payload_skips_optional_fields_when_not_extracted():
    """Optional Moco columns (due_date, receipt_identifier, iban, reference,
    info) only appear in the payload when OCR returned a value — otherwise
    Moco would interpret null as 'clear this field'. Exception:
    `due_date` is always computed (OCR value → else invoice_date+30,
    weekend-shifted), so it stays in the payload — covered in the
    dedicated due-date tests."""
    invoice = make_invoice(due_date=None, invoice_number=None,
                           iban=None, qr_reference=None,
                           payment_purpose=None)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    payload = purchases.creates[0]
    for key in ("receipt_identifier", "iban", "reference", "info"):
        assert key not in payload, key


def test_due_date_uses_ocr_value_when_weekday():
    """OCR'd due_date on a normal weekday passes through unchanged."""
    invoice = make_invoice(due_date="2026-06-11")  # Thursday
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["due_date"] == "2026-06-11"


def test_due_date_ocr_saturday_rolls_back_to_friday():
    """A Saturday due_date is unusable for supplier payments — shift to
    the preceding Friday so the bank schedule actually fires it."""
    invoice = make_invoice(due_date="2026-06-13")  # Saturday
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["due_date"] == "2026-06-12"  # Friday


def test_due_date_ocr_sunday_rolls_back_to_friday():
    invoice = make_invoice(due_date="2026-06-14")  # Sunday
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["due_date"] == "2026-06-12"  # Friday


def test_due_date_defaults_to_invoice_date_plus_30_when_ocr_missing():
    """No OCR due_date → invoice_date + 30 days. 2026-05-12 + 30d =
    2026-06-11 (Thursday) → kept as-is."""
    invoice = make_invoice(invoice_date="2026-05-12", due_date=None)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["due_date"] == "2026-06-11"


def test_due_date_default_plus_30_also_rolls_back_from_weekend():
    """The weekend-shift rule applies to the computed fallback too:
    2026-05-14 + 30d = 2026-06-13 (Saturday) → roll back to Friday 06-12."""
    invoice = make_invoice(invoice_date="2026-05-14", due_date=None)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["due_date"] == "2026-06-12"


def test_due_date_unit_helper():
    """Direct coverage of the resolver edges that are awkward to drive
    through the whole service."""
    from api.supplier_invoice_ocr_service import _resolve_due_date

    # OCR wins over invoice_date + 30.
    assert _resolve_due_date("2026-01-01", "2026-06-11") == "2026-06-11"
    # OCR on Sunday → Friday.
    assert _resolve_due_date("2026-01-01", "2026-06-14") == "2026-06-12"
    # No OCR → +30d, kept if weekday.
    assert _resolve_due_date("2026-05-12", None) == "2026-06-11"
    # Both missing → None (payload omits due_date).
    assert _resolve_due_date(None, None) is None
    # Garbage OCR value → return as-is (Moco will validate).
    assert _resolve_due_date("2026-05-12", "not-a-date") == "not-a-date"
    # Garbage invoice_date with no OCR → None (couldn't compute fallback).
    assert _resolve_due_date("garbage", None) is None


def test_credit_note_payload_uses_negative_total():
    """Gutschriften must book as negative — the operator can flip it back
    if the OCR misidentified, but defaulting to negative makes the sign
    match the document semantics."""
    invoice = make_invoice(is_credit_note=True, total_amount=500.0)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["total"] == -500.0


def test_credit_note_adds_gutschrift_tag():
    """Beyond the negative total + comment warning, a recognized
    Gutschrift gets its own tag so the operator can filter for credit
    notes in Moco's UI (`OCR` + `Review pending` + `Gutschrift`)."""
    invoice = make_invoice(is_credit_note=True)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["tags"] == ["OCR", "Review pending", "Gutschrift"]


def test_comment_includes_delivery_address_after_kommission():
    """OCR'd Lieferadresse (delivery / site address) lands in the OCR
    comment right after Kommission so both project-context fields sit
    together at the top."""
    invoice = make_invoice(
        commission="PV-2026-014",
        delivery_address="Hauptstrasse 5, 8304 Wallisellen",
    )
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    _, text = purchases.comments[0]
    assert "Lieferadresse" in text
    assert "Hauptstrasse 5, 8304 Wallisellen" in text
    # The three resolved-against-Moco fields lead the list with ✓/✗
    # markers (Lieferant → Konto → Kommission); the plain OCR readings
    # follow, Lieferadresse first.
    pos_lieferant = text.find("Lieferant")
    pos_konto = text.find("Konto")
    pos_kommission = text.find("Kommission")
    pos_lieferadresse = text.find("Lieferadresse")
    assert 0 <= pos_lieferant < pos_konto < pos_kommission < pos_lieferadresse


def test_comment_omits_lieferadresse_when_not_extracted():
    """If OCR couldn't find a Lieferadresse, the line drops out (no
    empty `<li>Lieferadresse:</li>` placeholder)."""
    invoice = make_invoice(delivery_address=None)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    _, text = purchases.comments[0]
    assert "Lieferadresse" not in text


def test_regular_invoice_does_not_get_gutschrift_tag():
    """Sanity check: a non-credit-note keeps only the standard OCR tags."""
    invoice = make_invoice(is_credit_note=False)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["tags"] == ["OCR", "Review pending"]


def test_missing_total_falls_back_to_zero():
    """A model that couldn't extract the total still produces a valid Moco
    payload — `total: 0` is acceptable to Moco and prompts the reviewer
    to fill it in (better than failing the whole sync over a single field)."""
    invoice = make_invoice(total_amount=None)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["total"] == 0.0


def test_filename_includes_date_supplier_and_invoice_number():
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {"id": 3001069, "file_url": "https://x/y.pdf"})
    filename = purchases.creates[0]["file"]["filename"]
    assert "2026-05-12" in filename
    assert "FLYERALARM" in filename
    assert "R-2026-042" in filename
    assert filename.endswith(".pdf")


def test_filename_falls_back_to_draft_id_when_metadata_missing():
    """Defensive: a totally-unextracted invoice still gets a unique-ish
    filename so Moco's attachment list doesn't show 'untitled.pdf'."""
    invoice = make_invoice(supplier_name=None, invoice_number=None,
                           invoice_date=None)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 3001069, "file_url": "https://x/y.pdf"})
    assert "draft-3001069" in purchases.creates[0]["file"]["filename"]


# --- supplier lookup --------------------------------------------------------

def test_no_supplier_match_leaves_company_id_unset():
    """Per "leave empty otherwise" — no match → no company_id field on
    the payload (so Moco doesn't 422 on company_id=null)."""
    source = FakeMoco()
    source.suppliers = []
    purchases = FakePurchaseClient()
    s = build_service(moco=source, purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert "company_id" not in purchases.creates[0]


def test_ambiguous_supplier_match_leaves_company_id_unset():
    """Multiple matches → human review needed; better to leave unset than
    auto-link the wrong company (would silently skew reporting)."""
    source = FakeMoco()
    source.suppliers = [
        {"id": 100, "name": "FLYERALARM"},
        {"id": 101, "name": "FLYERALARM"},   # duplicate registration
    ]
    purchases = FakePurchaseClient()
    s = build_service(moco=source, purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert "company_id" not in purchases.creates[0]


def test_supplier_lookup_failure_does_not_fail_sync():
    """The supplier lookup is best-effort — a flapping /companies endpoint
    shouldn't prevent the purchase from being created. Log + continue."""
    source = FakeMoco()
    source.suppliers_error = urlerror.HTTPError(
        "https://x", 500, "boom", {}, fp=None,
    )
    purchases = FakePurchaseClient()
    s = build_service(moco=source, purchases=purchases)
    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert len(purchases.creates) == 1
    assert "company_id" not in purchases.creates[0]
    assert result["company_id"] is None


def test_no_supplier_name_skips_lookup():
    """If the model couldn't extract a supplier name at all, don't even
    call the lookup (avoids unnecessary Moco round-trip + log noise)."""
    invoice = make_invoice(supplier_name=None)
    source = FakeMoco()
    s = build_service(moco=source, ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert source.supplier_list_calls == 0


# --- vat-code resolution ----------------------------------------------------

def test_vat_code_matched_from_ocr_vat_rate_decimal_format():
    """OCR returns rate as decimal (0.081 per the prompt). The resolver
    must match it to Moco's vat_code where `value=8.1`."""
    invoice = make_invoice(vat_rate=0.081)
    purchases = FakePurchaseClient()
    purchases.vat_codes = [
        {"id": 91, "tax": 7.7, "active": True},  # old rate
        {"id": 92, "tax": 8.1, "active": True},  # current rate
    ]
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 92


def test_vat_code_matched_even_if_ocr_returns_percentage_format():
    """Defensive: if a model run accidentally returns 8.1 instead of 0.081
    (against the prompt), the resolver still finds the match — both
    decimal and percentage forms are tried before giving up."""
    invoice = make_invoice(vat_rate=8.1)
    purchases = FakePurchaseClient()
    purchases.vat_codes = [{"id": 92, "tax": 8.1, "active": True}]
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 92


def test_vat_code_falls_back_to_supplier_default_when_rate_missing():
    """vat_rate is None on the invoice → look up supplier company →
    use its default_vat_code_purchase_id."""
    invoice = make_invoice(vat_rate=None)
    source = FakeMoco()
    source.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {
        "id": 555, "name": "FLYERALARM",
        "default_vat_code_purchase_id": 77,
    }
    purchases = FakePurchaseClient()
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 77


def test_vat_code_falls_back_to_supplier_vat_tax_via_lookup():
    """Per Moco's company docs the supplier's default purchase VAT lives
    under `supplier_vat.tax` (percentage) — there's NO direct `vat_code_id`
    on the company. Resolve the rate against `/vat_code_purchases` the
    same way OCR's vat_rate is resolved."""
    invoice = make_invoice(vat_rate=None)
    source = FakeMoco()
    source.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {
        "id": 555, "name": "FLYERALARM",
        "supplier_vat": {"tax": 2.6},   # percentage as Moco emits it
    }
    purchases = FakePurchaseClient()
    # FakePurchaseClient.vat_codes defaults include id=12 with tax=2.6.
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 12


def test_vat_code_supplier_vat_tax_zero_resolves_to_zero_rate_code():
    """A supplier marked `supplier_vat.tax = 0.0` (tax-free) must
    resolve to the 0% vat_code, not silently fall through to the
    account default."""
    invoice = make_invoice(vat_rate=None)
    source = FakeMoco()
    source.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {"id": 555, "supplier_vat": {"tax": 0.0}}
    purchases = FakePurchaseClient()
    # vat_codes defaults: id=13, tax=0.0.
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 13


def test_vat_code_supplier_vat_tax_with_no_matching_code_falls_through():
    """If `supplier_vat.tax` has no matching code (e.g. a foreign rate
    that's not in the Swiss account's `/vat_code_purchases` list), fall
    through to the account-wide default rather than getting stuck."""
    invoice = make_invoice(vat_rate=None)
    source = FakeMoco()
    source.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {"id": 555, "supplier_vat": {"tax": 19.0}}  # DE
    purchases = FakePurchaseClient()
    purchases.vat_codes = [
        {"id": 11, "tax": 8.1, "active": True},
        {"id": 99, "tax": 0.0, "active": True, "default": True},
    ]
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 99


def test_vat_code_supplier_lookup_uses_alternate_field_name():
    """Defensive: Moco's exact company field name isn't fully documented;
    accept `vat_code_purchase_id` in addition to the more-specific
    `default_vat_code_purchase_id`."""
    invoice = make_invoice(vat_rate=None)
    source = FakeMoco()
    source.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {"id": 555, "vat_code_purchase_id": 88}
    purchases = FakePurchaseClient()
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 88


def test_vat_code_falls_back_to_supplier_when_ocr_rate_doesnt_match():
    """If OCR's vat_rate has no matching code (e.g. unknown rate from a
    foreign-VAT supplier), fall through to the supplier default rather
    than giving up immediately."""
    invoice = make_invoice(vat_rate=0.19)   # German 19% — no Moco match
    source = FakeMoco()
    source.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {"id": 555, "default_vat_code_purchase_id": 77}
    purchases = FakePurchaseClient()
    purchases.vat_codes = [{"id": 11, "tax": 8.1, "active": True}, {"id": 12, "tax": 2.6, "active": True}]
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 77


def test_vat_code_falls_back_to_account_default_when_nothing_else_matches():
    """Neither vat_rate match nor supplier default → use the code marked
    as account-wide default (Moco's `default: true` flag on the vat code).
    This is the final fallback before omitting the field entirely."""
    invoice = make_invoice(vat_rate=None)
    purchases = FakePurchaseClient()
    purchases.vat_codes = [
        {"id": 11, "tax": 8.1, "active": True, "default": False},
        {"id": 12, "tax": 2.6, "active": True, "default": True},   # account default
        {"id": 13, "tax": 0.0, "active": True, "default": False},
    ]
    # No supplier match → no company branch taken.
    source = FakeMoco()
    source.suppliers = []
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 12


def test_vat_code_skips_inactive_codes_during_rate_match():
    """Moco keeps historical vat codes (old 7.7% pre-2024, special-purpose)
    around with `active: false`. They must not be picked even if their
    `tax` happens to match the OCR rate."""
    invoice = make_invoice(vat_rate=0.077)
    purchases = FakePurchaseClient()
    purchases.vat_codes = [
        {"id": 91, "tax": 7.7, "active": False},  # historical, must skip
        {"id": 92, "tax": 7.7, "active": True},   # current — pick this
    ]
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 92


def test_vat_code_skips_inactive_codes_during_account_default_lookup():
    """A code flagged default but `active: false` must NOT be picked —
    inactive codes are dead, period."""
    invoice = make_invoice(vat_rate=None)
    purchases = FakePurchaseClient()
    purchases.vat_codes = [
        {"id": 91, "tax": 7.7, "active": False, "default": True},
        {"id": 92, "tax": 8.1, "active": True, "default": True},
    ]
    s = build_service(purchases=purchases, ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 92


def test_vat_code_account_default_recognized_via_is_default_field():
    """Defensive: the doc shape isn't fully pinned, so also accept
    `is_default: true` as the default marker."""
    invoice = make_invoice(vat_rate=None)
    purchases = FakePurchaseClient()
    purchases.vat_codes = [
        {"id": 12, "tax": 2.6, "active": True, "is_default": True},
    ]
    s = build_service(purchases=purchases, ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 12


def test_bank_transfer_falls_back_to_standard_rate():
    """No OCR rate, no supplier default, no account-default flag (the
    state BOTH live Moco accounts are in) → the 8.1% standard-rate floor
    fires instead of omitting the field and eating a 422."""
    invoice = make_invoice(vat_rate=None)
    purchases = FakePurchaseClient()
    purchases.vat_codes = [{"id": 11, "tax": 8.1, "active": True}]  # nothing flagged default
    source = FakeMoco()
    source.suppliers = []  # no supplier match either
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 11
    # A guessed rate must never auto-release to Bexio.
    assert "Review pending" in purchases.creates[0]["tags"]


def test_already_paid_card_falls_back_to_zero_rate():
    """The skyr draft-3216692 case: a card/POS slip printing no VAT line.
    0% is the conservative booking — it claims no input tax."""
    invoice = make_invoice(vat_rate=None, already_paid_by_card=True)
    purchases = FakePurchaseClient()
    purchases.vat_codes = [
        {"id": 11, "tax": 8.1, "active": True},
        {"id": 12, "tax": 0.0, "active": True},
    ]
    source = FakeMoco()
    source.suppliers = []
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 12
    assert "Review pending" in purchases.creates[0]["tags"]


def test_zero_rate_fallback_skips_the_reverse_charge_code():
    """Both live accounts list the reverse-charge "(Ausland)" 0.0 code
    BEFORE the plain 0.0 one, so a first-match-by-rate would book a
    domestic lunch receipt to reverse charge."""
    invoice = make_invoice(vat_rate=None, already_paid_by_card=True)
    purchases = FakePurchaseClient()
    purchases.vat_codes = [
        {"id": 33681, "tax": 0.0, "active": True, "reverse_charge": True},
        {"id": 33679, "tax": 0.0, "active": True, "reverse_charge": False},
    ]
    source = FakeMoco()
    source.suppliers = []
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 33679


def test_vat_code_id_omitted_when_even_the_fallback_finds_no_code():
    """Only when the account has no active code at the wanted rate at all
    does the field come off. Moco 422s → Telegram alert + 200 ACK; the
    omission keeps the payload valid JSON."""
    invoice = make_invoice(vat_rate=None)
    purchases = FakePurchaseClient()
    purchases.vat_codes = [{"id": 11, "tax": 2.6, "active": True}]
    source = FakeMoco()
    source.suppliers = []
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert "vat_code_id" not in purchases.creates[0]["items"][0]


def test_vat_code_list_failure_still_allows_supplier_default():
    """A flapping /vat_code_purchases endpoint shouldn't nuke the run when
    the supplier could still supply a default — log + carry on."""
    invoice = make_invoice(vat_rate=None)
    purchases = FakePurchaseClient()
    purchases.vat_codes_error = urlerror.HTTPError(
        "https://x", 500, "boom", {}, fp=None,
    )
    source = FakeMoco()
    source.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {"id": 555, "default_vat_code_purchase_id": 77}
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 77


def test_vat_code_supplier_get_company_failure_falls_through_to_account_default():
    """If the supplier-default branch errors out (e.g. flapping
    get_company), the resolver should still try the account default
    rather than abandoning the run."""
    invoice = make_invoice(vat_rate=None)
    source = FakeMoco()
    source.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    source.get_company_error = urlerror.HTTPError(
        "https://x", 500, "boom", {}, fp=None,
    )
    purchases = FakePurchaseClient()
    purchases.vat_codes = [{"id": 99, "tax": 8.1, "active": True, "default": True}]
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 99


# --- draft auto-delete ------------------------------------------------------

def test_draft_is_deleted_after_successful_create():
    """Once the real purchase is created, the original draft is no longer
    needed — auto-delete to avoid duplicates in Moco's UI."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {"id": 3001069, "file_url": "https://x/y.pdf"})
    assert purchases.deleted_drafts == [3001069]


def test_draft_is_not_deleted_when_create_fails_with_4xx():
    """If POST /purchases 422'd (silent-skip path), the draft must stay
    so the operator can investigate. Auto-delete only on success."""
    import io
    purchases = FakePurchaseClient()
    purchases.create_error = urlerror.HTTPError(
        "https://x", 422, "boom", {}, fp=io.BytesIO(b"detail"),
    )
    s = build_service(purchases=purchases)
    s.process("create", {"id": 3001069, "file_url": "https://x/y.pdf"})
    assert purchases.deleted_drafts == []


def test_draft_404_on_delete_is_silently_idempotent():
    """A 404 from DELETE /purchases/drafts/{id} means the draft is
    already gone (replay, race) — swallow silently, no Telegram noise."""
    purchases = FakePurchaseClient()
    purchases.delete_draft_error = urlerror.HTTPError(
        "https://x", 404, "not found", {}, fp=None,
    )
    tg = FakeTelegram()
    s = build_service(purchases=purchases, telegram=tg)
    result = s.process("create", {"id": 3001069,
                                  "file_url": "https://x/y.pdf"})
    # Sync still returns success — create succeeded.
    assert "skipped" not in result
    # Only the success notification fired; no draft-delete-failed alert.
    assert all("nicht gelöscht" not in m for m in tg.messages)


def test_draft_non_404_delete_failure_alerts_but_keeps_sync_ok():
    """A 500 / other 4xx from delete is unexpected — alert via Telegram
    with both URLs so the operator can clean up, but DON'T roll the
    create back (the new purchase is the authoritative side effect)."""
    import io
    purchases = FakePurchaseClient()
    purchases.delete_draft_error = urlerror.HTTPError(
        "https://x", 500, "moco down", {},
        fp=io.BytesIO(b"server error"),
    )
    purchases.next_create_id = 4001234
    tg = FakeTelegram()
    s = build_service(purchases=purchases, telegram=tg)
    result = s.process("create", {"id": 3001069,
                                  "file_url": "https://x/y.pdf"})
    # Create still succeeded (authoritative side effect).
    assert result["purchase_id"] == 4001234
    # And the dedicated alert mentioning BOTH URLs fired.
    alerts = [m for m in tg.messages if "nicht gelöscht" in m]
    assert len(alerts) == 1
    assert "purchases/drafts/3001069" in alerts[0]
    assert "purchases/4001234" in alerts[0]
    assert "HTTP 500" in alerts[0]


def test_draft_delete_unexpected_exception_alerts():
    """Defensive: a non-HTTPError (URLError / arbitrary exception)
    during delete still produces a Telegram alert so the operator
    finds out — but doesn't fail the sync."""
    purchases = FakePurchaseClient()
    purchases.delete_draft_error = RuntimeError("boom")
    tg = FakeTelegram()
    s = build_service(purchases=purchases, telegram=tg)
    result = s.process("create", {"id": 3001069,
                                  "file_url": "https://x/y.pdf"})
    assert "skipped" not in result
    assert any("nicht gelöscht" in m for m in tg.messages)


# --- comment + telegram routing --------------------------------------------

def test_comment_text_includes_displayed_fields_and_draft_backlink():
    """Comment surfaces both standard fields and the non-Moco ones
    (is_credit_note, commission), plus a back-link to the original draft
    so the reviewer can clean it up post-approval."""
    invoice = make_invoice(commission="Bauvorhaben Müller")
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 3001069, "file_url": "https://x/y.pdf"})
    _, text = purchases.comments[0]
    assert "Konfidenz: 92%" in text
    assert "FLYERALARM" in text
    # Supplier address surfaces in the OCR comment (no dedicated Moco
    # column for it on the purchase — the comment is the only place
    # the reviewer sees what the model thought the address was).
    assert "Alfred-Nobel-Str. 18, 97080 Würzburg" in text
    assert "CHF 1234.50" in text
    assert "R-2026-042" in text
    assert "Bauvorhaben Müller" in text
    # No draft back-link — the draft is auto-deleted after successful create.
    assert "purchases/drafts/" not in text
    assert "Bitte prüfen und freigeben" in text


def test_comment_text_is_html_and_uses_only_moco_allowed_tags():
    """Moco only keeps these tags on comment bodies; anything else is
    stripped silently. Verify our output uses only the allowed set so the
    rendered comment in Moco's UI actually shows the formatting (and
    isn't degraded to a wall of stripped text)."""
    import re
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    _, text = purchases.comments[0]
    assert text.startswith("<div>")
    assert text.endswith("</div>")
    # Bulleted list rendering of the extracted fields.
    assert "<ul>" in text and "</ul>" in text
    assert "<li>" in text and "</li>" in text
    # Headers / important markers use <strong>.
    assert "<strong>🤖 OCR-Extraktion</strong>" in text
    assert "Bitte prüfen und freigeben</strong>" in text
    allowed = {"div", "strong", "em", "u", "pre", "ul", "ol", "li", "br"}
    used = set(re.findall(r"</?([a-z]+)", text))
    assert used <= allowed, f"used non-allowed tags: {used - allowed}"


def test_comment_text_escapes_html_special_chars_in_values():
    """Defensive: if a supplier_name contains `&` or `<`, the comment
    body must still be valid HTML (or Moco's parser may eat half of it)."""
    invoice = make_invoice(supplier_name="A&B <Co> AG",
                           description="Cost > 100 & < 200")
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    _, text = purchases.comments[0]
    # The raw chars must NOT appear verbatim (would break HTML).
    assert "A&B" not in text.replace("A&amp;B", "")
    assert "A&amp;B &lt;Co&gt; AG" in text


def test_two_comments_posted_when_draft_has_email_fields():
    """When Moco's email-import populated email_from / email_body, the
    service posts TWO separate comments: the 📧 Email-Quelle block first
    (chronological antecedent), then the 🤖 OCR-Extraktion. Two distinct
    timeline entries in Moco's UI."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {
        "id": 3001069,
        "file_url": "https://x/y.pdf",
        "email_from": "rechnung@sonepar.ch",
        "email_body": "Sehr geehrte Damen und Herren,\nbitte Rechnung im Anhang.",
    })
    assert len(purchases.comments) == 2
    # First comment: email source.
    pid_1, email_text = purchases.comments[0]
    pid_2, ocr_text = purchases.comments[1]
    assert pid_1 == pid_2  # both posted on the same new purchase
    assert "📧 Email-Quelle" in email_text
    assert "rechnung@sonepar.ch" in email_text
    assert "<pre>" in email_text and "</pre>" in email_text
    # Email-content does NOT leak into the OCR comment, and vice versa.
    assert "📧" not in ocr_text
    assert "rechnung@sonepar.ch" not in ocr_text
    assert "🤖 OCR-Extraktion" in ocr_text
    assert "FLYERALARM" in ocr_text   # OCR fields still in the OCR comment


def test_single_comment_when_draft_has_no_email_fields():
    """Manually-uploaded drafts (no email_from / email_body) get one
    comment only — the OCR summary. No empty 📧 placeholder."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert len(purchases.comments) == 1
    _, text = purchases.comments[0]
    assert "📧" not in text
    assert "🤖 OCR-Extraktion" in text


def test_email_only_comment_present_with_only_email_from():
    """Only one of the two fields populated is still enough to trigger
    the email comment (other field is just omitted in the rendering)."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {
        "id": 1, "file_url": "https://x/y.pdf",
        "email_from": "rechnung@sonepar.ch",
    })
    assert len(purchases.comments) == 2
    _, email_text = purchases.comments[0]
    assert "📧 Email-Quelle" in email_text
    assert "rechnung@sonepar.ch" in email_text
    assert "<pre>" not in email_text   # no body → no pre block


def test_html_email_body_is_sanitized_to_moco_tags_not_wrapped_in_pre():
    """Regression: forwarded emails from webmail clients arrive as HTML.
    Wrapping such a body in <pre> + html-escape would render the markup
    as literal `<div>` text in the Moco comment. Detect HTML and pass
    through (after stripping non-allowed tags) so the bold/paragraph
    structure is preserved."""
    html_body = (
        '<div><div>---------- Weitergeleitete Nachricht ----------<br>'
        '<strong>Von:</strong> verkauf_ro@sonepar.ch'
        '<br><strong>Betreff:</strong> Rechnung 9001769113</div>'
        '<div>Sehr geehrte Damen und Herren<br><br>'
        'Als Anlage erhalten Sie die gewünschte Rechnung.</div></div>'
    )
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {
        "id": 1, "file_url": "https://x/y.pdf",
        "email_from": "thomas.pluess@gmail.com",
        "email_body": html_body,
    })
    _, email_text = purchases.comments[0]
    # No <pre> wrap on an HTML body.
    assert "<pre>" not in email_text
    # Structural tags survive unchanged.
    assert "<strong>Von:</strong>" in email_text
    assert "<br>" in email_text
    assert "Weitergeleitete Nachricht" in email_text
    # The verbatim raw tags from the input must NOT appear as escaped
    # literals (the failure mode the user reported).
    assert "&lt;div&gt;" not in email_text


def test_html_email_strips_disallowed_tags_and_rewrites_b_i_p():
    """A forwarded email's `<b>` / `<i>` / `<p>` get rewritten to
    Moco's allowed subset (<strong>/<em>/<div>); `<span>` / `<font>` /
    `<a>` / `<table>` are removed entirely (content kept)."""
    body = (
        '<p>Hello <b>world</b></p>'
        '<span style="color:red">Some <font color="blue">colored</font> '
        'text</span> with <i>italics</i> and a '
        '<a href="https://example.com">link</a>.'
        '<table><tr><td>cell</td></tr></table>'
    )
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {
        "id": 1, "file_url": "https://x/y.pdf",
        "email_from": "x@y", "email_body": body,
    })
    _, text = purchases.comments[0]
    # Rewrites: b → strong, i → em, p → div.
    assert "<strong>world</strong>" in text
    assert "<em>italics</em>" in text
    assert "<div>Hello" in text
    # Disallowed tags gone (content kept):
    assert "<span" not in text
    assert "<font" not in text
    assert "<a " not in text and "<a>" not in text
    assert "<table" not in text and "<tr" not in text and "<td" not in text
    # Inner text from removed tags still readable.
    assert "Some" in text and "colored" in text and "link" in text
    assert "cell" in text


def test_plain_text_email_body_still_wrapped_in_pre():
    """Plain-text bodies (no tags) keep the existing <pre> treatment so
    indentation and newlines survive Moco's HTML normalizer."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {
        "id": 1, "file_url": "https://x/y.pdf",
        "email_from": "x@y",
        "email_body": "Line one\n  indented line two\nLine three",
    })
    _, text = purchases.comments[0]
    assert "<pre>" in text
    assert "</pre>" in text
    assert "indented line two" in text


def test_html_email_attributes_are_dropped():
    """Attributes on allowed tags (e.g. `<div style="…">`) are stripped
    too — Moco doesn't render them and they tend to carry CSS noise from
    random webmail clients."""
    body = '<div style="font-family:Arial" class="x"><strong id="z">Hi</strong></div>'
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {
        "id": 1, "file_url": "https://x/y.pdf",
        "email_from": "x@y", "email_body": body,
    })
    _, text = purchases.comments[0]
    assert "<div>" in text   # no attributes
    assert "<strong>" in text
    assert "style=" not in text and "class=" not in text and "id=" not in text


def test_html_detection_doesnt_misfire_on_plain_text_with_angle_brackets():
    """Plain text often contains `<email@host>` or `< 5%` — these must
    NOT trigger the HTML branch (would mangle the angle brackets and
    drop the email address)."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {
        "id": 1, "file_url": "https://x/y.pdf",
        "email_from": "x@y",
        "email_body": "Contact: <verkauf@sonepar.ch> for orders < 5%",
    })
    _, text = purchases.comments[0]
    # Plain-text branch (<pre>) kicks in; values are html-escaped.
    assert "<pre>" in text
    assert "&lt;verkauf@sonepar.ch&gt;" in text
    assert "&lt; 5%" in text


def test_email_body_whitespace_noise_is_normalized():
    """Regression: Outlook / webmail forwards arrive with CRLF runs,
    tab indentation, soft hyphens, zero-width chars, non-breaking
    spaces — all junk that drowns the actual content in the Moco
    comment. Normalize before posting (and the truncation marker should
    reflect the cleaned length, not the pre-normalization noise length)."""
    # Mirrors the user-reported sample: CRLF + tabs at the top, then
    # the actual content with zero-width / non-breaking / soft-hyphen
    # noise sprinkled in.
    noisy = (
        "\r\n\t\t\r\n\t\t\r\n      \r\n        \r\n        "
        "OrderInvoiceSending\r\n\t\t\t\t\r\n\t\t\t\t\r\n      \r\n"
        "‌Offene‍ ​Rechnungen\xad\xa0 "
    )
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {
        "id": 1, "file_url": "https://x/y.pdf",
        "email_from": "x@y", "email_body": noisy,
    })
    _, text = purchases.comments[0]
    # No CRLF, no tab runs, no zero-width chars, no soft hyphen surviving.
    assert "\r" not in text
    assert "\t" not in text
    assert "​" not in text and "‌" not in text
    assert "‍" not in text and "\xad" not in text
    # The actual content is intact and readable.
    assert "OrderInvoiceSending" in text
    assert "Offene Rechnungen" in text


def test_email_body_unit_helper_collapses_blank_lines():
    """Three+ consecutive blank lines collapse to one (paragraph break);
    leading/trailing blanks are stripped."""
    from api.supplier_invoice_ocr_service import _normalize_email_whitespace
    raw = "\n\n\n  first  \n\n\n\nsecond\n   third\n\n\n"
    cleaned = _normalize_email_whitespace(raw)
    # One blank line max between paragraphs; leading/trailing blanks gone.
    assert cleaned == "first\n\nsecond\nthird"


def test_email_body_unit_helper_keeps_html_markup_intact():
    """The normalizer collapses spaces but must not eat HTML tag chars;
    forwarded HTML emails go through this step too."""
    from api.supplier_invoice_ocr_service import _normalize_email_whitespace
    raw = "\r\n  <div>Hello\xa0<strong>world</strong></div>\t\r\n"
    cleaned = _normalize_email_whitespace(raw)
    assert cleaned == "<div>Hello <strong>world</strong></div>"


def test_email_body_is_truncated_when_huge():
    """Defensive: a multi-megabyte forwarded thread shouldn't bloat the
    Moco comment. Truncate to EMAIL_BODY_MAX_CHARS with a marker."""
    from api.supplier_invoice_ocr_service import EMAIL_BODY_MAX_CHARS
    huge = "x" * (EMAIL_BODY_MAX_CHARS + 500)
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {
        "id": 1, "file_url": "https://x/y.pdf",
        "email_from": "a@b.example", "email_body": huge,
    })
    _, email_text = purchases.comments[0]
    assert "gekürzt" in email_text
    assert str(EMAIL_BODY_MAX_CHARS) in email_text
    assert huge not in email_text


def test_email_fields_are_html_escaped():
    """Defensive: an email subject/body containing `<` or `&` must not
    break the comment HTML."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {
        "id": 1, "file_url": "https://x/y.pdf",
        "email_from": "<a@b> & co",
        "email_body": "Order #1 < 2 & valid",
    })
    _, email_text = purchases.comments[0]
    assert "&lt;a@b&gt; &amp; co" in email_text
    assert "Order #1 &lt; 2 &amp; valid" in email_text


def test_email_comment_failure_does_not_block_ocr_comment():
    """Each comment posts independently — if the email-source post fails
    (transient /comments error), the OCR-summary post still runs."""

    class FlakyPurchaseClient(FakePurchaseClient):
        def __init__(self):
            super().__init__()
            self.post_call_count = 0

        def post_comment(self, purchase_id, text):
            self.post_call_count += 1
            if self.post_call_count == 1:
                # First comment (email) fails; second (OCR) succeeds.
                raise urlerror.HTTPError(
                    "https://x", 500, "boom", {}, fp=None,
                )
            return super().post_comment(purchase_id, text)

    purchases = FlakyPurchaseClient()
    s = build_service(purchases=purchases)
    s.process("create", {
        "id": 1, "file_url": "https://x/y.pdf",
        "email_from": "a@b.example", "email_body": "hi",
    })
    # OCR comment still got through despite email comment failing.
    assert purchases.post_call_count == 2
    assert len(purchases.comments) == 1   # only the OCR one was recorded
    _, text = purchases.comments[0]
    assert "🤖 OCR-Extraktion" in text


def test_comment_text_marks_credit_note():
    invoice = make_invoice(is_credit_note=True)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    _, text = purchases.comments[0]
    assert "Gutschrift" in text
    assert "Vorzeichen" in text


def test_low_confidence_triggers_warning_telegram():
    invoice = make_invoice(confidence=0.62)
    assert 0.62 < CONFIDENCE_THRESHOLD
    tg = FakeTelegram()
    s = build_service(ocr=FakeOcr(result=invoice), telegram=tg)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert tg.messages[0].startswith("⚠️")
    assert "OCR unsicher" in tg.messages[0]


def test_credit_note_telegram_overrides_high_confidence():
    """Gutschrift's Vorzeichen-prüf alert wins even at 99% confidence."""
    invoice = make_invoice(is_credit_note=True, confidence=0.99)
    tg = FakeTelegram()
    s = build_service(ocr=FakeOcr(result=invoice), telegram=tg)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert "Gutschrift erkannt" in tg.messages[0]
    assert "Vorzeichen" in tg.messages[0]


def test_telegram_links_to_new_purchase_on_success():
    purchases = FakePurchaseClient()
    purchases.next_create_id = 4001234
    tg = FakeTelegram()
    s = build_service(purchases=purchases, telegram=tg)
    s.process("create", {"id": 3001069, "file_url": "https://x/y.pdf"})
    assert "purchases/4001234" in tg.messages[0]


def test_unknown_supplier_falls_back_to_unbekannt():
    invoice = make_invoice(supplier_name=None)
    tg = FakeTelegram()
    s = build_service(ocr=FakeOcr(result=invoice), telegram=tg)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert "Unbekannt" in tg.messages[0]


# --- error propagation ------------------------------------------------------

def test_ocr_4xx_propagates_to_caller():
    """A 4xx from Anthropic is the dispatcher's call to make (4xx → ok=false
    + Telegram, 5xx → 502). Don't swallow it in the service."""
    ocr = FakeOcr(error=AnthropicOcrError("bad model output", status_code=400))
    purchases = FakePurchaseClient()
    s = build_service(ocr=ocr, purchases=purchases)
    with pytest.raises(AnthropicOcrError):
        s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates == []


def test_network_error_during_download_propagates():
    """A URLError during PDF download is infrastructure — propagate so the
    dispatcher returns 502."""
    source = FakeMoco()
    source.download_error = urlerror.URLError("connection refused")
    s = build_service(moco=source)
    with pytest.raises(urlerror.URLError):
        s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})


def test_create_purchase_5xx_propagates():
    """A 5xx from POST /purchases is infrastructure — propagate so the
    endpoint maps it to HTTP 502 and Moco retries the webhook later."""
    purchases = FakePurchaseClient()
    purchases.create_error = urlerror.HTTPError(
        "https://x", 503, "moco down", {}, fp=None,
    )
    s = build_service(purchases=purchases)
    with pytest.raises(urlerror.HTTPError):
        s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})


def test_create_purchase_4xx_is_silent_skip_with_telegram_alert():
    """Regression: observed `POST /purchases 422 {"receipt_identifier":
    ["ist bereits vergeben"]}` on webhook replays of an already-OCR'd
    draft. A retry can't fix this — Moco itself enforces the unique
    constraint. Convert to a silent skip: HTTP 200 ok=true so Moco's
    webhook log stays clean, but a Telegram alert surfaces the
    rejection to the operator with the draft deep-link."""
    import io
    purchases = FakePurchaseClient()
    purchases.create_error = urlerror.HTTPError(
        "https://x", 422, "Unprocessable Entity", {},
        fp=io.BytesIO(b'{"receipt_identifier":["ist bereits vergeben"]}'),
    )
    tg = FakeTelegram()
    s = build_service(purchases=purchases, telegram=tg)
    result = s.process("create", {"id": 3001069,
                                  "file_url": "https://x/y.pdf"})
    assert result["skipped"] == "moco_rejected"
    assert result["draft_id"] == 3001069
    assert result["moco_status"] == 422
    assert "ist bereits vergeben" in result["moco_error"]
    # Telegram alert includes status, Moco's error body, and the
    # draft URL so the operator can jump to it.
    assert len(tg.messages) == 1
    assert "HTTP 422" in tg.messages[0]
    assert "ist bereits vergeben" in tg.messages[0]
    assert "purchases/drafts/3001069" in tg.messages[0]
    # No follow-up success comment / OCR-outcome alert fired — the
    # purchase wasn't created.
    assert purchases.comments == []


def test_moco_4xx_during_supplier_search_is_silent_skip():
    """4xx isn't only POST /purchases — a malformed `term` could also
    422 the supplier search. Same silent-skip treatment so the whole
    flow doesn't go down because of one Moco rejection."""
    source = FakeMoco()
    source.suppliers_error = urlerror.HTTPError(
        "https://x", 400, "Bad Request", {}, fp=None,
    )
    tg = FakeTelegram()
    purchases = FakePurchaseClient()
    s = build_service(moco=source, purchases=purchases, telegram=tg)
    # _lookup_supplier_company swallows the HTTPError internally → returns
    # None → process continues to POST /purchases which succeeds. So this
    # particular path actually does NOT 4xx-skip; document the boundary.
    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert "skipped" not in result   # supplier-lookup failure is best-effort
    assert len(purchases.creates) == 1


def test_moco_4xx_during_pdf_download_is_silent_skip():
    """A 4xx on the signed PDF download (expired link, auth glitch) is
    also unfixable-by-retry within the webhook lifetime — same
    silent-skip + Telegram pattern."""
    source = FakeMoco()
    source.download_error = urlerror.HTTPError(
        "https://x", 403, "Forbidden", {}, fp=None,
    )
    tg = FakeTelegram()
    s = build_service(moco=source, telegram=tg)
    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert result["skipped"] == "moco_rejected"
    assert result["moco_status"] == 403
    assert len(tg.messages) == 1
    assert "HTTP 403" in tg.messages[0]


def test_moco_4xx_alert_includes_subject_and_sender():
    """❌ OCR-Purchase nicht erstellt also carries the draft Betreff +
    Absender so the operator can triage from Telegram alone."""
    import io
    purchases = FakePurchaseClient()
    purchases.create_error = urlerror.HTTPError(
        "https://x", 422, "Unprocessable Entity", {},
        fp=io.BytesIO(b'{"receipt_identifier":["ist bereits vergeben"]}'),
    )
    tg = FakeTelegram()
    s = build_service(purchases=purchases, telegram=tg)
    s.process("create", {
        "id": 3001069,
        "file_url": "https://x/y.pdf",
        "title": "WG: Rechnung Müller GmbH",
        "email_from": "buchhaltung@meier-ag.ch",
    })
    msg = tg.messages[0]
    assert "Betreff: WG: Rechnung Müller GmbH" in msg
    assert "Absender: buchhaltung@meier-ag.ch" in msg
    # Detail line is preserved below the context lines.
    assert "ist bereits vergeben" in msg


def test_moco_4xx_skip_without_telegram_does_not_crash():
    """Telegram is optional on the service. The 4xx-skip branch must
    still return cleanly when telegram=None (unit-test convenience)."""
    import io
    purchases = FakePurchaseClient()
    purchases.create_error = urlerror.HTTPError(
        "https://x", 422, "boom", {}, fp=io.BytesIO(b"detail"),
    )
    s = build_service(purchases=purchases, telegram=None)
    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert result["skipped"] == "moco_rejected"


def test_comment_failure_does_not_undo_create():
    """The created purchase is the authoritative side effect; a flapping
    /comments endpoint must not surface as a failure (and importantly
    must not retry the create, which would duplicate)."""
    purchases = FakePurchaseClient()
    purchases.comment_error = urlerror.HTTPError(
        "https://x", 500, "boom", {}, fp=None,
    )
    tg = FakeTelegram()
    s = build_service(purchases=purchases, telegram=tg)
    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert len(purchases.creates) == 1
    assert "purchase_id" in result
    assert tg.messages


# --- project assignment (Kommission -> project) -----------------------------

PROJECT_HALDENWEG = {"id": 23345545, "name": "Sanierung Haldenweg",
                     "custom_properties": {"Kommission":
                                            "#Haldenweg12_Jegensdorf"}}


def _resolver_with(*projects):
    return MocoProjectResolver(list(projects))


def test_assign_runs_with_fixed_params_when_resolver_matches():
    """Single-item purchase + Kommission resolves uniquely → one
    assign_to_project call with the fixed param contract."""
    ocr = FakeOcr(result=make_invoice(commission="PVA Haldenweg 12_Jegensdorf"))
    purchases = FakePurchaseClient()
    s = build_service(
        purchases=purchases, ocr=ocr,
        project_resolver=_resolver_with(PROJECT_HALDENWEG),
    )
    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert len(purchases.assigns) == 1
    call = purchases.assigns[0]
    assert call["purchase_id"] == purchases.next_create_id - 0  # the one just created
    assert call["project_id"] == 23345545
    assert call["notify_project_leader"] is False
    assert call["billable"] is True
    assert call["budget_relevant"] is True
    assert call["surcharge"] is True
    assert call["expense_id"] is None
    # And the response surfaces what we resolved to.
    assert result["assigned_project_id"] == 23345545
    assert result["assigned_project_name"] == "Sanierung Haldenweg"


def test_assign_skipped_when_no_resolver_wired():
    """No resolver → no assign call, no extra fields in the response."""
    ocr = FakeOcr(result=make_invoice(commission="something"))
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases, ocr=ocr)  # no project_resolver
    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.assigns == []
    assert result["assigned_project_id"] is None


def test_assign_skipped_when_commission_does_not_match():
    """OCR'd commission resolves to no_match → no assign, response carries None."""
    ocr = FakeOcr(result=make_invoice(commission="totally-different-string"))
    purchases = FakePurchaseClient()
    s = build_service(
        purchases=purchases, ocr=ocr,
        project_resolver=_resolver_with(PROJECT_HALDENWEG),
    )
    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.assigns == []
    assert result["assigned_project_id"] is None


def test_assign_skipped_when_commission_is_ambiguous():
    """Two projects share the Kommission → ambiguous tier, do not assign."""
    other = {"id": 99, "name": "Other",
             "custom_properties": {"Kommission": "shared-key"}}
    twin = {"id": 100, "name": "Other2",
            "custom_properties": {"Kommission": "shared-key"}}
    ocr = FakeOcr(result=make_invoice(commission="shared-key"))
    purchases = FakePurchaseClient()
    s = build_service(
        purchases=purchases, ocr=ocr,
        project_resolver=_resolver_with(other, twin),
    )
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.assigns == []


def test_assign_skipped_when_commission_is_empty():
    """OCR returned no commission → resolver reports `empty` → no assign."""
    ocr = FakeOcr(result=make_invoice(commission=None))
    purchases = FakePurchaseClient()
    s = build_service(
        purchases=purchases, ocr=ocr,
        project_resolver=_resolver_with(PROJECT_HALDENWEG),
    )
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.assigns == []


CATEGORIES_FIXTURE = [
    {"id": 17, "credit_account": "4000", "label": "Wareneinkauf"},
    {"id": 18, "credit_account": "4500", "label": "Materialaufwand"},
]


def _category_resolver():
    return MocoCategoryResolver(CATEGORIES_FIXTURE)


def test_category_default_when_no_project_resolved():
    """No project resolver wired → 4000 fallback (Wareneinkauf, id=17)."""
    ocr = FakeOcr(result=make_invoice())
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases, ocr=ocr,
                      category_resolver=_category_resolver())
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    item = purchases.creates[0]["items"][0]
    assert item["category_id"] == 17


def test_category_uses_project_aufwandkonto_when_set():
    project = {"id": 99, "name": "Sanierung Haldenweg",
               "custom_properties": {"Kommission": "#H12",
                                     "Aufwandkonto": "4500"}}
    ocr = FakeOcr(result=make_invoice(commission="H12"))
    purchases = FakePurchaseClient()
    s = build_service(
        purchases=purchases, ocr=ocr,
        project_resolver=MocoProjectResolver([project]),
        category_resolver=_category_resolver(),
    )
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["category_id"] == 18


def test_category_omitted_when_project_aufwand_does_not_match_catalog():
    """Project says 4999 but no category has that credit_account.
    Must NOT silently fall back to 4000 — operator picks manually."""
    project = {"id": 99, "name": "Project Foo",
               "custom_properties": {"Kommission": "FOO",
                                     "Aufwandkonto": "4999"}}
    ocr = FakeOcr(result=make_invoice(commission="FOO"))
    purchases = FakePurchaseClient()
    s = build_service(
        purchases=purchases, ocr=ocr,
        project_resolver=MocoProjectResolver([project]),
        category_resolver=_category_resolver(),
    )
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert "category_id" not in purchases.creates[0]["items"][0]


def test_category_uses_supplier_aufwandkonto_when_no_project():
    """A matched supplier whose company carries an Aufwandkonto custom
    field routes the booking there instead of the 4000 default."""
    source = FakeMoco()
    source.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {
        "id": 555, "custom_properties": {"Aufwandkonto": "4500"},
    }
    purchases = FakePurchaseClient()
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=make_invoice()),
                      category_resolver=_category_resolver())
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["category_id"] == 18


def test_category_supplier_aufwandkonto_applies_to_already_paid():
    """The supplier override beats the already-paid guard — only the
    4000 default is suppressed for card receipts."""
    source = FakeMoco()
    source.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {
        "id": 555, "custom_properties": {"Aufwandkonto": "4500"},
    }
    purchases = FakePurchaseClient()
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=make_invoice(
                          already_paid_by_card=True)),
                      category_resolver=_category_resolver())
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["category_id"] == 18


def test_category_falls_back_when_get_company_fails():
    """A failing get_company degrades the supplier tier (no crash): the
    chain proceeds to the 4000 default."""
    source = FakeMoco()
    source.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    source.get_company_error = urlerror.HTTPError(
        "https://x", 500, "boom", {}, fp=None,
    )
    purchases = FakePurchaseClient()
    s = build_service(moco=source, purchases=purchases,
                      ocr=FakeOcr(result=make_invoice()),
                      category_resolver=_category_resolver())
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["category_id"] == 17


def test_category_omitted_when_already_paid_by_card():
    """Card receipts without an Aufwandkonto override must be manually
    triaged; no default at all."""
    ocr = FakeOcr(result=make_invoice(already_paid_by_card=True))
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases, ocr=ocr,
                      category_resolver=_category_resolver())
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert "category_id" not in purchases.creates[0]["items"][0]


def test_category_omitted_when_no_resolver_wired():
    """Service tests without a category resolver get no category_id."""
    ocr = FakeOcr(result=make_invoice())
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases, ocr=ocr)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert "category_id" not in purchases.creates[0]["items"][0]


def test_assign_failure_soft_fails_and_enriches_telegram():
    """A 422 on assign_to_project must NOT fail the sync; the warning is
    appended to the OCR-outcome Telegram alert."""
    ocr = FakeOcr(result=make_invoice(commission="PVA Haldenweg 12_Jegensdorf"))
    purchases = FakePurchaseClient()
    purchases.assign_errors = [urlerror.HTTPError(
        "https://x", 422, "boom", {}, fp=None,
    )]
    tg = FakeTelegram()
    s = build_service(
        purchases=purchases, ocr=ocr, telegram=tg,
        project_resolver=_resolver_with(PROJECT_HALDENWEG),
    )
    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    # Sync still ok; the purchase exists.
    assert result["purchase_id"] is not None
    # Outcome alert was sent and was enriched with the warning.
    assert tg.messages, "expected an outcome alert"
    last = tg.messages[-1]
    assert "Projektzuweisung teilweise fehlgeschlagen" in last
    assert "HTTP 422" in last


# --- smart-me dispatch --------------------------------------------------------

class FakeSmartmeService:
    def __init__(self):
        self.processed: list[dict] = []

    def process_draft(self, body: dict) -> dict:
        self.processed.append(body)
        return {"smartme": True, "draft_id": body.get("id"),
                "expense_id": 5555001}


# 2-of-3 detection: title keyword + body markers (email_from is the
# forwarder, like the real production sample).
SMARTME_BODY = {
    "id": 3070959,
    "title": "Test: smart-me: Ihre Energiekostenabrechnung",
    "email_from": "thomas@example.com",
    "email_body": "Objektname: Gesamtverbrauch\n"
                  "Abrechnungszeitraum: 01.01.2026 - 30.06.2026",
    "file_url": "https://data.mocoapp.com/objects/fake.pdf?sig=abc",
}


def test_smartme_draft_is_delegated_not_ocr_purchased():
    ocr = FakeOcr(result=make_invoice())
    purchases = FakePurchaseClient()
    smartme = FakeSmartmeService()
    s = SupplierInvoiceOcrService(
        moco=FakeMoco(), purchase_client=purchases, ocr=ocr,
        subdomain="solar", smartme=smartme)
    result = s.process("create", SMARTME_BODY)
    assert result == {"smartme": True, "draft_id": 3070959,
                      "expense_id": 5555001}
    assert smartme.processed == [SMARTME_BODY]
    # The generic OCR→purchase path never ran.
    assert ocr.calls == []
    assert purchases.creates == []


def test_smartme_draft_without_service_falls_through_to_generic_path():
    """smartme=None (default) — legacy behavior on the same body."""
    ocr = FakeOcr(result=make_invoice())
    purchases = FakePurchaseClient()
    s = build_service(ocr=ocr, purchases=purchases)
    result = s.process("create", SMARTME_BODY)
    assert "smartme" not in result
    assert len(purchases.creates) == 1


def test_attachmentless_smartme_draft_routes_to_smartme_not_notification():
    """Detection runs before the file_url gate: an attachment-less
    smart-me draft must reach the smart-me branch (keep + alert), not the
    notification silent-delete or the generic no-attachment alert."""
    purchases = FakePurchaseClient()
    smartme = FakeSmartmeService()
    s = SupplierInvoiceOcrService(
        moco=FakeMoco(), purchase_client=purchases,
        ocr=FakeOcr(result=make_invoice()),
        subdomain="solar", smartme=smartme)
    body = {k: v for k, v in SMARTME_BODY.items() if k != "file_url"}
    s.process("create", body)
    assert smartme.processed == [body]
    assert purchases.deleted_drafts == []


def test_non_smartme_draft_never_touches_smartme_service():
    smartme = FakeSmartmeService()
    purchases = FakePurchaseClient()
    s = SupplierInvoiceOcrService(
        moco=FakeMoco(), purchase_client=purchases,
        ocr=FakeOcr(result=make_invoice()),
        subdomain="solar", smartme=smartme)
    s.process("create", {"id": 3001069,
                         "title": "Rechnung R-2026-042",
                         "file_url": "https://x/y.pdf"})
    assert smartme.processed == []
    assert len(purchases.creates) == 1


# --- payment registration for already-paid receipts -------------------------
#
# See specs/SPEC_purchase_payment_already_paid.md. Card / TWINT / POS receipts
# arrive with the money already gone, so the created purchase is settled
# immediately via POST /purchases/payments rather than left showing an open
# balance for the operator to clear by hand.


def test_already_paid_receipt_registers_a_payment():
    purchases = FakePurchaseClient()
    # Moco recomputes gross from the line item + VAT code; the payment must
    # use that figure, not the OCR'd one, or the balance keeps a residual.
    purchases.create_gross_total = 249.05
    ocr = FakeOcr(result=make_invoice(already_paid_by_card=True,
                                      total_amount=249.0,
                                      invoice_date="2026-08-01"))
    s = build_service(purchases=purchases, ocr=ocr)

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.payments == [{
        "purchase_id": 4001234,
        "date": "2026-08-01",   # the purchase date, NOT today
        "total": 249.05,        # server gross_total, NOT the OCR 249.0
    }]
    assert result["payment_registered"] is True


def test_payment_total_falls_back_to_ocr_amount_without_gross_total():
    purchases = FakePurchaseClient()  # create_gross_total stays None
    ocr = FakeOcr(result=make_invoice(already_paid_by_card=True,
                                      total_amount=88.50))
    s = build_service(purchases=purchases, ocr=ocr)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.payments[0]["total"] == 88.50


def test_unpaid_bill_registers_no_payment():
    """An open bill is settled by the actual bank transfer later. Claiming
    otherwise would hide it from Moco's "was ist offen" view."""
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(already_paid_by_card=False))
    s = build_service(purchases=purchases, ocr=ocr)

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.payments == []
    assert result["payment_registered"] is False


def test_already_paid_credit_note_registers_no_payment():
    """A card refund is conceivable but the payment sign convention is
    unverified and we have no live example — deliberately skipped (D8)."""
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(already_paid_by_card=True,
                                      is_credit_note=True))
    s = build_service(purchases=purchases, ocr=ocr)

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.payments == []
    assert result["payment_registered"] is False


def test_payment_skipped_without_a_positive_amount():
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(already_paid_by_card=True,
                                      total_amount=None))
    s = build_service(purchases=purchases, ocr=ocr)

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.payments == []
    assert result["payment_registered"] is False


def test_payment_failure_is_soft_and_reported_on_telegram():
    """The purchase is the authoritative side effect — a failed payment
    warns and moves on rather than failing the sync."""
    purchases = FakePurchaseClient()
    purchases.payment_error = urlerror.HTTPError(
        "u", 422, "Unprocessable", {}, io.BytesIO(b'{"total":["ungueltig"]}'))
    telegram = FakeTelegram()
    ocr = FakeOcr(result=make_invoice(already_paid_by_card=True))
    s = build_service(purchases=purchases, ocr=ocr, telegram=telegram)

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert result["purchase_id"] == 4001234       # create still succeeded
    assert result["payment_registered"] is False
    assert "skipped" not in result
    assert purchases.deleted_drafts == [1]        # draft cleanup still ran
    assert any("Zahlung nicht registriert" in m for m in telegram.messages)


def test_successful_payment_is_mentioned_on_telegram():
    purchases = FakePurchaseClient()
    telegram = FakeTelegram()
    ocr = FakeOcr(result=make_invoice(already_paid_by_card=True))
    s = build_service(purchases=purchases, ocr=ocr, telegram=telegram)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert any("Zahlung erfasst" in m for m in telegram.messages)


# --- review gate wiring -----------------------------------------------------


def test_resolved_purchase_is_auto_released():
    """Company matched + trusted category + confidence >= 0.90 → no review."""
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "Digitec Galaxus AG"}]
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(supplier_name="Digitec Galaxus AG",
                                      confidence=0.94))
    s = build_service(moco=moco, purchases=purchases, ocr=ocr,
                      category_resolver=_category_resolver())

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.creates[0]["tags"] == ["OCR", "Auto"]
    assert result["review_pending"] is False
    assert result["review_reasons"] == []


def test_confidence_below_auto_release_bar_still_holds_for_review():
    """0.88 clears the 0.85 Telegram threshold but not the 0.90 auto-release
    bar — the regression test for those two constants being separate (D10)."""
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "Digitec Galaxus AG"}]
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(supplier_name="Digitec Galaxus AG",
                                      confidence=0.88))
    s = build_service(moco=moco, purchases=purchases, ocr=ocr,
                      category_resolver=_category_resolver())

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.creates[0]["tags"] == ["OCR", "Review pending"]
    assert result["review_pending"] is True
    assert result["review_reasons"] == ["Konfidenz 88% (< 90%)"]


def test_unmatched_company_holds_for_review():
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(confidence=0.99))
    s = build_service(purchases=purchases, ocr=ocr,
                      category_resolver=_category_resolver())

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.creates[0]["tags"] == ["OCR", "Review pending"]
    assert result["review_reasons"] == ["keine Firma zugeordnet"]


def test_already_paid_receipt_without_aufwandkonto_holds_but_still_pays():
    """The load-bearing card-receipt case: MocoCategoryResolver short-circuits
    at already_paid before the 4000 default, so a card receipt with no
    explicit Aufwandkonto has no category and is held. The payment is
    registered anyway — it does not depend on the review decision (D5)."""
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "Digitec Galaxus AG"}]
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(supplier_name="Digitec Galaxus AG",
                                      already_paid_by_card=True,
                                      confidence=0.99))
    s = build_service(moco=moco, purchases=purchases, ocr=ocr,
                      category_resolver=_category_resolver())

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert "category_id" not in purchases.creates[0]["items"][0]
    assert result["review_pending"] is True
    assert result["review_reasons"] == ["kein Aufwandkonto"]
    assert result["payment_registered"] is True


def test_already_paid_receipt_with_supplier_aufwandkonto_auto_releases():
    """The operator's opt-in lever: an Aufwandkonto on the supplier company
    gives a card receipt a trusted category, so it auto-releases and settles."""
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "Digitec Galaxus AG"}]
    moco.companies = {555: {"id": 555, "name": "Digitec Galaxus AG",
                            "custom_properties": {"Aufwandkonto": "4500"}}}
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(supplier_name="Digitec Galaxus AG",
                                      already_paid_by_card=True,
                                      confidence=0.99))
    s = build_service(moco=moco, purchases=purchases, ocr=ocr,
                      category_resolver=_category_resolver())

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.creates[0]["items"][0]["category_id"] == 18
    assert purchases.creates[0]["tags"] == ["OCR", "Auto"]
    assert result["review_pending"] is False
    assert result["payment_registered"] is True


def test_credit_note_keeps_gutschrift_tag_and_is_held():
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(is_credit_note=True, confidence=0.99))
    s = build_service(purchases=purchases, ocr=ocr,
                      category_resolver=_category_resolver())

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.creates[0]["tags"] == ["OCR", "Review pending",
                                            "Gutschrift"]
    assert result["review_pending"] is True


def test_registered_payment_posts_an_explanatory_comment():
    """The purchase timeline should say why it shows no open balance."""
    purchases = FakePurchaseClient()
    purchases.create_gross_total = 145.0
    ocr = FakeOcr(result=make_invoice(already_paid_by_card=True,
                                      currency="CHF",
                                      invoice_date="2026-06-17"))
    s = build_service(purchases=purchases, ocr=ocr)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    payment_comments = [text for pid, text in purchases.comments
                        if "Zahlung automatisch erfasst" in text]
    assert len(payment_comments) == 1
    body = payment_comments[0]
    assert "💳" in body
    assert "CHF 145.00" in body
    assert "2026-06-17" in body


def test_no_payment_comment_when_no_payment_registered():
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(already_paid_by_card=False))
    s = build_service(purchases=purchases, ocr=ocr)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert not any("Zahlung automatisch erfasst" in text
                   for _, text in purchases.comments)


def test_payment_comment_failure_does_not_unset_registered_flag():
    """The payment is the authoritative side effect — a failed comment
    must not make the caller believe the settle didn't happen."""
    purchases = FakePurchaseClient()
    purchases.comment_error = urlerror.HTTPError(
        "u", 500, "boom", {}, io.BytesIO(b"nope"))
    ocr = FakeOcr(result=make_invoice(already_paid_by_card=True))
    s = build_service(purchases=purchases, ocr=ocr)

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.payments != []          # payment still went through
    assert result["payment_registered"] is True


# --- OCR comment: verdict header + resolution checkmarks --------------------


def _resolved_service(purchases, ocr, moco=None):
    m = moco or FakeMoco()
    return build_service(moco=m, purchases=purchases, ocr=ocr,
                         category_resolver=_category_resolver())


def test_ocr_comment_leads_with_auto_release_verdict():
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "Digitec Galaxus AG"}]
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(supplier_name="Digitec Galaxus AG",
                                      confidence=0.94))
    s = _resolved_service(purchases, ocr, moco)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    _, text = purchases.comments[0]

    assert "Automatisch freigegeben" in text
    # The verdict is the FIRST thing, ahead of the extraction block.
    assert text.find("Automatisch freigegeben") < text.find("OCR-Extraktion")
    # ...and the old trailing review banner is gone, not merely moved.
    assert "Bitte prüfen und freigeben" not in text


def test_ocr_comment_leads_with_hold_verdict_and_reasons():
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(confidence=0.72))
    s = _resolved_service(purchases, ocr)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    _, text = purchases.comments[0]

    assert text.find("Bitte prüfen und freigeben") < text.find("OCR-Extraktion")
    assert "keine Firma zugeordnet" in text        # the reasons, verbatim
    assert "Konfidenz 72%" in text
    assert "Review pending" in text                # what to do next
    assert "Automatisch freigegeben" not in text


def test_ocr_comment_marks_resolved_fields_with_checkmarks():
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "Digitec Galaxus AG"}]
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(supplier_name="Digitec Galaxus AG"))
    s = _resolved_service(purchases, ocr, moco)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    _, text = purchases.comments[0]

    assert "✓ <strong>Lieferant:</strong>" in text
    assert "✓ <strong>Konto:</strong>" in text
    # Plain OCR readings stay unmarked — a ✓ there would imply a match
    # that was never attempted.
    assert "✓ <strong>Betrag:</strong>" not in text
    assert "✗ <strong>Betrag:</strong>" not in text


def test_ocr_comment_marks_unresolved_fields_and_says_why():
    purchases = FakePurchaseClient()  # no suppliers → no company match
    ocr = FakeOcr(result=make_invoice(supplier_name="Nozomi Luzern GmbH"))
    s = _resolved_service(purchases, ocr)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    _, text = purchases.comments[0]

    assert "✗ <strong>Lieferant:</strong>" in text
    assert "keine Firma in Moco zugeordnet" in text


def test_ocr_comment_konto_source_is_german():
    """`CategoryDecision.source` is an internal English token; the comment
    must not leak it to the operator."""
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "Digitec Galaxus AG"}]
    moco.companies = {555: {"id": 555, "name": "Digitec Galaxus AG",
                            "custom_properties": {"Aufwandkonto": "4500"}}}
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(supplier_name="Digitec Galaxus AG"))
    s = _resolved_service(purchases, ocr, moco)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    _, text = purchases.comments[0]

    assert "4500 (Lieferant)" in text
    assert "(supplier)" not in text


def test_ocr_comment_already_paid_konto_reason_is_german():
    """The already-paid omit is the most common HOLD reason for card
    receipts — it must read as an instruction, not a log line."""
    purchases = FakePurchaseClient()
    ocr = FakeOcr(result=make_invoice(already_paid_by_card=True))
    s = _resolved_service(purchases, ocr)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    _, text = purchases.comments[0]

    assert "kein Aufwandkonto hinterlegt — bitte manuell wählen" in text
    assert "skipped: already paid" not in text


def test_ocr_comment_standalone_render_keeps_legacy_banner():
    """Without a ReviewDecision (operator scripts, ad-hoc calls) there's no
    verdict to lead with, so the original trailing banner still applies."""
    text = _format_ocr_comment(make_invoice())

    assert "Bitte Felder prüfen und freigeben" in text
    assert "Automatisch freigegeben" not in text


# --- manual-upload subject --------------------------------------------------

def test_manual_upload_subject_reaches_the_model():
    """A hand-uploaded draft's title is the operator's statement of what
    the expense was FOR — the one thing OCR can never recover."""
    ocr = FakeOcr(result=make_invoice())
    s = build_service(ocr=ocr)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf",
                         "title": "Mittagessen 20.8."})
    assert ocr.subjects == ["Mittagessen 20.8."]


def test_email_subject_is_never_passed_as_an_operator_subject():
    """Regression: a FORWARDED email carries both `email_from` and the
    forwarding staff member as `user` (confirmed live, solar draft
    3213194), so `email_from` — not `user` — is the discriminator. An
    email Subject is not operator input and must not steer the title."""
    ocr = FakeOcr(result=make_invoice())
    s = build_service(ocr=ocr)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf",
                         "title": "Fwd: Rechnung Bützi 257g",
                         "email_from": "r.kaelin@pvcontracting.ch",
                         "user": {"id": 42, "firstname": "Romain",
                                  "lastname": "Kälin"}})
    assert ocr.subjects == [None]


def test_blank_subject_is_not_forwarded():
    ocr = FakeOcr(result=make_invoice())
    s = build_service(ocr=ocr)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf", "title": "   "})
    assert ocr.subjects == [None]


def test_position_title_becomes_the_purchase_and_item_title():
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases, ocr=FakeOcr(result=make_invoice(
        position_title="Mittagessen 20.8. — Ligu Lehm",
        description="Küche (Kassenbon)")))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    created = purchases.creates[0]
    assert created["title"] == "Mittagessen 20.8. — Ligu Lehm"
    assert created["items"][0]["title"] == "Mittagessen 20.8. — Ligu Lehm"


def test_missing_position_title_falls_back_to_description():
    """The model can omit the field, and every pre-`position_title` caller
    passes None — both must land on the old behaviour, not on a blank."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases, ocr=FakeOcr(
        result=make_invoice(position_title=None,
                            description="Solarmodule und Montage")))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["title"] == "Solarmodule und Montage"


# --- provenance comment -----------------------------------------------------

def test_manual_upload_gets_a_provenance_comment():
    """The draft is deleted after the create, so this comment is the only
    surviving record of who uploaded the document and what they called it."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases, ocr=FakeOcr(result=make_invoice()))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf",
                         "title": "Mittagessen 20.8.",
                         "user": {"id": 933633806, "firstname": "Tom",
                                  "lastname": "Plüss"}})
    comment = purchases.comments[0][1]
    assert "📎 Manueller Upload" in comment
    assert "Mittagessen 20.8." in comment
    assert "Tom Plüss" in comment


def test_email_draft_gets_the_email_comment_not_the_manual_one():
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases, ocr=FakeOcr(result=make_invoice()))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf",
                         "title": "Ihr Beleg Rechnung 2026-4022608",
                         "email_from": "rechnung@ch.krannich-solar.com"})
    comment = purchases.comments[0][1]
    assert "📧 Email-Quelle" in comment
    assert "📎 Manueller Upload" not in comment
    # The draft title IS the mail's Subject header — worth showing.
    assert "Ihr Beleg Rechnung 2026-4022608" in comment


def test_no_provenance_comment_when_the_draft_says_nothing():
    """No email fields, no title, no user — skip rather than post an
    empty header."""
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases, ocr=FakeOcr(result=make_invoice()))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert all("Manueller Upload" not in text
               and "Email-Quelle" not in text
               for _, text in purchases.comments)


def test_provenance_comment_failure_does_not_block_the_ocr_comment():
    """Independent best-effort posts — the OCR summary is the one the
    reviewer actually works from."""
    purchases = FakePurchaseClient()
    purchases.comment_errors = [RuntimeError("boom")]
    s = build_service(purchases=purchases, ocr=FakeOcr(result=make_invoice()))
    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf",
                                  "title": "Mittagessen 20.8."})
    assert result.get("skipped") is None
    assert any("OCR-Extraktion" in text for _, text in purchases.comments)


def test_subject_is_html_escaped_in_the_comment():
    assert "&lt;script&gt;" in _format_manual_upload_comment(
        "<script>alert(1)</script>", "Tom Plüss")


def test_manual_comment_renders_with_subject_alone():
    out = _format_manual_upload_comment("Mittagessen 20.8.", None)
    assert "Mittagessen 20.8." in out
    assert "Hochgeladen von" not in out


def test_manual_comment_is_empty_without_either_field():
    assert _format_manual_upload_comment(None, None) == ""


# --- supplier-company IBAN fallback -----------------------------------------
#
# Live case: solar purchase 4645420. The model mangled a Zahlteil IBAN
# ending in a letter ("...7001T"), `_normalize_iban` nulled it on the
# checksum, and the IBAN-less purchase made `BexioExpenseSyncService`
# take its MANUAL branch — which silently skips booking + the outgoing
# payment. Moco held the correct IBAN on the supplier all along.

SUPPLIER_IBAN = "CH773000523621577001T"   # real, mod-97 valid, QR-IBAN


def test_missing_iban_is_recovered_from_the_supplier_record():
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "energiecheck bern ag"}]
    moco.companies[555] = {"id": 555, "name": "energiecheck bern ag",
                           "iban": SUPPLIER_IBAN}
    purchases = FakePurchaseClient()
    s = build_service(moco=moco, purchases=purchases, ocr=FakeOcr(
        result=make_invoice(supplier_name="energiecheck bern ag", iban=None)))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.creates[0]["iban"] == SUPPLIER_IBAN


def test_ocr_read_iban_is_never_overridden():
    """The document wins when it's legible — a supplier can invoice from
    an account other than the one on file."""
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    moco.companies[555] = {"id": 555, "iban": SUPPLIER_IBAN}
    purchases = FakePurchaseClient()
    s = build_service(moco=moco, purchases=purchases, ocr=FakeOcr(
        result=make_invoice(iban="CH4431999123000889012")))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.creates[0]["iban"] == "CH4431999123000889012"


def test_malformed_supplier_iban_is_rejected_too():
    """A hand-entered Moco IBAN is not automatically trustworthy — it goes
    through the same mod-97 gate as the model's."""
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "FLYERALARM"}]
    moco.companies[555] = {"id": 555, "iban": "CH7730005236215770010"}  # bad
    purchases = FakePurchaseClient()
    s = build_service(moco=moco, purchases=purchases, ocr=FakeOcr(
        result=make_invoice(iban=None)))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert "iban" not in purchases.creates[0]


def test_no_supplier_match_leaves_the_iban_missing():
    purchases = FakePurchaseClient()
    moco = FakeMoco()
    moco.suppliers = []
    s = build_service(moco=moco, purchases=purchases,
                      ocr=FakeOcr(result=make_invoice(iban=None)))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert "iban" not in purchases.creates[0]


def test_recovered_iban_restores_the_qr_payment_method():
    """The point of the fallback: with the IBAN back, the QR-reference is
    usable again and the purchase books as a QR-ESR transfer instead of
    degrading to a plain one."""
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "energiecheck bern ag"}]
    moco.companies[555] = {"id": 555, "iban": SUPPLIER_IBAN}
    purchases = FakePurchaseClient()
    s = build_service(moco=moco, purchases=purchases, ocr=FakeOcr(
        result=make_invoice(supplier_name="energiecheck bern ag", iban=None,
                            qr_reference="000000000000000000004091456")))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    created = purchases.creates[0]
    assert created["payment_method"] == "bank_transfer_swiss_qr_esr"
    assert created["reference"] == "000000000000000000004091456"


def test_recovered_iban_is_flagged_in_the_ocr_comment():
    """The reviewer is confirming a different claim than 'we read this off
    the document', so the comment has to say which."""
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "energiecheck bern ag"}]
    moco.companies[555] = {"id": 555, "iban": SUPPLIER_IBAN}
    purchases = FakePurchaseClient()
    s = build_service(moco=moco, purchases=purchases, ocr=FakeOcr(
        result=make_invoice(supplier_name="energiecheck bern ag", iban=None)))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    comment = next(t for _, t in purchases.comments if "OCR-Extraktion" in t)
    assert "aus Lieferant-Stammdaten" in comment


def test_ocr_read_iban_is_not_flagged_in_the_comment():
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases, ocr=FakeOcr(
        result=make_invoice(iban="CH4431999123000889012")))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    comment = next(t for _, t in purchases.comments if "OCR-Extraktion" in t)
    assert "aus Lieferant-Stammdaten" not in comment


# --- QR-reference-without-IBAN inconsistency --------------------------------

def test_qr_reference_without_iban_warns_on_telegram():
    """The Zahlteil was legible enough for a 27-digit reference but not the
    IBAN beside it, and no supplier record could supply one. Downstream
    that silently becomes a MANUAL Bexio payment with no Zahlungsausgang,
    so it has to be said out loud."""
    tg = FakeTelegram()
    s = build_service(ocr=FakeOcr(result=make_invoice(
        iban=None, qr_reference="000000000000000000004091456")), telegram=tg)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert any("QR-Referenz erkannt, aber keine IBAN" in m
               for m in tg.messages)


def test_no_warning_once_the_supplier_iban_recovers_it():
    """The fallback ran, so there's nothing inconsistent left to report."""
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "energiecheck bern ag"}]
    moco.companies[555] = {"id": 555, "iban": SUPPLIER_IBAN}
    tg = FakeTelegram()
    s = build_service(moco=moco, telegram=tg, ocr=FakeOcr(
        result=make_invoice(supplier_name="energiecheck bern ag", iban=None,
                            qr_reference="000000000000000000004091456")))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert not any("keine IBAN" in m for m in tg.messages)


def test_no_warning_without_a_qr_reference():
    """A plain invoice with no IBAN is ordinary — many suppliers bill
    without one. Only the QR-reference makes the gap contradictory."""
    tg = FakeTelegram()
    s = build_service(ocr=FakeOcr(result=make_invoice(
        iban=None, qr_reference=None)), telegram=tg)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert not any("keine IBAN" in m for m in tg.messages)


def test_warning_rides_on_the_existing_message():
    """One Telegram per draft — the warning is a suffix, not a second send."""
    tg = FakeTelegram()
    s = build_service(ocr=FakeOcr(result=make_invoice(
        iban=None, qr_reference="000000000000000000004091456")), telegram=tg)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert len(tg.messages) == 1


# --- auto-release → Bexio hand-off ------------------------------------------
#
# Moco does not deliver a `Purchase:create` webhook for purchases created
# through the API, so an auto-released purchase never reached
# `bexio-expense-sync` on its own: the `Review pending` tag it
# deliberately lacks is exactly what a later *update* webhook would have
# cleared. Ten of eleven live auto-released purchases had no Bexio bill.


def _auto_release_service(**kwargs):
    """A service whose gate auto-releases: company matched, category
    trusted (4000 default), confidence above the bar."""
    moco = FakeMoco()
    moco.suppliers = [{"id": 555, "name": "Digitec Galaxus AG"}]
    kwargs.setdefault("moco", moco)
    kwargs.setdefault("ocr", FakeOcr(result=make_invoice(
        supplier_name="Digitec Galaxus AG", confidence=0.94)))
    kwargs.setdefault("category_resolver", _category_resolver())
    return build_service(**kwargs)


def test_auto_released_purchase_is_handed_to_bexio():
    bexio = FakeBexioExpense()
    purchases = FakePurchaseClient()
    s = _auto_release_service(purchases=purchases, bexio_expense=bexio)

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert result["review_pending"] is False
    assert len(bexio.synced) == 1
    assert result["bexio_bill_id"] == 9001


def test_held_purchase_is_not_handed_to_bexio():
    """The point of the tag is that a person looks first — and the expense
    sync's own first gate would refuse it anyway."""
    bexio = FakeBexioExpense()
    purchases = FakePurchaseClient()
    s = build_service(purchases=purchases, bexio_expense=bexio,
                      ocr=FakeOcr(result=make_invoice(confidence=0.50)))

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert result["review_pending"] is True
    assert bexio.synced == []
    assert result["bexio_bill_id"] is None


def test_handoff_sends_the_refetched_purchase_not_the_create_response():
    """The expense sync reads `file_url` and the expanded
    `items[].category`; nothing guarantees POST /purchases echoes every
    association Moco resolves server-side."""
    bexio = FakeBexioExpense()
    purchases = FakePurchaseClient()
    purchases.purchases_by_id[4001234] = {
        "id": 4001234, "tags": ["OCR", "Auto"],
        "file_url": "https://moco/signed.pdf",
        "items": [{"category": {"credit_account": "4000"}}],
    }
    s = _auto_release_service(purchases=purchases, bexio_expense=bexio)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert bexio.synced[0]["file_url"] == "https://moco/signed.pdf"


def test_bexio_failure_does_not_fail_the_ocr_sync():
    """Best-effort, like the project assign and the payment. A 4xx escaping
    to index.py would flip this run to ok=false; a 5xx would trigger a
    webhook retry that re-runs the whole paid OCR."""
    bexio = FakeBexioExpense(error=urlerror.HTTPError(
        "https://bexio", 400, "Bad Request", {},
        fp=io.BytesIO(b'{"message":"nope"}')))
    purchases = FakePurchaseClient()
    tg = FakeTelegram()
    s = _auto_release_service(purchases=purchases, bexio_expense=bexio,
                              telegram=tg)

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert result.get("skipped") is None          # the purchase still stands
    assert result["purchase_id"] == 4001234
    assert result["bexio_bill_id"] is None
    assert any("Bexio-Übergabe fehlgeschlagen" in m for m in tg.messages)


def test_bexio_transport_error_is_also_swallowed():
    bexio = FakeBexioExpense(error=urlerror.URLError("connection refused"))
    tg = FakeTelegram()
    s = _auto_release_service(bexio_expense=bexio, telegram=tg)

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert result["purchase_id"] == 4001234
    assert any("Bexio-Übergabe fehlgeschlagen" in m for m in tg.messages)


def test_a_failed_refetch_does_not_fail_the_sync():
    purchases = FakePurchaseClient()
    purchases.get_purchase_error = urlerror.HTTPError(
        "https://moco", 404, "Not Found", {}, fp=io.BytesIO(b"gone"))
    bexio = FakeBexioExpense()
    s = _auto_release_service(purchases=purchases, bexio_expense=bexio)

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert result["purchase_id"] == 4001234
    assert bexio.synced == []


def test_bexio_skip_is_reported_without_a_second_alert():
    """The expense sync fires its own Telegram alert naming the reason
    (no company / no account / bill not DRAFT) — don't duplicate it."""
    bexio = FakeBexioExpense(result={"skipped": "no_account"})
    tg = FakeTelegram()
    s = _auto_release_service(bexio_expense=bexio, telegram=tg)

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert result["bexio_bill_id"] is None
    assert not any("Bexio-Übergabe fehlgeschlagen" in m for m in tg.messages)


def test_successful_handoff_is_reported_on_telegram():
    """The Moco comment claims the purchase went to Bexio without review;
    Telegram has to confirm that actually happened."""
    tg = FakeTelegram()
    s = _auto_release_service(bexio_expense=FakeBexioExpense(), telegram=tg)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert any("Bexio-Rechnung erstellt (#9001)" in m for m in tg.messages)


def test_no_collaborator_keeps_the_old_behaviour():
    """An instance without Bexio credentials still OCRs; the purchase just
    waits for a human to touch it in Moco."""
    purchases = FakePurchaseClient()
    s = _auto_release_service(purchases=purchases)   # bexio_expense=None

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert result["review_pending"] is False
    assert result["bexio_bill_id"] is None


# --- regression: solar purchase 4646039 -------------------------------------
#
# A Solarmarkt QR-bill. OCR read the 27-digit QR-reference but not the
# IBAN beside it (the IBAN ends in a letter — "CH59 3000 5231 1801 9101 U"
# — the alphanumeric-account case `_normalize_iban` documents Sonnet
# mangling). The purchase was booked as a plain `bank_transfer` with the
# QR-reference DROPPED and no IBAN at all, then auto-released to Bexio in
# that state.
#
# One missing IBAN causes all three symptoms, because
# `_payment_method_for` and `_resolve_reference_and_info` both branch on
# it. This pins the whole payload, not just the IBAN.

SOLARMARKT_IBAN = "CH593000523118019101U"          # real, QR-IBAN (IID 30005)
SOLARMARKT_QR_REF = "123456000000033908054024177"  # real, 27 digits


def _solarmarkt_service(purchases, **kwargs):
    moco = FakeMoco()
    moco.suppliers = [{"id": 762656480, "name": "Solarmarkt GmbH"}]
    moco.companies[762656480] = {"id": 762656480, "name": "Solarmarkt GmbH",
                                 "iban": SOLARMARKT_IBAN}
    return build_service(
        moco=moco, purchases=purchases,
        category_resolver=_category_resolver(),
        ocr=FakeOcr(result=make_invoice(
            supplier_name="Solarmarkt GmbH",
            iban=None,                       # the model missed it
            qr_reference=SOLARMARKT_QR_REF,  # but got the reference
            payment_purpose="Rechnung 5402417",
            confidence=0.93)),
        **kwargs)


def test_regression_4646039_qr_bill_books_with_recovered_iban():
    """All four fields the live purchase got wrong, in one assertion
    block: IBAN recovered, QR-ESR payment method, QR-reference kept, and
    auto-released while carrying those correct values."""
    purchases = FakePurchaseClient()
    s = _solarmarkt_service(purchases)

    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    created = purchases.creates[0]
    assert created["iban"] == SOLARMARKT_IBAN
    assert created["payment_method"] == "bank_transfer_swiss_qr_esr"
    assert created["reference"] == SOLARMARKT_QR_REF
    # Auto-release must carry the corrected values, not merely happen.
    assert created["tags"] == ["OCR", "Auto"]
    assert result["review_pending"] is False


def test_regression_4646039_hands_the_corrected_purchase_to_bexio():
    """The bug shipped a wrong purchase straight to Bexio unreviewed, so
    the hand-off has to be part of the regression, not a separate concern."""
    purchases = FakePurchaseClient()
    bexio = FakeBexioExpense()
    purchases.purchases_by_id[4001234] = {
        "id": 4001234, "tags": ["OCR", "Auto"], "iban": SOLARMARKT_IBAN,
        "reference": SOLARMARKT_QR_REF,
    }
    s = _solarmarkt_service(purchases, bexio_expense=bexio)

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert len(bexio.synced) == 1
    assert bexio.synced[0]["iban"] == SOLARMARKT_IBAN


def test_regression_4646039_without_recovery_the_qr_reference_is_lost():
    """Pins WHY this mattered: with no IBAN, `_payment_method_for` falls to
    a plain transfer and `_resolve_reference_and_info` drops the
    QR-reference outright — Moco 422s the QR-ESR path on a non-QR-IBAN.
    That is the exact shape the live purchase had."""
    purchases = FakePurchaseClient()
    moco = FakeMoco()
    moco.suppliers = []          # no supplier match → nothing to recover from
    s = build_service(moco=moco, purchases=purchases,
                      category_resolver=_category_resolver(),
                      ocr=FakeOcr(result=make_invoice(
                          supplier_name="Solarmarkt GmbH", iban=None,
                          qr_reference=SOLARMARKT_QR_REF,
                          payment_purpose="Rechnung 5402417")))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    created = purchases.creates[0]
    assert "iban" not in created
    assert created["payment_method"] == "bank_transfer"
    assert "reference" not in created
    assert created["info"] == "Rechnung 5402417"


@pytest.mark.parametrize("script", ["scripts/batch_ocr_drafts.py",
                                    "scripts/test_ocr_create_purchase.py"])
def test_operator_scripts_run_the_iban_fallback(script):
    """Anti-drift guard, deliberately a source check.

    The operator scripts don't call `SupplierInvoiceOcrService.process` —
    they re-walk the pipeline step by step so each resolution can be
    printed — and then hand the result to the same
    `_build_create_payload`. That makes them silently skippable: when
    `_apply_supplier_iban_fallback` was added to the service it was NOT
    added here, so `--apply` created purchases with no IBAN, a plain
    `bank_transfer` and the QR-reference dropped, while the webhook path
    got it right.

    A behavioural test would need the scripts' whole argparse/Moco
    plumbing stubbed; asserting the call exists is cheap and catches the
    exact failure that happened. Same reasoning that removed the
    hand-written `VatCodeResolver` mirror from `batch_ocr_drafts.py`.
    """
    source = pathlib.Path(script).read_text()
    assert "_apply_supplier_iban_fallback(" in source, (
        f"{script} does not apply the supplier-IBAN fallback — an "
        "--apply run there will book QR-bills as plain transfers"
    )


# --- QR-IBAN without a QR-reference -----------------------------------------
#
# The mirror of the QR-reference-without-IBAN case above, and the more
# dangerous one: a QR-IBAN is only payable WITH a QR-reference, so this
# combination is a bill no bank will accept. Live on solar purchase
# 4646154 — the supplier fallback supplied the QR-IBAN, but the model
# dropped a zero from the reference's long zero-run and
# `_normalize_qr_reference` nulled it. Nothing can recover it: the
# reference is per-invoice and exists only on the document.

QR_IBAN = "CH593000523118019101U"        # Solarmarkt, IID 30005
PLAIN_IBAN = "CH7708836121049112006"     # not in the QR-IBAN range


def test_qr_iban_without_reference_warns_on_telegram():
    tg = FakeTelegram()
    s = build_service(telegram=tg, ocr=FakeOcr(result=make_invoice(
        iban=QR_IBAN, qr_reference=None)))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert any("QR-IBAN erkannt, aber keine gültige QR-Referenz" in m
               for m in tg.messages)


def test_qr_iban_with_reference_does_not_warn():
    tg = FakeTelegram()
    s = build_service(telegram=tg, ocr=FakeOcr(result=make_invoice(
        iban=QR_IBAN, qr_reference="123456000000033908054024177")))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert not any("QR-IBAN erkannt" in m for m in tg.messages)


def test_plain_iban_without_reference_does_not_warn():
    """An ordinary IBAN bill needs no reference — warning on those would be
    noise. It is the QR-IBAN that makes the gap fatal."""
    tg = FakeTelegram()
    s = build_service(telegram=tg, ocr=FakeOcr(result=make_invoice(
        iban=PLAIN_IBAN, qr_reference=None)))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert not any("QR-IBAN erkannt" in m for m in tg.messages)


def test_the_two_qr_warnings_are_mutually_exclusive():
    """Reference-without-IBAN keeps its own message; the branches must not
    both fire, and one Telegram per draft stays the rule."""
    tg = FakeTelegram()
    s = build_service(telegram=tg, ocr=FakeOcr(result=make_invoice(
        iban=None, qr_reference="123456000000033908054024177")))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert len(tg.messages) == 1
    assert "keine IBAN gelesen" in tg.messages[0]
    assert "QR-IBAN erkannt" not in tg.messages[0]


def test_recovered_qr_iban_without_reference_still_warns():
    """The exact 4646154 shape: the supplier fallback supplies the QR-IBAN,
    so the bill LOOKS more complete than it is — the warning is the only
    signal that it cannot actually be paid."""
    moco = FakeMoco()
    moco.suppliers = [{"id": 762656480, "name": "Solarmarkt GmbH"}]
    moco.companies[762656480] = {"id": 762656480, "iban": QR_IBAN}
    purchases = FakePurchaseClient()
    tg = FakeTelegram()
    s = build_service(moco=moco, purchases=purchases, telegram=tg,
                      ocr=FakeOcr(result=make_invoice(
                          supplier_name="Solarmarkt GmbH",
                          iban=None, qr_reference=None)))

    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})

    assert purchases.creates[0]["iban"] == QR_IBAN
    assert purchases.creates[0]["payment_method"] == "bank_transfer"
    assert any("QR-IBAN erkannt, aber keine gültige QR-Referenz" in m
               for m in tg.messages)
