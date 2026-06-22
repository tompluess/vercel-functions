"""SupplierInvoiceOcrService — turn a Moco draft into a real Moco purchase
pre-filled with Claude-Vision OCR data.

Approach: Moco's email-import drops supplier invoices in as **draft**
purchases, but those drafts can't be patched via the API (PATCH
/purchases/drafts/{id} → 404). So this service instead:

  1. Downloads the PDF attached to the draft.
  2. Runs OCR via `AnthropicOcrClient` → `InvoiceData`.
  3. Looks up the supplier in Moco's companies list — if exactly one
     supplier matches by name (case-insensitive exact), links its
     `company_id`. Ambiguous / no match → leave empty for the human.
  4. Creates a NEW real purchase via `POST /purchases` containing:
       - the extracted fields (date, due_date, currency, totals, IBAN,
         QR-reference, payment note, title)
       - the PDF base64-encoded under `file: {filename, base64}`
       - `tags: ["OCR", "Review pending"]` so the operator can filter
         these in Moco's UI
       - `company_id` if a supplier match was found
       - `payment_method` derived from OCR (`bank_transfer_swiss_qr_esr`
         when a QR-reference is present, otherwise `bank_transfer`)
  5. Posts a comment on the new purchase summarising the OCR
     (incl. the special fields `is_credit_note` + `commission`).
  6. Sends a Telegram alert (confidence-routed; Gutschrift always
     triggers the Vorzeichen-prüf warning).

The original draft is left untouched — the operator can delete it
manually after verifying the new real purchase. The webhook payload
itself is the draft; we only use its `id` (for the back-reference in
the comment text) and its `file_url`.

VAT-code resolution: Moco's POST /purchases requires `vat_code_id` on
every item. The service resolves it dynamically — `GET /vat_code_purchases`
to list the available codes, then:
  1. If OCR extracted a `vat_rate`, find the code whose `value` matches
     (tolerant of percent-vs-decimal formats and tiny rounding).
  2. Else if the supplier was matched in Moco's company list, use that
     company's `default_vat_code_purchase_id` (fetched via get_company).
  3. Else: use the code marked `default: true` in `/vat_code_purchases`
     (Moco accounts typically have one designated default for purchases).
  4. Only if all three fail (no vat-code list reachable AND no default
     flagged): omit the field — Moco 422s, the dispatcher fires a
     Telegram alert + ACKs 200 ok=false. Rare in practice.
"""

import base64
import logging
import re
from dataclasses import replace
from html import escape
from typing import Any
from urllib import error as urlerror

from api.anthropic_ocr_client import (
    AnthropicOcrClient,
    InvoiceData,
    _normalize_iban,
    _normalize_qr_reference,
)
from api.moco_purchase_client import MocoPurchaseClient
from api.source_moco_client import SourceMocoClient
from api.telegram_notifier import TelegramNotifier

logger = logging.getLogger("supplier_invoice_ocr_service")

CONFIDENCE_THRESHOLD = 0.85
OCR_TAGS = ["OCR", "Review pending"]


