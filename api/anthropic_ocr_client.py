"""AnthropicOcrClient — Claude Vision wrapper for supplier-invoice PDF OCR.

Thin transport over `POST https://api.anthropic.com/v1/messages` that sends a
PDF as a `document` content block and asks Claude (vision-capable Sonnet 4.6)
to return structured invoice data as JSON. Pure: no business logic, no I/O
beyond the single API call. Used by `SupplierInvoiceOcrService` (step 2 of
the SPEC) so it can be unit-tested with a `FakeAnthropicOcrClient`.

Auth: `x-api-key: {ANTHROPIC_API_KEY}` + `anthropic-version: 2023-06-01`
(Anthropic's required headers — Bearer is not accepted).

Error contract — aligned with the project's 2xx/5xx split (`CLAUDE.md`):

  - Any 4xx or 5xx from Anthropic → raise `AnthropicOcrError` with
    `status_code` set. The endpoint dispatcher branches on `.status_code`
    (4xx → 200 ok=false + Telegram, 5xx → 502 retry).
  - A non-JSON / malformed model response → `AnthropicOcrError` with
    `status_code=None` (the request succeeded but the model misbehaved;
    treated as an application error — there's nothing to retry).
  - Network failures (URLError, timeout) propagate unchanged so the caller
    can treat them as infrastructure and return a 502.

Docs: https://docs.anthropic.com/en/api/messages
"""

import base64
import json
import logging
from dataclasses import dataclass
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger("anthropic_ocr_client")


@dataclass
class InvoiceData:
    """Structured invoice fields extracted from a PDF.

    Every field except `confidence` is `None` when the model couldn't find it
    on the invoice — the caller (SupplierInvoiceOcrService) skips null fields
    when building the Moco PATCH payload. `confidence` is the model's own
    overall extraction confidence and is used by the service to decide
    whether to surface a "please review" Telegram alert.
    """

    supplier_name: str | None
    supplier_address: str | None
    invoice_date: str | None
    due_date: str | None
    invoice_number: str | None
    total_amount: float | None
    net_amount: float | None
    vat_amount: float | None
    vat_rate: float | None
    currency: str | None
    iban: str | None
    qr_reference: str | None
    payment_purpose: str | None
    description: str | None
    # Gutschrift / Rechnung discriminator. Defaults to False (the common
    # case) so callers can branch with `if invoice.is_credit_note: ...`
    # without juggling Optional[bool] semantics. A true credit note ends
    # up booked differently in Moco/Bexio (negative gross_total, "GS-"
    # numbering prefix), so getting this wrong silently is worse than
    # not extracting it.
    is_credit_note: bool
    # Project / site identifier ("Kommission", "Objekt", "Auftragsnummer",
    # "Bauvorhaben") — used downstream to auto-assign the resulting
    # purchase to the matching Moco project. Optional: many supplier
    # invoices don't carry one, and we don't want to invent a value.
    commission: str | None
    confidence: float


