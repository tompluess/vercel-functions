"""Unit tests for AnthropicOcrClient.

Verify URL construction, auth headers, base64-encoded `document` block shape,
JSON parsing (incl. ```json fence tolerance), numeric coercion, IBAN
normalization, and the error contract (4xx/5xx → AnthropicOcrError with
status_code; URLError propagates so the dispatcher treats it as infra).
"""

import base64
import io
import json
from urllib import error as urlerror

import pytest

import api.anthropic_ocr_client as ocr_mod
from api.anthropic_ocr_client import (
    AnthropicOcrClient,
    AnthropicOcrError,
    InvoiceData,
)
from tests.conftest import FakeUrlopenResponse


# A minimal "PDF" — content doesn't matter, only the base64 round-trip does.
PDF_BYTES = b"%PDF-1.4\n%fake pdf for tests\n"

# Reference structured output the model is supposed to return.
SAMPLE_OCR = {
    "supplier_name": "PVcontracting AG",
    "supplier_address": "Industriestrasse 1, 8005 Zürich",
    "invoice_date": "2026-05-12",
    "due_date": "2026-06-11",
    "invoice_number": "R-2026-042",
    "total_amount": 1234.50,
    "net_amount": 1142.00,
    "vat_amount": 92.50,
    "vat_rate": 0.081,
    "currency": "CHF",
    "iban": "CH93 0076 2011 6238 5295 7",
    "qr_reference": "210000000003139471430009017",
    "payment_purpose": "Rechnung Mai 2026",
    "description": "Solarmodule und Montage",
    "is_credit_note": False,
    "commission": "PV-2026-014 Müller Wallisellen",
    "delivery_address": "Hauptstrasse 5, 8304 Wallisellen",
    "already_paid_by_card": False,
    "confidence": 0.92,
}


def _anthropic_response(text: str) -> bytes:
    """Build a realistic Anthropic /v1/messages response envelope."""
    return json.dumps({
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 200},
    }).encode()


@pytest.fixture
def calls(monkeypatch):
    state: dict = {"calls": [], "next_response": _anthropic_response(json.dumps(SAMPLE_OCR))}

    def fake_urlopen(req, timeout=None):
        state["calls"].append({
            "url": req.full_url,
            "method": req.get_method(),
            "payload": json.loads(req.data) if req.data else None,
            "headers": dict(req.header_items()),
            "timeout": timeout,
        })
        return FakeUrlopenResponse(state["next_response"])

    monkeypatch.setattr(ocr_mod.urlrequest, "urlopen", fake_urlopen)
    return state


@pytest.fixture
def client():
    return AnthropicOcrClient(api_key="sk-ant-test")