class SupplierInvoiceOcrService:
    def __init__(self, *, source_moco: SourceMocoClient,
                 purchase_client: MocoPurchaseClient,
                 ocr: AnthropicOcrClient,
                 source_account_url: str,
                 telegram: TelegramNotifier | None = None):
        self._source_moco = source_moco
        self._purchases = purchase_client
        self._ocr = ocr
        self._source_account_url = source_account_url
        self._telegram = telegram

    def process(self, event: str, body: dict) -> dict[str, Any]:
        """Drive one Purchase webhook through OCR + new-purchase creation.

        Returns a dict whose contents are merged into the HTTP envelope by
        the endpoint dispatcher. `skipped` keys mark non-error early-outs
        (event filter, missing attachment); the absence of `skipped` means
        a new purchase was created.
        """
        if event != "create":
            logger.info("ocr: skipped event=%s (only 'create' is processed)",
                        event)
            return {"skipped": "event_not_create"}

        draft_id = body.get("id")
        file_url = body.get("file_url")
        if not draft_id:
            logger.warning("ocr: skipped (no draft id) body_keys=%s",
                           sorted(body.keys()))
            self._notify("⚠️ OCR übersprungen — Webhook ohne Draft-ID")
            return {"skipped": "no_purchase_id"}
        if not file_url:
            logger.warning("ocr: skipped (no file_url) draft_id=%s", draft_id)
            self._notify(
                "⚠️ OCR übersprungen — Draft ohne Anhang: "
                f"{self._draft_url(draft_id)}"
            )
            return {"skipped": "no_file_url", "draft_id": draft_id}

        try:
            pdf_bytes = self._source_moco.download_file(file_url)
            logger.info("ocr: downloaded PDF draft_id=%s bytes=%d",
                        draft_id, len(pdf_bytes))

            invoice = self._ocr.extract(pdf_bytes)
            logger.info("ocr: extracted draft_id=%s confidence=%.2f "
                        "supplier=%r number=%r",
                        draft_id, invoice.confidence,
                        invoice.supplier_name, invoice.invoice_number)
            invoice = _prefer_draft_payment_fields(invoice, body)

            company_id = self._lookup_supplier_company(invoice.supplier_name)
            vat_code_id = self._resolve_vat_code_id(invoice, company_id)

            payload = _build_create_payload(
                invoice, pdf_bytes,
                vat_code_id=vat_code_id,
                company_id=company_id,
                draft_id=draft_id,
            )
            created = self._purchases.create_purchase(payload)
        except urlerror.HTTPError as e:
            # 4xx from any Moco call (most commonly POST /purchases 422 for
            # `receipt_identifier: ["ist bereits vergeben"]` on a duplicate)
            # is an unfixable-by-retry condition. Treat as a silent skip:
            # the OCR purchase isn't created, the operator gets a Telegram
            # alert with the Moco error body, and Moco's webhook delivery
            # log shows 200 ok=true so it doesn't keep retrying. 5xx still
            # propagates so the endpoint maps it to 502 (Moco retries).
            if not (400 <= e.code < 500):
                raise
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            logger.warning("ocr: Moco rejected request: %s %s",
                           e.code, err_body)
            self._notify_moco_4xx(draft_id, e.code, err_body)
            return {"skipped": "moco_rejected", "draft_id": draft_id,
                    "moco_status": e.code, "moco_error": err_body}

        new_purchase_id = created.get("id")
        logger.info("ocr: created purchase id=%s from draft=%s",
                    new_purchase_id, draft_id)

        if new_purchase_id:
            self._post_summary_comments(new_purchase_id, invoice,
                                        draft_id, body)
            self._delete_draft_after_create(draft_id, new_purchase_id)

        self._notify_outcome(new_purchase_id, draft_id, invoice)

        return {
            "draft_id": draft_id,
            "purchase_id": new_purchase_id,
            "confidence": invoice.confidence,
            "company_id": company_id,
            "is_credit_note": invoice.is_credit_note,
        }

    # --- vat code resolution ------------------------------------------------

    def _resolve_vat_code_id(self, invoice: InvoiceData,
                             supplier_company_id: int | None) -> int | None:
        """Decide which Moco vat_code_id to put on the new purchase's item.

        Priority order (per the product spec):
          1. The OCR'd `vat_rate`, matched against the values in
             `GET /vat_code_purchases`.
          2. The matched supplier's default vat code (requires fetching
             the full company via `get_company` — the company-list shape
             from `search_suppliers` doesn't carry the default).
          3. The vat_code from `GET /vat_code_purchases` marked as
             `default: true` (most Moco accounts have one designated
             default for purchases).
          4. Give up — return None. `POST /purchases` will 422 and the
             dispatcher fires a Telegram alert + ACKs 200 ok=false. Rare
             in practice, but better than guessing.

        A failure in any *individual* lookup (vat-codes list, get_company)
        is logged and treated as "no match in this branch" — we don't
        want a flapping /vat_code_purchases to nuke an otherwise-good run
        when the supplier could still supply a fallback.
        """
        try:
            vat_codes = self._purchases.list_vat_codes()
        except Exception:
            logger.exception("ocr: list_vat_codes failed, "
                             "vat_code_id resolution degraded")
            vat_codes = []

        if invoice.vat_rate is not None:
            match = _find_vat_code_by_rate(vat_codes, invoice.vat_rate)
            if match is not None:
                logger.info("ocr: matched vat_rate=%s to vat_code_id=%s",
                            invoice.vat_rate, match.get("id"))
                return match.get("id")
            logger.warning("ocr: vat_rate=%s did not match any active Moco "
                           "vat_code (tax values=%s); falling back to "
                           "supplier default", invoice.vat_rate,
                           [c.get("tax") for c in vat_codes
                            if c.get("active") is not False])

        if supplier_company_id is not None:
            try:
                company = self._source_moco.get_company(supplier_company_id)
            except Exception:
                logger.exception("ocr: get_company failed for vat fallback "
                                 "id=%s", supplier_company_id)
                company = None
            if company:
                supplier_default = _supplier_default_vat_code_id(company)
                if supplier_default is not None:
                    logger.info("ocr: using supplier default vat_code_id=%s "
                                "(company_id=%s)",
                                supplier_default, supplier_company_id)
                    return supplier_default
                logger.info("ocr: supplier id=%s has no default vat_code, "
                            "falling back to account default",
                            supplier_company_id)

        account_default = _account_default_vat_code(vat_codes)
        if account_default is not None:
            logger.info("ocr: using account-default vat_code_id=%s",
                        account_default.get("id"))
            return account_default.get("id")

        logger.warning("ocr: could not resolve vat_code_id from OCR, "
                       "supplier default, or account default — POST /purchases "
                       "will likely 422")
        return None

    # --- supplier lookup ----------------------------------------------------

    def _lookup_supplier_company(self, supplier_name: str | None) -> int | None:
        """Return a Moco company_id only when there's exactly one match.

        Ambiguity (multiple matches) or no match → leave the purchase
        company-less for the reviewer to assign. We prefer "no company"
        over "wrong company" — a misassigned supplier would invisibly
        skew downstream reporting.
        """
        if not supplier_name:
            return None
        try:
            matches = self._source_moco.search_suppliers(supplier_name)
        except Exception:
            # Don't fail the whole sync just because supplier lookup
            # blew up — the purchase is the authoritative side effect;
            # the human can link the company manually.
            logger.exception("ocr: supplier lookup failed name=%r",
                             supplier_name)
            return None
        if len(matches) != 1:
            if matches:
                logger.info("ocr: supplier_name=%r matched %d companies, "
                            "leaving company_id empty (ambiguous)",
                            supplier_name, len(matches))
            else:
                logger.info("ocr: supplier_name=%r had no Moco company match",
                            supplier_name)
            return None
        company = matches[0]
        return company.get("id")

    # --- comment back to Moco -----------------------------------------------

    def _post_summary_comments(self, purchase_id: int, invoice: InvoiceData,
                               draft_id: int, draft: dict) -> None:
        """Two separate best-effort comments on the newly created purchase.

        Posted as two distinct Moco comments so the reviewer sees them as
        independent timeline entries:
          1. 📧 Email-Quelle (sender + body) — only when Moco's email-
             import populated `email_from` / `email_body` on the draft.
          2. 🤖 OCR-Extraktion (fields, draft back-link, please-review).

        Each comment is independent: if the email-source post fails, the
        OCR-summary post still runs (and vice versa). The created
        purchase is the authoritative side effect — neither failure
        rolls it back.
        """
        email_text = _format_email_source_comment(
            email_from=draft.get("email_from"),
            email_body=draft.get("email_body"),
        )
        if email_text:
            try:
                self._purchases.post_comment(purchase_id, email_text)
            except Exception:
                logger.exception("ocr: email-source comment failed "
                                 "purchase_id=%s", purchase_id)

        ocr_text = _format_ocr_comment(invoice)
        try:
            self._purchases.post_comment(purchase_id, ocr_text)
        except Exception:
            logger.exception("ocr: OCR-summary comment failed "
                             "purchase_id=%s", purchase_id)

    # --- telegram routing ---------------------------------------------------

    def _notify_outcome(self, purchase_id: int | None, draft_id: int,
                        invoice: InvoiceData) -> None:
        if not self._telegram:
            return
        link = (self._purchase_url(purchase_id) if purchase_id
                else self._draft_url(draft_id))
        supplier = invoice.supplier_name or "Unbekannt"
        amount = (f"{invoice.currency or 'CHF'} {invoice.total_amount:.2f}"
                  if invoice.total_amount is not None else "Betrag ?")
        if invoice.is_credit_note:
            # Gutschrift always triggers the alert regardless of confidence:
            # the reviewer must flip the sign on the total before approving.
            self._telegram.notify(
                f"⚠️ Gutschrift erkannt ({invoice.confidence:.0%}) — "
                f"{supplier} {amount}\n"
                f"Moco-Purchase erstellt, Vorzeichen prüfen: {link}"
            )
            return
        if invoice.confidence >= CONFIDENCE_THRESHOLD:
            self._telegram.notify(
                f"✅ OCR erfolgreich ({invoice.confidence:.0%}) — "
                f"{supplier} {amount}\n"
                f"Moco-Purchase erstellt, bitte prüfen: {link}"
            )
        else:
            self._telegram.notify(
                f"⚠️ OCR unsicher ({invoice.confidence:.0%}) — "
                f"{supplier} {amount}\n"
                f"Moco-Purchase erstellt, bitte manuell prüfen: {link}"
            )

    def _notify(self, text: str) -> None:
        if self._telegram:
            self._telegram.notify(text)

    def _delete_draft_after_create(self, draft_id: int,
                                   new_purchase_id: int) -> None:
        """Remove the original draft once a real purchase has been created.

        Best-effort: the new purchase is the authoritative side effect, so
        a failed delete must NOT roll the sync back. A 404 is treated as
        "already gone" (idempotent — a webhook replay would hit this) and
        swallowed silently. Any other failure logs at warning and fires
        a Telegram alert so the operator can clean up by hand; the sync
        still reports success for the create.
        """
        try:
            self._purchases.delete_purchase_draft(draft_id)
        except urlerror.HTTPError as e:
            if e.code == 404:
                logger.info("ocr: draft %s already gone (delete idempotent)",
                            draft_id)
                return
            err_body = "<unreadable>"
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            logger.warning("ocr: failed to delete draft %s after creating "
                           "purchase %s: %s %s",
                           draft_id, new_purchase_id, e.code, err_body)
            self._notify_draft_delete_failed(draft_id, new_purchase_id,
                                             e.code, err_body)
        except Exception as e:
            logger.exception("ocr: unexpected error deleting draft %s",
                             draft_id)
            self._notify_draft_delete_failed(draft_id, new_purchase_id,
                                             None, str(e))

    def _notify_draft_delete_failed(self, draft_id: int,
                                    new_purchase_id: int,
                                    status_code: int | None,
                                    detail: str) -> None:
        if not self._telegram:
            return
        status = f"HTTP {status_code}" if status_code else "Exception"
        self._telegram.notify(
            "⚠️ OCR-Draft konnte nach erfolgreichem Create nicht gelöscht "
            f"werden — manuell entfernen:\n"
            f"Draft: {self._draft_url(draft_id)}\n"
            f"Neue Purchase: {self._purchase_url(new_purchase_id)}\n"
            f"Detail: {status} {detail}"
        )

    def _notify_moco_4xx(self, draft_id: int, status_code: int,
                         err_body: str) -> None:
        """Telegram alert for a Moco 4xx that the silent-skip swallowed.

        Without this the operator has no signal that the OCR purchase
        didn't get created (the webhook response is 200 ok=true, and
        Vercel logs are only checked when something feels off). The
        message includes the deep-link back to the draft so the operator
        can investigate from one click.
        """
        if not self._telegram:
            return
        self._telegram.notify(
            "❌ OCR-Purchase nicht erstellt — Moco hat die Anfrage "
            f"abgelehnt (HTTP {status_code})\n"
            f"Draft: {self._draft_url(draft_id)}\n"
            f"Detail: {err_body}"
        )

    def _purchase_url(self, purchase_id: int) -> str:
        return (f"https://{self._source_account_url}.mocoapp.com"
                f"/purchases/{purchase_id}")

    def _draft_url(self, draft_id: int) -> str:
        return (f"https://{self._source_account_url}.mocoapp.com"
                f"/purchases/drafts/{draft_id}")