class AnthropicOcrError(Exception):
    """Raised on any non-2xx from Anthropic or on a malformed model response.

    `status_code` is the HTTP status when the failure was an Anthropic API
    error, or `None` when the API returned 200 but the model's text wasn't
    parseable as JSON / didn't conform to the expected shape. The endpoint
    dispatcher uses this to map 4xx → application error and 5xx → 502.
    """

    def __init__(self, message: str, *, status_code: int | None = None,
                 body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


SYSTEM_PROMPT = (
    "You are an invoice data extraction assistant for a Swiss solar energy "
    "company (PVcontracting AG). Extract structured data from the supplied "
    "invoice PDF. Respond ONLY with a JSON object — no preamble, no markdown "
    "fences.\n\n"
    # Swiss QR-bills print the payment fields as plain text in the lower
    # "Zahlteil" / "Empfangsschein" area AND as a 2D Swiss QR Code. Without
    # this guidance Sonnet sometimes tries to decode the QR Code visually,
    # which produces near-correct-but-not-quite output (an extra leading 0
    # on the QR-Referenz, an extra digit in the IBAN account portion, etc).
    # Length constraints below force a re-check before responding.
    "IMPORTANT — Swiss QR-bill payment slip (lower portion of the page):\n"
    "  - IBAN, QR-Referenznummer and Zahlungszweck are printed as plain "
    "text in the 'Zahlteil' / 'Empfangsschein' section. ALWAYS read them "
    "from the printed text (usually grouped in blocks of 4 / 5 digits). "
    "NEVER attempt to decode the 2D Swiss QR Code image itself — vision "
    "decoding adds spurious digits.\n"
    "  - Invoices often print MULTIPLE IBANs: a contact-info / "
    "letterhead IBAN at the top, and the payment IBAN on the Zahlteil at "
    "the bottom. EXTRACT ONLY THE ZAHLTEIL IBAN — that is the one money "
    "is actually transferred to. If the document only has one IBAN (no "
    "separate Zahlteil), use that. Do not blend digits from different "
    "IBANs.\n"
    "  - Swiss IBAN: exactly 21 characters — 'CH' + 2 check digits + 5 "
    "digit bank code + 12 ALPHANUMERIC account characters. The account "
    "portion is NOT necessarily all digits: real-world Swiss IBANs often "
    "contain uppercase letters (e.g. 'CH22 3000 00DE 1611 6572 0' has "
    "'DE' inside the account part). Read each character literally — do "
    "NOT silently convert letters to digits to make it look 'cleaner'. "
    "Strip whitespace. If your extraction is not exactly 21 characters, "
    "re-examine the printed IBAN (re-count each group of 4) before "
    "responding.\n"
    "  - QR-Referenznummer: exactly 27 digits including leading zeros. "
    "If your extraction is not exactly 27 digits, re-examine the printed "
    "reference number (it is grouped 2-5-5-5-5-5 in the Zahlteil) before "
    "responding.\n"
    "  - If a field on the QR slip is ambiguous or you cannot meet the "
    "length constraint with confidence, set the field to null and lower "
    "your overall confidence rather than guessing.\n\n"
    "Required fields (null if not found):\n"
    "{\n"
    '  "supplier_name": "string — company or person name on the invoice",\n'
    '  "supplier_address": "string — full address",\n'
    '  "invoice_date": "string — ISO 8601 date (YYYY-MM-DD)",\n'
    '  "due_date": "string — ISO 8601 date or null",\n'
    '  "invoice_number": "string — invoice/Rechnungsnummer",\n'
    '  "total_amount": "number — total including VAT, in CHF",\n'
    '  "net_amount": "number — total excluding VAT or null",\n'
    '  "vat_amount": "number — VAT amount or null",\n'
    '  "vat_rate": "number — VAT rate as decimal (e.g. 0.081) or null",\n'
    '  "currency": "string — ISO 4217 (usually CHF)",\n'
    '  "iban": "string — IBAN without spaces, exactly 21 chars for CH, or null",\n'
    '  "qr_reference": "string — QR-Referenznummer, exactly 27 digits, or null",\n'
    '  "payment_purpose": "string — Zahlungszweck/Mitteilung or null",\n'
    '  "description": "string — brief description of goods/services (max 75 chars)",\n'
    '  "is_credit_note": "boolean — true if this is a credit note '
    '(Gutschrift / Stornorechnung) rather than a regular invoice (Rechnung). '
    'Decide based on the document header (e.g. \\"Gutschrift\\" instead of '
    '\\"Rechnung\\"), an explicitly negative total, or wording like '
    '\\"Wir schreiben Ihnen gut\\". Default false.",\n'
    '  "commission": "string — Kommission / Objekt / Auftragsnummer / '
    'Bauvorhaben / Baustelle — typically a short project identifier or '
    'site address printed on the invoice header or reference block (used '
    'downstream to assign the purchase to a Moco project). null if not '
    'explicitly present — do not infer from the supplier address.",\n'
    '  "confidence": "number — your overall extraction confidence 0.0–1.0"\n'
    "}"
)


class AnthropicOcrClient:
    HTTP_TIMEOUT_SECONDS = 90  # PDF upload + model inference can dominate
    BASE_URL = "https://api.anthropic.com"
    DEFAULT_MODEL = "claude-sonnet-4-6"
    ANTHROPIC_VERSION = "2023-06-01"
    MAX_TOKENS = 1024

    def __init__(self, *, api_key: str, model: str | None = None):
        self._api_key = api_key
        self._model = model or self.DEFAULT_MODEL

    def extract(self, pdf_bytes: bytes) -> InvoiceData:
        """Run OCR on a PDF and return the parsed `InvoiceData`.

        Raises `AnthropicOcrError` on a non-2xx response or a non-conforming
        model output. Network errors (URLError, timeout) propagate.
        """
        encoded = base64.b64encode(pdf_bytes).decode("ascii")
        payload = {
            "model": self._model,
            "max_tokens": self.MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": encoded,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Extract the invoice data as JSON.",
                    },
                ],
            }],
        }
        response_body = self._post_messages(payload)
        text = _extract_text(response_body)
        data = _parse_invoice_json(text)
        return _to_invoice_data(data)

    def _post_messages(self, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = urlrequest.Request(
            f"{self.BASE_URL}/v1/messages",
            data=data, method="POST",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self.ANTHROPIC_VERSION,
                "content-type": "application/json",
                "accept": "application/json",
            },
        )
        try:
            with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
        except urlerror.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                pass
            raise AnthropicOcrError(
                f"Anthropic API {e.code}: {body}",
                status_code=e.code, body=body,
            ) from e
        try:
            return json.loads(raw)
        except ValueError as e:
            raise AnthropicOcrError(
                f"Anthropic API returned non-JSON response: {e}",
            ) from e