def test_extract_posts_to_messages_endpoint_with_required_headers(client, calls):
    client.extract(PDF_BYTES)
    call = calls["calls"][0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["method"] == "POST"
    # Anthropic requires both x-api-key and anthropic-version.
    header_names = {k.lower(): v for k, v in call["headers"].items()}
    assert header_names["x-api-key"] == "sk-ant-test"
    assert header_names["anthropic-version"] == "2023-06-01"
    assert header_names["content-type"] == "application/json"


def test_extract_sends_pdf_as_base64_document_block(client, calls):
    client.extract(PDF_BYTES)
    payload = calls["calls"][0]["payload"]
    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["max_tokens"] >= 512
    assert "system" in payload and "JSON object" in payload["system"]
    content_blocks = payload["messages"][0]["content"]
    doc_block = next(b for b in content_blocks if b["type"] == "document")
    assert doc_block["source"] == {
        "type": "base64",
        "media_type": "application/pdf",
        "data": base64.b64encode(PDF_BYTES).decode("ascii"),
    }
    # A short text instruction should accompany the document.
    assert any(b["type"] == "text" for b in content_blocks)


def test_extract_returns_invoice_data_with_all_fields(client, calls):
    result = client.extract(PDF_BYTES)
    assert isinstance(result, InvoiceData)
    assert result.supplier_name == "PVcontracting AG"
    assert result.invoice_date == "2026-05-12"
    assert result.due_date == "2026-06-11"
    assert result.invoice_number == "R-2026-042"
    assert result.total_amount == pytest.approx(1234.50)
    assert result.net_amount == pytest.approx(1142.00)
    assert result.vat_rate == pytest.approx(0.081)
    assert result.currency == "CHF"
    # IBAN spaces stripped + uppercased.
    assert result.iban == "CH9300762011623852957"
    assert result.qr_reference == "210000000003139471430009017"
    assert result.is_credit_note is False
    assert result.commission == "PV-2026-014 Müller Wallisellen"
    assert result.delivery_address == "Hauptstrasse 5, 8304 Wallisellen"
    assert result.confidence == pytest.approx(0.92)


def test_extract_strips_markdown_fences_around_json(client, calls):
    """Sonnet occasionally wraps the JSON in ```json fences even when told not
    to. We strip them rather than fail the extraction."""
    fenced = "```json\n" + json.dumps(SAMPLE_OCR) + "\n```"
    calls["next_response"] = _anthropic_response(fenced)
    result = client.extract(PDF_BYTES)
    assert result.supplier_name == "PVcontracting AG"


def test_extract_strips_bare_triple_backtick_fences(client, calls):
    """The opening fence may be plain ``` (without `json`). Handle both."""
    fenced = "```\n" + json.dumps(SAMPLE_OCR) + "\n```"
    calls["next_response"] = _anthropic_response(fenced)
    result = client.extract(PDF_BYTES)
    assert result.supplier_name == "PVcontracting AG"


def test_extract_coerces_numeric_strings(client, calls):
    """Models sometimes return amounts as strings ('1234.50'). Coerce."""
    payload = {**SAMPLE_OCR, "total_amount": "1234.50", "vat_rate": "0.081"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.total_amount == pytest.approx(1234.50)
    assert result.vat_rate == pytest.approx(0.081)


def test_extract_strips_spaces_from_qr_reference(client, calls):
    """Regression: Sonnet sometimes mirrors the Swiss QR-bill's printed
    2-5-5-5-5-5 grouping in the JSON output even when told to strip spaces.
    Normalize to digits-only so Moco's `reference` field and Bexio's QR
    payment never see formatting."""
    payload = {**SAMPLE_OCR,
               "qr_reference": "21 00000 00003 13947 14300 09017"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.qr_reference == "210000000003139471430009017"


def test_extract_strips_all_separators_from_qr_reference(client, calls):
    """Defensive: strip dashes / dots / slashes too, not only spaces."""
    payload = {**SAMPLE_OCR,
               "qr_reference": "21-00000.00003/13947 14300\t09017"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.qr_reference == "210000000003139471430009017"


def test_extract_nulls_qr_reference_when_length_is_not_27(client, calls):
    """Strict length: a Swiss QR-Referenznummer is always 27 digits. If OCR
    drops or adds a digit, null the field rather than push a broken
    reference into Moco — a wrong digit would silently mis-route the
    eventual QR payment. The dropped value gets a warning log; the operator
    notices via the side-by-side in Moco + the confidence-based Telegram
    alert (SPEC step 2)."""
    # 26 digits — one short, mirrors the real OCR miss observed in prod
    # ("000000010283608960437 31282" stripped to digits).
    payload = {**SAMPLE_OCR, "qr_reference": "00000001028360896043731282"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.qr_reference is None


def test_extract_nulls_iban_with_invalid_checksum(client, calls):
    """ISO 13616 mod-97: an OCR-mangled IBAN that fails the checksum gets
    nulled rather than passed through. Mod-97 has a ~1% false-positive
    rate so this catches most of the typo-style misreads (one digit
    flipped, two adjacent swapped, etc.)."""
    # The valid checksum for the supplier example would be CH93...; flip
    # one digit so it fails mod-97 but still has 21 chars + starts with CH.
    payload = {**SAMPLE_OCR, "iban": "CH93007620116238 52958"}   # last digit changed
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.iban is None


def test_extract_nulls_iban_with_non_iban_text(client, calls):
    """A non-IBAN string slipping into the field (model halucination)
    fails the checksum and gets dropped."""
    payload = {**SAMPLE_OCR, "iban": "ABC12345DEFGHIJKLMN67"}   # 21 alphanum, but not IBAN
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.iban is None


def test_extract_nulls_qr_reference_when_too_long(client, calls):
    """Symmetric: an over-long reference (model padded with an extra digit)
    is just as broken as a short one."""
    payload = {**SAMPLE_OCR, "qr_reference": "2100000000031394714300090170"}  # 28
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.qr_reference is None


def test_extract_treats_null_optional_fields_as_none(client, calls):
    """Every optional field can be null when not found on the invoice."""
    payload = {**SAMPLE_OCR, "due_date": None, "iban": None,
               "qr_reference": None, "vat_amount": None}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.due_date is None
    assert result.iban is None
    assert result.qr_reference is None
    assert result.vat_amount is None


def test_extract_normalizes_creditor_reference_strips_spaces(client, calls):
    """ISO 11649 SCOR is printed with spacing on Swiss bills
    (`RF87 R003 2202 6060 70000 00000`). Normalize to the canonical
    space-free uppercase form before sending to Moco's reference field."""
    payload = {**SAMPLE_OCR,
               "qr_reference": None,
               "creditor_reference": "rf43 r003 2202 6060 70"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.creditor_reference == "RF43R0032202606070"


def test_extract_nulls_creditor_reference_with_invalid_checksum(client, calls):
    """A SCOR that fails mod-97 (one OCR-misread digit) gets dropped, same
    safety rule as IBAN — better no reference than a wrong one routed at
    the bank."""
    payload = {**SAMPLE_OCR, "qr_reference": None,
               "creditor_reference": "RF44R0032202606070"}   # cd flipped
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.creditor_reference is None


def test_extract_nulls_creditor_reference_without_rf_prefix(client, calls):
    """A reference value that doesn't start with `RF` is not a SCOR — the
    field is dropped rather than silently accepting an arbitrary string."""
    payload = {**SAMPLE_OCR, "qr_reference": None,
               "creditor_reference": "12345678"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.creditor_reference is None


def test_extract_lifts_scor_out_of_payment_purpose(client, calls):
    """Safety net: if the model puts a SCOR string into payment_purpose
    instead of the dedicated creditor_reference field (older runs / prompt
    drift), the parser detects the ISO 11649 pattern, lifts it to the
    creditor_reference, and strips it out of payment_purpose so the Moco
    purchase's info field doesn't carry a duplicate."""
    payload = {**SAMPLE_OCR,
               "qr_reference": None,
               "creditor_reference": None,
               "payment_purpose": "RF43 R003 2202 6060 70 "
                                  "Rechnung Mai 2026"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.creditor_reference == "RF43R0032202606070"
    assert result.payment_purpose == "Rechnung Mai 2026"


def test_extract_returns_already_paid_by_card_when_true(client, calls):
    """OCR detects a card-settled bill (Visa receipt etc.) — the flag
    drives the service into `payment_method=credit_card`."""
    payload = {**SAMPLE_OCR, "already_paid_by_card": True}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.already_paid_by_card is True


def test_extract_defaults_already_paid_by_card_to_false_when_missing(client, calls):
    """When the model omits the field, default to False — open invoices
    are the common case and "paid" must be an explicit signal."""
    payload = {k: v for k, v in SAMPLE_OCR.items() if k != "already_paid_by_card"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.already_paid_by_card is False


def test_extract_leaves_payment_purpose_unchanged_when_no_scor(client, calls):
    """When the payment_purpose carries plain text with no embedded SCOR
    the lift is a no-op."""
    payload = {**SAMPLE_OCR, "qr_reference": None,
               "creditor_reference": None,
               "payment_purpose": "Rechnung Mai 2026"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.creditor_reference is None
    assert result.payment_purpose == "Rechnung Mai 2026"


def test_extract_treats_empty_strings_as_none(client, calls):
    """Some models emit '' instead of null for missing fields."""
    payload = {**SAMPLE_OCR, "iban": "", "qr_reference": "   "}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.iban is None
    assert result.qr_reference is None


def test_extract_defaults_missing_confidence_to_zero(client, calls):
    """Missing confidence shouldn't crash — default to 0.0 so the service
    treats it as low-confidence and prompts manual review."""
    payload = {k: v for k, v in SAMPLE_OCR.items() if k != "confidence"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.confidence == 0.0


def test_extract_raises_on_4xx_with_status_code(client, monkeypatch):
    """4xx → AnthropicOcrError with status_code set so the dispatcher can
    map it to an application error (Telegram + 200 ok=false)."""
    def boom(req, timeout=None):
        raise urlerror.HTTPError(
            req.full_url, 400, "Bad Request", {},
            fp=io.BytesIO(b'{"error":{"type":"invalid_request_error"}}'),
        )

    monkeypatch.setattr(ocr_mod.urlrequest, "urlopen", boom)
    with pytest.raises(AnthropicOcrError) as exc_info:
        client.extract(PDF_BYTES)
    assert exc_info.value.status_code == 400
    assert "invalid_request_error" in (exc_info.value.body or "")


def test_extract_raises_on_5xx_with_status_code(client, monkeypatch):
    """5xx is still wrapped as AnthropicOcrError (spec); the status_code
    lets the dispatcher decide it's infrastructure → HTTP 502."""
    def boom(req, timeout=None):
        raise urlerror.HTTPError(
            req.full_url, 529, "Overloaded", {},
            fp=io.BytesIO(b"server overloaded"),
        )

    monkeypatch.setattr(ocr_mod.urlrequest, "urlopen", boom)
    with pytest.raises(AnthropicOcrError) as exc_info:
        client.extract(PDF_BYTES)
    assert exc_info.value.status_code == 529


def test_extract_propagates_url_error(client, monkeypatch):
    """A network failure (DNS, connection refused, timeout) propagates as
    URLError so the dispatcher returns 502 without going through Telegram."""
    def boom(req, timeout=None):
        raise urlerror.URLError("anthropic unreachable")

    monkeypatch.setattr(ocr_mod.urlrequest, "urlopen", boom)
    with pytest.raises(urlerror.URLError):
        client.extract(PDF_BYTES)


def test_extract_raises_when_model_returns_non_json(client, calls):
    """Model returned text but it contains no JSON object at all — treat as
    app error (status_code=None) since retrying won't help."""
    calls["next_response"] = _anthropic_response("Sorry, I can't read this PDF.")
    with pytest.raises(AnthropicOcrError) as exc_info:
        client.extract(PDF_BYTES)
    assert exc_info.value.status_code is None


def test_extract_raises_when_response_has_no_content_block(client, calls):
    """Defensive: a 200 with an empty content array shouldn't crash with a
    cryptic IndexError; surface it as AnthropicOcrError."""
    calls["next_response"] = json.dumps({
        "id": "msg_x", "type": "message", "role": "assistant",
        "content": [],
    }).encode()
    with pytest.raises(AnthropicOcrError):
        client.extract(PDF_BYTES)


def test_extract_tolerates_reasoning_preamble_before_json(client, calls):
    """Regression: when length-check guidance triggers Sonnet to think out
    loud (correct IBAN/QR-Ref but with a preamble like '**IBAN analysis:**
    ... CH72 3000 5254 2480 0603 A ...'), the parser must scan past the
    text and find the first balanced JSON object. Observed in real OCR runs
    against PVcontracting invoices — the prompt's length constraints fix the
    digit-padding bug, but trade off strict 'JSON only' compliance."""
    preamble = (
        "I need to carefully extract the data, paying special attention to "
        "the IBAN and QR reference number from the payment slip.\n\n"
        "**IBAN analysis:**\n"
        "From the Zahlteil: `CH72 3000 5254 2480 0603 A`\n"
        "- 5 groups of 4 + 1 trailing char = 21 chars ✓\n\n"
        "Here is the extracted data:\n\n"
    )
    calls["next_response"] = _anthropic_response(preamble + json.dumps(SAMPLE_OCR))
    result = client.extract(PDF_BYTES)
    assert result.supplier_name == "PVcontracting AG"
    assert result.iban == "CH9300762011623852957"


def test_extract_finds_json_with_braces_inside_string_values(client, calls):
    """The brace scanner must respect JSON string literals so an `{` or `}`
    inside a description doesn't break depth tracking."""
    payload = {**SAMPLE_OCR,
               "description": "Set {x} = 1; cost {a,b,c} per kWp"}
    calls["next_response"] = _anthropic_response("Preamble.\n" + json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.description == "Set {x} = 1; cost {a,b,c} per kWp"


def test_extract_raises_when_text_has_no_brace_at_all(client, calls):
    """No `{` in the response → can't extract any JSON object."""
    calls["next_response"] = _anthropic_response("This invoice is unreadable.")
    with pytest.raises(AnthropicOcrError):
        client.extract(PDF_BYTES)


def test_extract_raises_when_balanced_object_is_malformed(client, calls):
    """A balanced `{...}` substring that isn't valid JSON (e.g. trailing
    comma, single-quoted keys) still surfaces as AnthropicOcrError."""
    calls["next_response"] = _anthropic_response("Here: {supplier_name: 'X',}")
    with pytest.raises(AnthropicOcrError):
        client.extract(PDF_BYTES)


def test_extract_returns_credit_note_true_when_model_says_so(client, calls):
    """A Gutschrift document must surface as is_credit_note=True so the
    downstream service can book it with a negative gross_total / "GS-"
    numbering, not as a regular Rechnung."""
    payload = {**SAMPLE_OCR, "is_credit_note": True}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.is_credit_note is True


def test_extract_defaults_credit_note_to_false_when_missing(client, calls):
    """Missing / null / unrecognized values default to False — most supplier
    documents are regular invoices, so False is the safe baseline."""
    payload = {k: v for k, v in SAMPLE_OCR.items() if k != "is_credit_note"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    assert client.extract(PDF_BYTES).is_credit_note is False

    null_payload = {**SAMPLE_OCR, "is_credit_note": None}
    calls["next_response"] = _anthropic_response(json.dumps(null_payload))
    assert client.extract(PDF_BYTES).is_credit_note is False


def test_extract_coerces_string_booleans_for_credit_note(client, calls):
    """Sonnet usually returns native bool, but other models / a sloppy run
    may emit 'true' / 'yes' / 'ja' — accept all three so we don't lose a
    Gutschrift over JSON-formatting nitpicks."""
    for raw in ("true", "True", "yes", "ja", "1"):
        payload = {**SAMPLE_OCR, "is_credit_note": raw}
        calls["next_response"] = _anthropic_response(json.dumps(payload))
        assert client.extract(PDF_BYTES).is_credit_note is True, raw
    for raw in ("false", "no", "nein", "", "maybe", "0"):
        payload = {**SAMPLE_OCR, "is_credit_note": raw}
        calls["next_response"] = _anthropic_response(json.dumps(payload))
        assert client.extract(PDF_BYTES).is_credit_note is False, raw


def test_extract_returns_commission_when_present(client, calls):
    """Commission / Kommission / Objekt — used downstream to assign the
    Moco project. The value is whatever the invoice prints; we don't
    normalize it (free-form site identifier or address)."""
    payload = {**SAMPLE_OCR, "commission": "Bauvorhaben Solaranlage Müller, Wallisellen"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    result = client.extract(PDF_BYTES)
    assert result.commission == "Bauvorhaben Solaranlage Müller, Wallisellen"


def test_extract_commission_is_none_when_missing(client, calls):
    """Many supplier invoices carry no commission/object reference. null
    must round-trip as None so the service doesn't push an empty string
    into Moco's project field."""
    payload = {**SAMPLE_OCR, "commission": None}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    assert client.extract(PDF_BYTES).commission is None

    no_field = {k: v for k, v in SAMPLE_OCR.items() if k != "commission"}
    calls["next_response"] = _anthropic_response(json.dumps(no_field))
    assert client.extract(PDF_BYTES).commission is None


def test_extract_commission_whitespace_only_becomes_none(client, calls):
    """A whitespace-only commission ('   ') is effectively absent — return
    None so downstream Moco-project matching doesn't try to look up an
    empty identifier."""
    payload = {**SAMPLE_OCR, "commission": "   "}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    assert client.extract(PDF_BYTES).commission is None


def test_constructor_accepts_custom_model_override(calls):
    """Lets the validation script try a different model (e.g. Opus) without
    code changes when comparing OCR quality."""
    custom = AnthropicOcrClient(api_key="sk-ant-test", model="claude-opus-4-7")
    custom.extract(PDF_BYTES)
    assert calls["calls"][0]["payload"]["model"] == "claude-opus-4-7"


# --- extract_energy_bill (smart-me Energiekostenabrechnung schema) ------------

SAMPLE_ENERGY_OCR = {
    "objekt": "Gesamtverbrauch (Hauptstrasse 33 Leimbach)",
    "net_amount": 558.09,
    "period_from": "2026-01-01",
    "period_to": "2026-06-30",
    "invoice_date": "2026-07-05",
    "invoice_number": "10007",
    "confidence": 0.95,
}


def test_extract_energy_bill_sends_energy_prompt(client, calls):
    calls["next_response"] = _anthropic_response(
        json.dumps(SAMPLE_ENERGY_OCR))
    client.extract_energy_bill(PDF_BYTES)
    payload = calls["calls"][0]["payload"]
    # Own system prompt — the invoice schema must not leak in.
    assert "Energiekostenabrechnung" in payload["system"]
    assert "qr_reference" not in payload["system"]
    # Same document-block transport as extract().
    doc_block = next(b for b in payload["messages"][0]["content"]
                     if b["type"] == "document")
    assert doc_block["source"]["data"] == base64.b64encode(
        PDF_BYTES).decode("ascii")


def test_extract_energy_bill_parses_all_fields(client, calls):
    calls["next_response"] = _anthropic_response(
        json.dumps(SAMPLE_ENERGY_OCR))
    bill = client.extract_energy_bill(PDF_BYTES)
    assert bill.objekt == "Gesamtverbrauch (Hauptstrasse 33 Leimbach)"
    assert bill.net_amount == 558.09
    assert bill.period_from == "2026-01-01"
    assert bill.period_to == "2026-06-30"
    assert bill.invoice_date == "2026-07-05"
    assert bill.invoice_number == "10007"
    assert bill.confidence == 0.95


def test_extract_energy_bill_coerces_numeric_strings(client, calls):
    payload = {**SAMPLE_ENERGY_OCR, "net_amount": "558.09",
               "confidence": "0.9"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    bill = client.extract_energy_bill(PDF_BYTES)
    assert bill.net_amount == 558.09
    assert bill.confidence == 0.9


def test_extract_energy_bill_missing_fields_default_safely(client, calls):
    calls["next_response"] = _anthropic_response(json.dumps({}))
    bill = client.extract_energy_bill(PDF_BYTES)
    assert bill.objekt is None
    assert bill.net_amount is None
    assert bill.period_from is None
    assert bill.invoice_number is None
    assert bill.confidence == 0.0


def test_extract_energy_bill_tolerates_reasoning_preamble(client, calls):
    text = ("Let me check the Übersicht table first...\n\n"
            + json.dumps(SAMPLE_ENERGY_OCR))
    calls["next_response"] = _anthropic_response(text)
    bill = client.extract_energy_bill(PDF_BYTES)
    assert bill.net_amount == 558.09


# --- extract_energy_credit_note ------------------------------------------------

SAMPLE_CREDIT_NOTE_OCR = {
    "objekt": "Produktion PVA HEIV Meierhofweg 10",
    "net_amount": 3580.58,
    "vat_rate": 0.081,
    "period_from": "2026-04-01",
    "period_to": "2026-06-30",
    "invoice_date": "2026-07-31",
    "invoice_number": "600949594",
    "confidence": 0.93,
}


def test_extract_energy_credit_note_sends_own_prompt(client, calls):
    calls["next_response"] = _anthropic_response(
        json.dumps(SAMPLE_CREDIT_NOTE_OCR))
    client.extract_energy_credit_note(PDF_BYTES)
    payload = calls["calls"][0]["payload"]
    assert "production credit" in payload["system"]
    assert "qr_reference" not in payload["system"]
    doc_block = next(b for b in payload["messages"][0]["content"]
                     if b["type"] == "document")
    assert doc_block["source"]["data"] == base64.b64encode(
        PDF_BYTES).decode("ascii")


def test_extract_energy_credit_note_parses_all_fields(client, calls):
    calls["next_response"] = _anthropic_response(
        json.dumps(SAMPLE_CREDIT_NOTE_OCR))
    credit = client.extract_energy_credit_note(PDF_BYTES)
    assert credit.objekt == "Produktion PVA HEIV Meierhofweg 10"
    assert credit.net_amount == 3580.58
    assert credit.vat_rate == pytest.approx(0.081)
    assert credit.period_from == "2026-04-01"
    assert credit.period_to == "2026-06-30"
    assert credit.invoice_date == "2026-07-31"
    assert credit.invoice_number == "600949594"
    assert credit.confidence == 0.93


def test_extract_energy_credit_note_normalizes_negative_net_amount():
    """Real-world regression (draft 3143992, EGBB): some EVUs frame their
    ENTIRE statement as a negative payout figure ("Elektrizität
    Rücklieferung -908.25", net -840.20) rather than CKW's plain-positive
    convention. `net_amount` must always come out positive — a hard
    code-level guarantee, not just prompt wording."""
    from api.anthropic_ocr_client import _to_energy_credit_note_data
    credit = _to_energy_credit_note_data(
        {**SAMPLE_CREDIT_NOTE_OCR, "net_amount": -840.20})
    assert credit.net_amount == 840.20


def test_extract_energy_credit_note_missing_net_amount_stays_none():
    from api.anthropic_ocr_client import _to_energy_credit_note_data
    credit = _to_energy_credit_note_data(
        {**SAMPLE_CREDIT_NOTE_OCR, "net_amount": None})
    assert credit.net_amount is None


def test_extract_energy_credit_note_coerces_numeric_strings(client, calls):
    payload = {**SAMPLE_CREDIT_NOTE_OCR, "net_amount": "-3580.58",
               "confidence": "0.9"}
    calls["next_response"] = _anthropic_response(json.dumps(payload))
    credit = client.extract_energy_credit_note(PDF_BYTES)
    assert credit.net_amount == 3580.58
    assert credit.confidence == 0.9


def test_extract_energy_credit_note_missing_fields_default_safely(client, calls):
    calls["next_response"] = _anthropic_response(json.dumps({}))
    credit = client.extract_energy_credit_note(PDF_BYTES)
    assert credit.objekt is None
    assert credit.net_amount is None
    assert credit.vat_rate is None
    assert credit.period_from is None
    assert credit.invoice_number is None
    assert credit.confidence == 0.0


def test_extract_energy_credit_note_tolerates_reasoning_preamble(client, calls):
    text = ("Let me check which section is the production credit...\n\n"
            + json.dumps(SAMPLE_CREDIT_NOTE_OCR))
    calls["next_response"] = _anthropic_response(text)
    credit = client.extract_energy_credit_note(PDF_BYTES)
    assert credit.net_amount == 3580.58