# --- payload construction ---------------------------------------------------

def _build_create_payload(invoice: InvoiceData, pdf_bytes: bytes, *,
                          vat_code_id: int | None,
                          company_id: int | None,
                          draft_id: int) -> dict[str, Any]:
    """Construct the POST /purchases body.

    Moco requires: `date`, `currency`, `payment_method`, and `items` with
    `title` + `total` + `vat_code_id`. We always send a single line item
    because OCR returns the invoice total, not individual positions.
    `tax_included=True` because Swiss invoices print gross totals.

    `vat_code_id` is included only when the resolver could determine it
    (OCR rate match → supplier default chain). When both fail, the field
    is omitted; Moco rejects with 422 and the handler turns that into a
    Telegram alert + ok=false — better than guessing and silently
    misbooking VAT.

    The PDF is JSON-embedded as base64. Tags are the OCR markers so a
    reviewer can filter in Moco's UI.
    """
    title = (invoice.description
             or invoice.supplier_name
             or "OCR-importierte Rechnung")
    total = invoice.total_amount if invoice.total_amount is not None else 0.0
    if invoice.is_credit_note and total > 0:
        # Credit notes book as negative; the operator can re-flip during
        # review, but having the sign right by default is less error-prone.
        total = -total

    item: dict[str, Any] = {
        "title": title[:255],
        "total": total,
        "tax_included": True,
    }
    if vat_code_id is not None:
        item["vat_code_id"] = vat_code_id

    payment_method = _payment_method_for(invoice)
    # "Gutschrift" alongside the standard OCR markers when the model
    # identified a credit note — easy to filter in Moco's UI and a
    # second visual cue for the reviewer (on top of the negative total
    # and the comment warning).
    tags = list(OCR_TAGS)
    if invoice.is_credit_note:
        tags.append("Gutschrift")
    payload: dict[str, Any] = {
        "date": invoice.invoice_date or _today(),
        "currency": invoice.currency or "CHF",
        "payment_method": payment_method,
        "title": title[:255],
        "tags": tags,
        "items": [item],
        "file": {
            "filename": _attachment_filename(invoice, draft_id),
            "base64": base64.b64encode(pdf_bytes).decode("ascii"),
        },
    }

    if invoice.due_date:
        payload["due_date"] = invoice.due_date
    if invoice.invoice_number:
        payload["receipt_identifier"] = invoice.invoice_number
    if invoice.iban:
        payload["iban"] = invoice.iban
    # Reference is QR-bill-only: a 27-digit QR-reference under
    # bank_transfer (no QR-IBAN) would either be rejected by Moco or
    # interpreted as something else. Skip it on the non-QR-ESR branch.
    if invoice.qr_reference and payment_method == "bank_transfer_swiss_qr_esr":
        payload["reference"] = invoice.qr_reference
    elif invoice.qr_reference and not _is_qr_iban(invoice.iban):
        logger.warning("ocr: extracted qr_reference=%r but iban=%r is not a "
                       "QR-IBAN — falling back to bank_transfer and dropping "
                       "the reference", invoice.qr_reference, invoice.iban)
    if invoice.payment_purpose:
        payload["info"] = invoice.payment_purpose
    if company_id is not None:
        payload["company_id"] = company_id

    return payload


