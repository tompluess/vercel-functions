"""Unit tests for SupplierInvoiceOcrService — payload shape, skip gates,
supplier lookup branches, comment text, and confidence-routed Telegram
alerts. In-memory fakes for all collaborators."""

import base64
from urllib import error as urlerror

import pytest

from api.anthropic_ocr_client import AnthropicOcrError, InvoiceData
from api.supplier_invoice_ocr_service import (
    CONFIDENCE_THRESHOLD,
    OCR_TAGS,
    SupplierInvoiceOcrService,
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
        payment_purpose="Rechnung Mai 2026",
        description="Solarmodule und Montage",
        is_credit_note=False,
        commission=None,
        confidence=0.92,
    )
    base.update(overrides)
    return InvoiceData(**base)


# --- fakes ------------------------------------------------------------------

class FakeSourceMoco:
    def __init__(self, pdf_bytes: bytes = b"%PDF-fake"):
        self.pdf_bytes = pdf_bytes
        self.downloads: list[str] = []
        self.download_error: Exception | None = None
        self.searches: list[str] = []
        self.search_result: list[dict] = []
        self.search_error: Exception | None = None
        # get_company is now also exercised — for the VAT-supplier-default
        # fallback path. Default: keyed by id, returns whatever was set.
        self.companies: dict[int, dict] = {}
        self.get_company_error: Exception | None = None

    def download_file(self, signed_url: str) -> bytes:
        self.downloads.append(signed_url)
        if self.download_error:
            raise self.download_error
        return self.pdf_bytes

    def search_suppliers(self, name: str) -> list[dict]:
        self.searches.append(name)
        if self.search_error:
            raise self.search_error
        return self.search_result

    def get_company(self, company_id: int) -> dict:
        if self.get_company_error:
            raise self.get_company_error
        return self.companies.get(company_id, {"id": company_id})


class FakePurchaseClient:
    def __init__(self):
        self.creates: list[dict] = []
        self.next_create_id: int = 4001234
        self.create_error: Exception | None = None
        self.comments: list[tuple[int, str]] = []
        self.comment_error: Exception | None = None
        # Default vat-code list covers the typical Swiss rates so most
        # tests don't need to override it. Shape mirrors the real Moco
        # /vat_code_purchases response: id, tax (in percent), code, active.
        self.vat_codes: list[dict] = [
            {"id": 11, "tax": 8.1, "code": "1", "active": True},
            {"id": 12, "tax": 2.6, "code": "2", "active": True},
            {"id": 13, "tax": 0.0, "code": "0", "active": True},
        ]
        self.vat_codes_error: Exception | None = None

    def list_vat_codes(self) -> list[dict]:
        if self.vat_codes_error:
            raise self.vat_codes_error
        return self.vat_codes

    def create_purchase(self, payload: dict) -> dict:
        if self.create_error:
            raise self.create_error
        self.creates.append(payload)
        return {"id": self.next_create_id, **payload}

    def post_comment(self, purchase_id: int, text: str) -> dict:
        if self.comment_error:
            raise self.comment_error
        self.comments.append((purchase_id, text))
        return {"id": 1}