# --- helpers ----------------------------------------------------------------

def _extract_text(response_body: dict) -> str:
    """Pull the assistant text out of an Anthropic messages response.

    Shape: `{"content": [{"type": "text", "text": "..."}, ...]}`. We
    concatenate any text blocks defensively — the model usually returns one,
    but mixed content blocks are valid per the API.
    """
    content = response_body.get("content")
    if not isinstance(content, list) or not content:
        raise AnthropicOcrError(
            f"Anthropic response had no content blocks: {response_body!r}"
        )
    chunks = [b.get("text", "") for b in content
              if isinstance(b, dict) and b.get("type") == "text"]
    text = "".join(chunks).strip()
    if not text:
        raise AnthropicOcrError(
            f"Anthropic response had no text content: {response_body!r}"
        )
    return text


def _parse_invoice_json(text: str) -> dict:
    """Pull the first balanced JSON object out of the model's text.

    The system prompt asks for "JSON only, no preamble", but the length-check
    guidance (re-examine IBAN if not 21 chars / QR-Ref if not 27 digits)
    sometimes triggers Sonnet to think out loud before emitting JSON — e.g.
    "I need to carefully extract the data... **IBAN analysis:** ... {...}".
    The reasoning improved accuracy in practice (correct IBAN/QR-Ref vs
    invented digits), so we keep the prompt and just let the parser scan
    past the preamble to find the first balanced `{...}` object.

    Also tolerates ```json``` fences and leading whitespace.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()

    obj_text = _find_first_json_object(stripped)
    if obj_text is None:
        raise AnthropicOcrError(
            f"No JSON object found in model response: text={text[:300]!r}"
        )
    try:
        data = json.loads(obj_text)
    except ValueError as e:
        raise AnthropicOcrError(
            f"Model response had a malformed JSON object: {e}; "
            f"object={obj_text[:300]!r}"
        ) from e
    if not isinstance(data, dict):
        raise AnthropicOcrError(
            f"Model response was JSON but not an object: {type(data).__name__}"
        )
    return data


def _find_first_json_object(text: str) -> str | None:
    """Return the first balanced `{...}` substring, or None.

    Scans for matching braces while respecting JSON string literals (so an
    `{` inside a string value doesn't bump the depth). Robust enough for the
    "preamble then JSON" shape Sonnet produces when it reasons out loud;
    not a full JSON parser — `json.loads` does that on the result.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _to_invoice_data(data: dict) -> InvoiceData:
    """Build an `InvoiceData` from the parsed JSON.

    Numeric fields are coerced from strings if the model returns them as
    strings — observed in practice with "1234.50". Confidence defaults to
    0.0 when missing so a slightly off-spec response still produces a
    usable result that triggers manual review downstream.
    """
    return InvoiceData(
        supplier_name=_str_or_none(data.get("supplier_name")),
        supplier_address=_str_or_none(data.get("supplier_address")),
        invoice_date=_str_or_none(data.get("invoice_date")),
        due_date=_str_or_none(data.get("due_date")),
        invoice_number=_str_or_none(data.get("invoice_number")),
        total_amount=_float_or_none(data.get("total_amount")),
        net_amount=_float_or_none(data.get("net_amount")),
        vat_amount=_float_or_none(data.get("vat_amount")),
        vat_rate=_float_or_none(data.get("vat_rate")),
        currency=_str_or_none(data.get("currency")),
        iban=_normalize_iban(data.get("iban")),
        qr_reference=_normalize_qr_reference(data.get("qr_reference")),
        payment_purpose=_str_or_none(data.get("payment_purpose")),
        description=_str_or_none(data.get("description")),
        is_credit_note=_bool_or_false(data.get("is_credit_note")),
        commission=_str_or_none(data.get("commission")),
        confidence=_float_or_none(data.get("confidence")) or 0.0,
    )


def _str_or_none(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _bool_or_false(value) -> bool:
    """Coerce the model's `is_credit_note` to a real bool.

    Sonnet usually returns native booleans, but occasionally emits the
    strings "true"/"false" or "yes"/"no" — accept those too. Anything
    unrecognized (including None / missing field) defaults to False,
    which is the safe baseline since most supplier documents are invoices.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "ja", "1"}
    return False


def _float_or_none(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


QR_REFERENCE_LENGTH = 27


def _normalize_qr_reference(value) -> str | None:
    """QR-Referenznummer stripped to digits only — exactly 27 digits or None.

    The Swiss QR-bill payment slip prints the 27-digit reference in groups
    (`XX XXXXX XXXXX XXXXX XXXXX XXXXX`), and Sonnet sometimes echoes those
    spaces in its JSON output even though the prompt says "no spaces". The
    canonical form is digits-only — strip anything else so downstream
    consumers (Moco `reference`, Bexio QR payment) never see a formatted
    variant.

    Length is enforced strictly: anything other than 27 digits returns None
    (with a warning logged) so we don't push a broken reference into Moco
    that would silently mis-route the eventual QR payment. The model's
    confidence score plus the Telegram review alert is the human-facing
    signal that a field was dropped; the operator can correct it in the
    Moco draft.
    """
    if value is None:
        return None
    cleaned = "".join(c for c in str(value) if c.isdigit())
    if not cleaned:
        return None
    if len(cleaned) != QR_REFERENCE_LENGTH:
        logger.warning("OCR returned QR-reference with wrong length: "
                       "%d digits (expected %d), nulling field. raw=%r",
                       len(cleaned), QR_REFERENCE_LENGTH, value)
        return None
    return cleaned


def _normalize_iban(value) -> str | None:
    """IBAN normalized + mod-97 checksum-validated.

    Strips non-alphanum, uppercases (so downstream consumers never see a
    "DE89 3704 ..." formatted variant — same shape Bexio expects, see
    `bexio_expense_sync_service._moco_iban`).

    Then validates the ISO 13616 check digits: rearrange (move first 4
    chars to end), replace letters with their A=10..Z=35 codes, the
    resulting integer must be ≡ 1 (mod 97). Invalid → null + warn.

    Why null on checksum failure: Sonnet occasionally mis-OCRs Swiss
    QR-IBANs that contain alphanumeric characters in the account portion
    (observed: real `CH22 3000 00DE 1611 6572 0` read as
    `CH3909000000161165720` — the model dropped the "00DE" and re-padded
    with digits). The mangled IBAN passes the 21-char length gate but
    fails mod-97; nulling it out is far safer than pushing it to Moco,
    where a wrong IBAN would silently send the QR-bill payment to the
    wrong account.
    """
    if value is None:
        return None
    cleaned = "".join(c for c in str(value) if c.isalnum()).upper()
    if not cleaned:
        return None
    if not _iban_checksum_valid(cleaned):
        logger.warning("OCR returned IBAN with invalid mod-97 checksum, "
                       "nulling field. raw=%r normalized=%r",
                       value, cleaned)
        return None
    return cleaned


def _iban_checksum_valid(iban: str) -> bool:
    """ISO 13616 mod-97 check.

    Rearrange (first 4 chars to end), replace each letter with its
    A=10, B=11, ..., Z=35 numeric code, treat the resulting digit string
    as one large integer that must be ≡ 1 modulo 97. Pure: no I/O.
    """
    if len(iban) < 4:
        return False
    rearranged = iban[4:] + iban[:4]
    digits: list[str] = []
    for c in rearranged:
        if c.isdigit():
            digits.append(c)
        elif "A" <= c <= "Z":
            digits.append(str(ord(c) - ord("A") + 10))
        else:
            return False
    try:
        return int("".join(digits)) % 97 == 1
    except ValueError:
        return False