def _find_vat_code_by_rate(vat_codes: list[dict], rate: float) -> dict | None:
    """Find the active vat_code whose `tax` matches the OCR'd rate.

    Moco's `/vat_code_purchases` objects look like::
        {"id": 186, "tax": 7.7, "code": "9", "active": true, ...}

    `tax` is a percentage (8.1, 7.7, 2.6). OCR returns the rate as a
    decimal (0.081 for 8.1% — per the system prompt). We multiply the OCR
    rate by 100 for comparison, but also try the raw value to be tolerant
    of a prompt-drift run that accidentally returned the percentage
    directly. Epsilon of 0.05 absorbs OCR float-rounding (Sonnet sometimes
    returns 0.077 for the legal 7.7%, or rounds slightly).

    Inactive vat codes are filtered out — Moco keeps historical codes
    (old VAT rates from before the 2024 increase, special-purpose) around
    with `active: false`, and posting one would either 422 or book to a
    deprecated rate.
    """
    if rate is None:
        return None
    # OCR rate in decimal (0.081). Compare against Moco's `tax` percentage
    # (8.1). Cross-format candidate covers the rare case where OCR sent the
    # percentage directly.
    candidates = [rate * 100, rate]

    for code in vat_codes:
        if code.get("active") is False:
            continue
        raw = code.get("tax")
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        for target in candidates:
            if abs(value - target) < 0.05:
                return code
    return None