class FakeOcr:
    def __init__(self, result: InvoiceData | None = None,
                 error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[bytes] = []

    def extract(self, pdf_bytes: bytes) -> InvoiceData:
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


def build_service(*, source_moco=None, purchases=None, ocr=None,
                  telegram=None, source_account_url="solar"):
    return SupplierInvoiceOcrService(
        source_moco=source_moco or FakeSourceMoco(),
        purchase_client=purchases or FakePurchaseClient(),
        ocr=ocr or FakeOcr(result=make_invoice()),
        source_account_url=source_account_url,
        telegram=telegram,
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
    source = FakeSourceMoco(pdf_bytes=pdf)
    ocr = FakeOcr(result=make_invoice())
    purchases = FakePurchaseClient()
    purchases.next_create_id = 4001234
    source.search_result = [{"id": 555, "name": "FLYERALARM"}]
    s = build_service(source_moco=source, purchases=purchases,
                      ocr=ocr, telegram=tg)

    result = s.process("create", {"id": 3001069,
                                  "file_url": "https://x/y.pdf"})

    assert source.downloads == ["https://x/y.pdf"]
    assert ocr.calls == [pdf]
    assert source.searches == ["FLYERALARM"]
    assert len(purchases.creates) == 1
    payload = purchases.creates[0]

    # Required fields are present and correctly mapped.
    assert payload["date"] == "2026-05-12"
    assert payload["currency"] == "CHF"
    # QR-reference + IBAN present → Swiss QR-bill payment method.
    assert payload["payment_method"] == "bank_transfer_swiss_qr_esr"
    # Tags mark this as an OCR-imported purchase for the human reviewer.
    assert payload["tags"] == OCR_TAGS
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
    invoice = make_invoice(qr_reference="999999999999999999999999999")  # OCR-wrong but valid 27d
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
    Moco would interpret null as 'clear this field'."""
    invoice = make_invoice(due_date=None, invoice_number=None,
                           iban=None, qr_reference=None,
                           payment_purpose=None)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    payload = purchases.creates[0]
    for key in ("due_date", "receipt_identifier", "iban", "reference", "info"):
        assert key not in payload, key


def test_credit_note_payload_uses_negative_total():
    """Gutschriften must book as negative — the operator can flip it back
    if the OCR misidentified, but defaulting to negative makes the sign
    match the document semantics."""
    invoice = make_invoice(is_credit_note=True, total_amount=500.0)
    purchases = FakePurchaseClient()
    s = build_service(ocr=FakeOcr(result=invoice), purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["total"] == -500.0


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
    source = FakeSourceMoco()
    source.search_result = []
    purchases = FakePurchaseClient()
    s = build_service(source_moco=source, purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert "company_id" not in purchases.creates[0]


def test_ambiguous_supplier_match_leaves_company_id_unset():
    """Multiple matches → human review needed; better to leave unset than
    auto-link the wrong company (would silently skew reporting)."""
    source = FakeSourceMoco()
    source.search_result = [
        {"id": 100, "name": "FLYERALARM"},
        {"id": 101, "name": "FLYERALARM"},   # duplicate registration
    ]
    purchases = FakePurchaseClient()
    s = build_service(source_moco=source, purchases=purchases)
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert "company_id" not in purchases.creates[0]


def test_supplier_lookup_failure_does_not_fail_sync():
    """The supplier lookup is best-effort — a flapping /companies endpoint
    shouldn't prevent the purchase from being created. Log + continue."""
    source = FakeSourceMoco()
    source.search_error = urlerror.HTTPError(
        "https://x", 500, "boom", {}, fp=None,
    )
    purchases = FakePurchaseClient()
    s = build_service(source_moco=source, purchases=purchases)
    result = s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert len(purchases.creates) == 1
    assert "company_id" not in purchases.creates[0]
    assert result["company_id"] is None


def test_no_supplier_name_skips_lookup():
    """If the model couldn't extract a supplier name at all, don't even
    call the lookup (avoids unnecessary Moco round-trip + log noise)."""
    invoice = make_invoice(supplier_name=None)
    source = FakeSourceMoco()
    s = build_service(source_moco=source, ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert source.searches == []


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
    source = FakeSourceMoco()
    source.search_result = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {
        "id": 555, "name": "FLYERALARM",
        "default_vat_code_purchase_id": 77,
    }
    purchases = FakePurchaseClient()
    s = build_service(source_moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 77


def test_vat_code_supplier_lookup_uses_alternate_field_name():
    """Defensive: Moco's exact company field name isn't fully documented;
    accept `vat_code_purchase_id` in addition to the more-specific
    `default_vat_code_purchase_id`."""
    invoice = make_invoice(vat_rate=None)
    source = FakeSourceMoco()
    source.search_result = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {"id": 555, "vat_code_purchase_id": 88}
    purchases = FakePurchaseClient()
    s = build_service(source_moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 88


def test_vat_code_falls_back_to_supplier_when_ocr_rate_doesnt_match():
    """If OCR's vat_rate has no matching code (e.g. unknown rate from a
    foreign-VAT supplier), fall through to the supplier default rather
    than giving up immediately."""
    invoice = make_invoice(vat_rate=0.19)   # German 19% — no Moco match
    source = FakeSourceMoco()
    source.search_result = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {"id": 555, "default_vat_code_purchase_id": 77}
    purchases = FakePurchaseClient()
    purchases.vat_codes = [{"id": 11, "tax": 8.1, "active": True}, {"id": 12, "tax": 2.6, "active": True}]
    s = build_service(source_moco=source, purchases=purchases,
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
    source = FakeSourceMoco()
    source.search_result = []
    s = build_service(source_moco=source, purchases=purchases,
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


def test_vat_code_id_omitted_when_no_branch_resolves():
    """If all three branches fail (no rate, no supplier default, no
    account default in the vat-code list), the item carries no
    `vat_code_id`. Moco will 422 → the dispatcher fires Telegram + ACKs
    ok=false. The omission keeps the payload valid JSON."""
    invoice = make_invoice(vat_rate=None)
    purchases = FakePurchaseClient()
    purchases.vat_codes = [{"id": 11, "tax": 8.1, "active": True}]  # nothing flagged default
    source = FakeSourceMoco()
    source.search_result = []  # no supplier match either
    s = build_service(source_moco=source, purchases=purchases,
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
    source = FakeSourceMoco()
    source.search_result = [{"id": 555, "name": "FLYERALARM"}]
    source.companies[555] = {"id": 555, "default_vat_code_purchase_id": 77}
    s = build_service(source_moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 77


def test_vat_code_supplier_get_company_failure_falls_through_to_account_default():
    """If the supplier-default branch errors out (e.g. flapping
    get_company), the resolver should still try the account default
    rather than abandoning the run."""
    invoice = make_invoice(vat_rate=None)
    source = FakeSourceMoco()
    source.search_result = [{"id": 555, "name": "FLYERALARM"}]
    source.get_company_error = urlerror.HTTPError(
        "https://x", 500, "boom", {}, fp=None,
    )
    purchases = FakePurchaseClient()
    purchases.vat_codes = [{"id": 99, "tax": 8.1, "active": True, "default": True}]
    s = build_service(source_moco=source, purchases=purchases,
                      ocr=FakeOcr(result=invoice))
    s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})
    assert purchases.creates[0]["items"][0]["vat_code_id"] == 99


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
    assert "CHF 1234.50" in text
    assert "R-2026-042" in text
    assert "Bauvorhaben Müller" in text
    assert "purchases/drafts/3001069" in text   # draft back-link
    assert "Bitte Felder prüfen" in text


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
    assert "<strong>⚠️ Bitte Felder prüfen und freigeben.</strong>" in text
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
    source = FakeSourceMoco()
    source.download_error = urlerror.URLError("connection refused")
    s = build_service(source_moco=source)
    with pytest.raises(urlerror.URLError):
        s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})


def test_create_purchase_error_propagates():
    """If POST /purchases itself fails, the sync can't recover — surface
    the error so the handler maps it to alert/retry."""
    purchases = FakePurchaseClient()
    purchases.create_error = urlerror.HTTPError(
        "https://x", 422, "missing vat_code_id", {}, fp=None,
    )
    s = build_service(purchases=purchases)
    with pytest.raises(urlerror.HTTPError):
        s.process("create", {"id": 1, "file_url": "https://x/y.pdf"})


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