def _account_default_vat_code(vat_codes: list[dict]) -> dict | None:
    """Return the active vat_code marked as the account-wide default.

    Moco's `/vat_code_purchases` response may carry a flag indicating
    which code is the configured default; the field name isn't fully
    documented in the example shape we have, so we try `default`,
    `is_default`, and the legacy `default_for_purchase` to be robust.
    Falls back to None if no code is flagged — tier 4 (omit field, Moco
    422 → Telegram alert) handles that case.
    """
    for code in vat_codes:
        if code.get("active") is False:
            continue
        if (code.get("default") is True
                or code.get("is_default") is True
                or code.get("default_for_purchase") is True):
            return code
    return None


def _supplier_default_vat_code_id(company: dict) -> int | None:
    """Pull the supplier's default vat-code id from a Moco company.

    Moco's exact field name isn't fully documented for the supplier case;
    observed candidates are `default_vat_code_purchase_id` and the older
    `vat_code_purchase_id`. Try both, prefer the more specific one.
    """
    for key in ("default_vat_code_purchase_id", "vat_code_purchase_id"):
        value = company.get(key)
        if isinstance(value, int):
            return value
    return None


def _prefer_draft_payment_fields(invoice: InvoiceData, draft: dict) -> InvoiceData:
    """Override OCR's iban / qr_reference with the draft's values when present.

    Moco's email-import already runs a QR-bill parser on the incoming PDF
    and populates the `iban` and `reference` fields on the draft webhook
    body. That parser is dedicated to QR-bill structure and reliably
    reads the Zahlteil (payment slip), whereas vision-OCR can confuse a
    Zahlteil-IBAN with a different IBAN printed elsewhere on the
    document (letterhead, terms-and-conditions, contact block) and has
    been observed to mangle alphanumeric Swiss IBANs (`CH22 3000 00DE
    1611 6572 0` read as `CH3909000000161165720`).

    Both values are still passed through the same normalization +
    validation as OCR output (`_normalize_iban` enforces mod-97;
    `_normalize_qr_reference` enforces 27 digits) so the override doesn't
    bypass safety checks — a malformed draft IBAN doesn't get a free pass.

    If the draft carries no value, the OCR result is kept.
    """
    updates: dict[str, Any] = {}

    draft_iban = _normalize_iban(draft.get("iban"))
    if draft_iban:
        if invoice.iban and invoice.iban != draft_iban:
            logger.info("ocr: overriding OCR iban=%s with draft iban=%s "
                        "(Moco's QR-bill parser is authoritative for the "
                        "Zahlteil)", invoice.iban, draft_iban)
        updates["iban"] = draft_iban

    draft_reference = _normalize_qr_reference(draft.get("reference"))
    if draft_reference:
        if invoice.qr_reference and invoice.qr_reference != draft_reference:
            logger.info("ocr: overriding OCR qr_reference=%s with draft "
                        "reference=%s", invoice.qr_reference, draft_reference)
        updates["qr_reference"] = draft_reference

    return replace(invoice, **updates) if updates else invoice


def _is_qr_iban(iban: str | None) -> bool:
    """True if `iban` is a Swiss QR-IBAN.

    Per the Swiss QR-bill spec, a QR-IBAN is identified by its IID (bank
    clearing number) in positions 5–9: `30000`–`31999`. Regular IBANs
    have IIDs outside that range and Moco will 422 with
    `"iban":["ist keine QR-IBAN"]` if we POST one under
    `payment_method=bank_transfer_swiss_qr_esr`.
    """
    if not iban or len(iban) < 9 or not iban.startswith("CH"):
        return False
    iid = iban[4:9]
    if not iid.isdigit():
        return False
    return 30000 <= int(iid) <= 31999


def _payment_method_for(invoice: InvoiceData) -> str:
    """Pick the Moco payment_method enum from what OCR found.

    Swiss QR-ESR requires a QR-IBAN AND a QR-reference together — Moco
    enforces both. A QR-reference with a regular (non-QR) IBAN is a
    common OCR misread (the model recognises the numeric block but the
    creditor is on a normal account) and would 422; fall through to
    plain `bank_transfer` in that case rather than pushing a guaranteed
    failure. The caller drops the `reference` field too so it doesn't
    surface as a stray SCOR-shaped string on a plain transfer.
    """
    if invoice.qr_reference and _is_qr_iban(invoice.iban):
        return "bank_transfer_swiss_qr_esr"
    return "bank_transfer"


def _attachment_filename(invoice: InvoiceData, draft_id: int) -> str:
    """Build a readable filename for Moco's attachment list.

    Mirrors the shape `BexioExpenseSyncService._attachment_filename` uses
    for Bexio uploads, falling back to the draft id when supplier/number
    are missing so it's always non-empty.
    """
    parts = [invoice.invoice_date,
             invoice.supplier_name,
             invoice.invoice_number]
    base = " ".join(p for p in parts if p) or f"draft-{draft_id}"
    # Strip path separators defensively (model could echo them from the
    # invoice body) — Moco stores this verbatim.
    base = base.replace("/", "-").replace("\\", "-")
    return f"{base}.pdf"


def _today() -> str:
    """ISO date string for "today" — used as the fallback purchase date
    when OCR couldn't extract an invoice date. Imported lazily so unit
    tests can monkeypatch `datetime` without touching module import order."""
    import datetime as dt
    return dt.date.today().isoformat()


# --- comment text -----------------------------------------------------------

# Cap email_body length in the rendered comment so a hugely-quoted email
# thread doesn't bloat the Moco comment (no documented hard limit, but
# multi-megabyte bodies aren't useful for a reviewer scrolling past).
EMAIL_BODY_MAX_CHARS = 2000


def _format_ocr_comment(invoice: InvoiceData) -> str:
    """HTML comment body for the 🤖 OCR-extraction summary.

    Posted as its own Moco comment (separate from the 📧 email-source
    comment) so the reviewer's timeline shows the two pieces of context
    independently. Moco accepts only this subset of HTML on comment
    bodies: `div, strong, em, u, pre, ul, ol, li, br` (everything else
    is stripped). Plain-text newlines are NOT preserved, so structure
    via `<br>` and `<ul>`.

    No back-link to the original draft is included — the service
    auto-deletes the draft after the create succeeds (see
    `_delete_draft_after_create`).
    """
    parts: list[str] = []
    confidence_pct = f"{invoice.confidence:.0%}"
    parts.append(
        f"<strong>🤖 OCR-Extraktion</strong> (Konfidenz: {confidence_pct})"
    )
    if invoice.is_credit_note:
        parts.append(
            "<strong>⚠️ Als Gutschrift erkannt — Vorzeichen prüfen!</strong>"
        )

    fields: list[str] = []
    fields.append(_li("Lieferant", invoice.supplier_name))
    fields.append(_li(
        "Betrag",
        (f"{invoice.currency or 'CHF'} {invoice.total_amount:.2f}"
         if invoice.total_amount is not None else None),
    ))
    fields.append(_li("Datum", invoice.invoice_date))
    fields.append(_li("Fällig", invoice.due_date))
    fields.append(_li("Rechnungs-Nr", invoice.invoice_number))
    fields.append(_li("IBAN", invoice.iban))
    fields.append(_li("QR-Ref", invoice.qr_reference))
    fields.append(_li("Kommission", invoice.commission))
    fields = [li for li in fields if li]
    if fields:
        parts.append("<ul>" + "".join(fields) + "</ul>")

    # Original draft is auto-deleted by `_delete_draft_after_create` once
    # we know the new purchase landed, so no back-link is needed in the
    # comment. (If the delete fails, the operator gets a separate
    # Telegram alert with both URLs.)
    parts.append(
        "<strong>⚠️ Bitte Felder prüfen und freigeben.</strong>"
    )
    return "<div>" + "<br>".join(parts) + "</div>"


def _format_email_source_comment(email_from: str | None,
                                 email_body: str | None) -> str:
    """HTML comment body for the 📧 source-email block, or empty string.

    Posted as its own Moco comment (separate from the 🤖 OCR comment)
    when Moco's email-import populated `email_from` / `email_body` on
    the webhook body. Manually-uploaded drafts have neither — in that
    case this returns "" and the caller skips posting.

    Body rendering branches on shape:
      - HTML body (forwarded email from a webmail client, contains
        <div>/<br>/<strong>/etc) → sanitize to Moco's allowed tag subset
        and pass through inline. Otherwise Moco renders raw escaped
        markup as literal `<div>` text — ugly.
      - Plain text body → wrap in <pre> so newlines / indentation
        survive Moco's HTML normalizer.
    """
    if not email_from and not email_body:
        return ""
    parts: list[str] = ["<strong>📧 Email-Quelle</strong>"]
    if email_from:
        parts.append(f"<strong>Von:</strong> {escape(email_from)}")
    if email_body:
        body = email_body
        truncated = ""
        if len(body) > EMAIL_BODY_MAX_CHARS:
            body = body[:EMAIL_BODY_MAX_CHARS]
            truncated = (f"\n[…gekürzt von {len(email_body)} auf "
                         f"{EMAIL_BODY_MAX_CHARS} Zeichen]")
        if _looks_like_html(body):
            rendered = _sanitize_html_for_moco(body)
            if truncated:
                rendered += f"<br>{escape(truncated.strip())}"
            parts.append(rendered)
        else:
            parts.append(f"<pre>{escape(body)}{escape(truncated)}</pre>")
    return "<div>" + "<br>".join(parts) + "</div>"


# Tags Moco's comment renderer keeps; anything else is stripped on submit.
_MOCO_ALLOWED_TAGS = {"div", "strong", "em", "u", "pre", "ul", "ol", "li", "br"}

# Common forwarded-email tags rewritten to Moco-allowed equivalents instead
# of being dropped, so the visual structure (paragraph breaks, bold) is
# preserved.
_TAG_REWRITES = {
    "b": "strong",
    "i": "em",
    "p": "div",
    "h1": "div", "h2": "div", "h3": "div",
    "h4": "div", "h5": "div", "h6": "div",
}


def _looks_like_html(text: str) -> bool:
    """True if `text` carries HTML markup we should preserve.

    Conservative pattern — only triggers on actual tag names so plain
    text containing `<verkauf@example.com>` or `< 5%` doesn't accidentally
    fall into the HTML branch. Real forwarded emails always carry at
    least one of these structural tags.
    """
    return bool(re.search(
        r"</?(?:div|p|br|strong|b|em|i|span|a|table|tr|td|"
        r"html|body|head|h[1-6]|ul|ol|li)\b",
        text, re.IGNORECASE,
    ))


def _sanitize_html_for_moco(html: str) -> str:
    """Strip / rewrite tags so only Moco's allowed subset survives.

    Moco's renderer silently strips disallowed tags on submit. We do
    the same client-side and additionally rewrite common HTML-email
    tags (`<b>`, `<i>`, `<p>`, `<h1..h6>`) into Moco-allowed substitutes
    so the structure (bold, paragraphs) is preserved rather than
    flattened to a wall of text. Attributes are dropped — Moco doesn't
    accept them on the allowed tags either, and they're a vector for
    style noise from random webmail clients.
    """
    def replace(m: re.Match) -> str:
        slash = m.group(1) or ""
        name = m.group(2).lower()
        if name in _TAG_REWRITES:
            name = _TAG_REWRITES[name]
        if name in _MOCO_ALLOWED_TAGS:
            return f"<{slash}{name}>"
        return ""   # strip the tag itself, leave inner text in place
    return re.sub(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)(?:\s[^>]*)?>", replace, html)


def _li(label: str, value) -> str:
    """Render one `<li><strong>Label:</strong> value</li>`.

    Returns empty string when the value is None / blank so the caller can
    filter and `<li>` placeholders don't leak into the rendered list.
    Label is static (no escape needed); value is HTML-escaped defensively.
    """
    if value is None or value == "":
        return ""
    return f"<li><strong>{label}:</strong> {escape(str(value))}</li>"
