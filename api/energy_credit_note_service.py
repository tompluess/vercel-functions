"""EnergyCreditNoteService — turn an EVU production credit note into a
Moco project expense + Moco invoice.

Local energy suppliers (EVUs — e.g. CKW) send quarterly statements that
combine a small consumption invoice with a much larger production credit
("Gutschrift" / "Rücklieferung") — PVcontracting's own outgoing revenue for
electricity fed back into the grid. These arrive in the same
`Purchase::Draft` inbox as regular supplier invoices; running them through
the generic OCR→purchase path would book them backwards (as a cost, not
revenue). `SupplierInvoiceOcrService.process` detects them (see
`is_energy_credit_note`, using the ALREADY-run general OCR pass + supplier
lookup — there is no cheap pre-download signal here, unlike smart-me) and
delegates here instead. This service:

  1. Runs a second, targeted OCR pass (`extract_energy_credit_note`) to
     pull the Objekt (production section, PRIMARY) / Objekt (top-level
     summary, FALLBACK) / top-level gross Gutschriftsbetrag (inkl. MWST) /
     VAT rate / Abrechnungszeitraum. The bookable ex-VAT amount is then
     DERIVED, not OCR'd: `net_amount = gross_amount / (1 + vat_rate)` —
     the top-level gross figure already nets the consumption section
     against the production section, so it (not either subsection's own
     Nettobetrag) is the correct basis (see
     `specs/SPEC_energy_credit_note.md`, decision D6).
  2. Matches the Objekt + supplier to exactly one `Stromproduktion`-tagged
     Moco project (`StromproduktionProjectMatcher`, via `_match_project`) —
     tries the production-section Objekt first, falling back once to the
     top-level summary Objekt when the primary attempt is `no_match`/
     `empty` (some vZEV community statements print a generic, non-site-
     specific Objekt on the production section; see decision D7).
  3. Creates a project expense via `POST /projects/{id}/expenses` with the
     field conventions the operator uses manually: quantity 1, unit "x",
     unit_price = derived net amount, unit_cost 0, billable, NOT
     budget_relevant, service_period = Abrechnungszeitraum, PDF attached.
  4. Creates a Moco invoice via `POST /invoices` linking that expense
     through `items[].expense_ids` (Moco marks the expense billed
     automatically), attaches the same PDF, and leaves the invoice at
     `status: "created"` — it is deliberately NOT transitioned to "sent".
     Sending is a manual step the operator performs later in the Moco UI;
     the existing `bexio-invoice-sync` cascade fires then, automatically.
  5. On success: best-effort deletes the draft (404 = already gone) and
     sends a Telegram summary with a link to the new invoice, noting it
     still needs manual review + sending.

Anything that prevents a confident booking — no project match, ambiguous
match, missing Brutto-Betrag or VAT rate (either one blocks the net-amount
derivation), missing/unparseable Abrechnungszeitraum — takes the *keep*
path: Telegram alert + best-effort comment on the draft
itself (`commentable_type="PurchaseDraft"`), the draft stays in the inbox,
and the webhook ACKs ok=true (a retry can't fix it). Expense/invoice/
attachment creation errors propagate so `index.py`'s existing mapping
applies (4xx → 200 ok=false + Telegram, 5xx/URLError → 502 retry) — same
posture as `SmartmeEnergyExpenseService`, no internal HTTPError swallow.
"""

import base64
import datetime as dt
import logging
from html import escape
from typing import Any
from urllib import error as urlerror

from api.anthropic_ocr_client import (
    AnthropicOcrClient,
    EnergyCreditNoteData,
    InvoiceData,
)
from api.moco_client import MocoClient
from api.moco_invoice_client import MocoInvoiceClient
from api.moco_purchase_client import MocoPurchaseClient
from api.moco_supplier_matcher import MocoSupplierMatcher
from api.stromproduktion_project_matcher import (
    StromproduktionProjectMatch,
    StromproduktionProjectMatcher,
)
from api.telegram_notifier import TelegramNotifier

logger = logging.getLogger("energy_credit_note_service")

EVU_TAG = "Lokaler Energieversorger (EVU)"
# Draft purchases are their own polymorphic comment target — "Purchase"
# with a draft id 404s (drafts live in a separate URL space, see
# MocoPurchaseClient.get_purchase_draft).
COMMENTABLE_TYPE_DRAFT = "PurchaseDraft"
CONFIDENCE_THRESHOLD = 0.85
DEFAULT_VAT_RATE = 8.1  # Swiss standard rate — the account's historical default
DUE_DATE_DAYS = 30


def is_energy_credit_note(invoice: InvoiceData, company: dict | None) -> bool:
    """True when a draft is an EVU production credit note.

    Reuses the general OCR pass's own credit-note detection
    (`invoice.is_credit_note`, already asked for in `SYSTEM_PROMPT`) plus
    the already-matched supplier company's tags — there is no reliable
    pre-OCR signal for this document class (unlike `is_smartme_draft`),
    so detection necessarily happens after the general OCR + supplier
    lookup `SupplierInvoiceOcrService.process` already runs for every
    draft.

    This is `company` — the SUPPLIER-type company match — one of THREE
    independent detection signals; callers should also check
    `EnergyCreditNoteService.has_matching_project` and
    `EnergyCreditNoteService.is_evu_tagged_customer`, treating any one as
    sufficient. The EVU tag on the supplier-type record alone is not
    reliable: confirmed live that Moco can hold the tag on one of an
    entity's two company records but not the other (see
    `has_candidate_for_supplier`'s docstring in
    `stromproduktion_project_matcher.py`), so a real EVU credit note can
    arrive with `company.tags == []` — or with no supplier-type company
    record at all (confirmed live for BKW, see `is_evu_tagged_customer`).
    """
    if not invoice.is_credit_note or company is None:
        return False
    tags = {str(t).casefold() for t in (company.get("tags") or [])}
    return EVU_TAG.casefold() in tags


class EnergyCreditNoteService:
    def __init__(self, *, moco: MocoClient, moco_invoices: MocoInvoiceClient,
                 purchase_client: MocoPurchaseClient,
                 ocr: AnthropicOcrClient,
                 matcher: StromproduktionProjectMatcher,
                 customer_matcher: MocoSupplierMatcher,
                 subdomain: str,
                 telegram: TelegramNotifier | None = None):
        self._moco = moco
        self._moco_invoices = moco_invoices
        self._purchases = purchase_client
        self._ocr = ocr
        self._matcher = matcher
        self._customer_matcher = customer_matcher
        self._subdomain = subdomain
        self._telegram = telegram

    def has_matching_project(self, supplier_name: str | None) -> bool:
        """Second detection signal — see `is_energy_credit_note`'s
        docstring. True when a `Stromproduktion` project's customer
        plausibly matches `supplier_name`, regardless of whether the
        supplier's own Moco company record carries the EVU tag."""
        return self._matcher.has_candidate_for_supplier(supplier_name)

    def is_evu_tagged_customer(self, supplier_name: str | None) -> bool:
        """Third detection signal — see `is_energy_credit_note`'s
        docstring. True when a CUSTOMER-type company matching
        `supplier_name` carries the EVU tag, independent of whatever the
        SUPPLIER-type lookup (`is_energy_credit_note`'s `company` param)
        found — the two are different Moco company records for the same
        real-world entity (confirmed live: CKW and BKW both tag their
        `type: "customer"` record, since that's the relationship an
        energy-credit-note represents — PVcontracting selling production
        back to the EVU). Reuses `MocoSupplierMatcher`'s own name-matching
        tiers against the customer-type company list — same conservative
        "unique hit only" semantics as the supplier-type lookup."""
        match = self._customer_matcher.match(supplier_name)
        if match.status != "matched":
            return False
        tags = {str(t).casefold() for t in (match.company.get("tags") or [])}
        return EVU_TAG.casefold() in tags

    def process(self, *, pdf_bytes: bytes, invoice: InvoiceData,
                company: dict, draft_id: int, body: dict) -> dict[str, Any]:
        """Drive one detected energy-credit-note draft through OCR +
        expense + invoice creation.

        The caller (`SupplierInvoiceOcrService.process`) has already
        downloaded the PDF, run the general OCR pass, and matched the
        supplier company — all three are passed in so this method doesn't
        redo that work.
        """
        credit = self._ocr.extract_energy_credit_note(pdf_bytes)
        logger.info("energy_credit_note: extracted draft_id=%s confidence=%.2f "
                    "objekt=%r objekt_top_level=%r gross=%r vat_rate=%r "
                    "period=%r..%r",
                    draft_id, credit.confidence, credit.objekt,
                    credit.objekt_top_level, credit.gross_amount,
                    credit.vat_rate, credit.period_from, credit.period_to)

        match, objekt_used = self._match_project(invoice.supplier_name,
                                                  credit, draft_id=draft_id)
        if match.status != "matched":
            logger.warning("energy_credit_note: draft %s objekt=%r not "
                           "matched (status=%s candidates=%d)",
                           draft_id, objekt_used, match.status,
                           match.candidate_count)
            return self._keep_draft(
                draft_id, credit=credit,
                reason=_unmatched_reason(match),
                skipped=f"energy_credit_note_project_{match.status}",
                extra={"match_status": match.status, "objekt": objekt_used})
        net_amount = _derive_net_amount(credit.gross_amount, credit.vat_rate)
        if net_amount is None:
            if credit.gross_amount is None:
                reason, skipped = ("OCR fand keinen Brutto-Betrag",
                                   "energy_credit_note_no_gross_amount")
            else:
                reason, skipped = ("OCR fand keinen Mehrwertsteuersatz "
                                   "(zur Netto-Berechnung nötig)",
                                   "energy_credit_note_no_vat_rate")
            logger.warning("energy_credit_note: draft %s cannot derive net "
                           "amount (gross=%r vat_rate=%r)", draft_id,
                           credit.gross_amount, credit.vat_rate)
            return self._keep_draft(
                draft_id, credit=credit, reason=reason, skipped=skipped,
                extra={"objekt": objekt_used})
        leistungszeitraum = _format_leistungszeitraum(credit.period_from)
        if not credit.period_from or not credit.period_to or leistungszeitraum is None:
            logger.warning("energy_credit_note: draft %s has no usable "
                           "Abrechnungszeitraum", draft_id)
            return self._keep_draft(
                draft_id, credit=credit,
                reason="OCR fand keinen (lesbaren) Abrechnungszeitraum",
                skipped="energy_credit_note_no_period",
                extra={"objekt": objekt_used})

        project = match.project
        project_id = project.get("id")

        expense_payload = _build_expense_payload(
            credit, pdf_bytes, net_amount=net_amount,
            leistungszeitraum=leistungszeitraum, draft_id=draft_id)
        # HTTPError propagates — index.py maps 4xx → app error (Telegram +
        # 200 ok=false) and 5xx → 502 retry. Same posture as the smart-me
        # flow: no known routine 4xx here, so no service-internal swallow.
        expense = self._moco.create_project_expense(project_id, expense_payload)
        expense_id = expense.get("id")
        logger.info("energy_credit_note: created expense id=%s project=%s "
                    "(%r) from draft=%s", expense_id, project_id,
                    project.get("name"), draft_id)

        vat_code_id = self._resolve_vat_code_id(credit.vat_rate)
        invoice_payload = _build_invoice_payload(
            project, credit, expense_id=expense_id, net_amount=net_amount,
            leistungszeitraum=leistungszeitraum, vat_code_id=vat_code_id)
        created_invoice = self._moco_invoices.create_invoice(invoice_payload)
        invoice_id = created_invoice.get("id")
        logger.info("energy_credit_note: created invoice id=%s (status=%s) "
                    "expense=%s project=%s", invoice_id,
                    created_invoice.get("status"), expense_id, project_id)

        self._moco_invoices.add_attachment(
            invoice_id,
            filename=_attachment_filename(credit, draft_id),
            base64_content=base64.b64encode(pdf_bytes).decode("ascii"))

        self._delete_draft_after_create(draft_id, invoice_id)
        self._notify_success(project, credit, invoice_id, objekt_used)

        return {
            "energy_credit_note": True,
            "draft_id": draft_id,
            "expense_id": expense_id,
            "invoice_id": invoice_id,
            "project_id": project_id,
            "project_name": project.get("name"),
            "gross_amount": credit.gross_amount,
            "net_amount": net_amount,
            "leistungszeitraum": leistungszeitraum,
            "confidence": credit.confidence,
            "objekt_matched": objekt_used,
        }

    # --- project matching ------------------------------------------------

    def _match_project(self, supplier_name: str | None,
                       credit: EnergyCreditNoteData, *,
                       draft_id: int
                       ) -> tuple[StromproduktionProjectMatch, str | None]:
        """Match the credit note to a `Stromproduktion` project.

        Tries the production-section Objekt (`credit.objekt`) first — the
        primary, better-evidenced signal. Only when that comes back
        `no_match` or `empty` does it retry once with the top-level summary
        Objekt (`credit.objekt_top_level`). An `ambiguous` primary result is
        NOT retried — it already gets its own keep-draft + Telegram alert
        telling the operator which Kommission field to pin, and retrying
        with a different Objekt on an already-ambiguous case would just add
        an unpredictable second axis to reason about (see
        `specs/SPEC_energy_credit_note.md`, decision D7).

        Returns the winning match plus the Objekt string that produced it
        (for logging/diagnostics and for the `_keep_draft`/`_notify_success`
        display).
        """
        match = self._matcher.match(supplier_name=supplier_name,
                                    objekt=credit.objekt)
        if match.status in ("no_match", "empty") and credit.objekt_top_level:
            fallback = self._matcher.match(supplier_name=supplier_name,
                                           objekt=credit.objekt_top_level)
            logger.info("energy_credit_note: draft %s primary objekt %r "
                       "(%s) -> retrying with top-level objekt %r (%s)",
                       draft_id, credit.objekt, match.status,
                       credit.objekt_top_level, fallback.status)
            return fallback, credit.objekt_top_level
        return match, credit.objekt

    # --- vat code resolution -------------------------------------------

    def _resolve_vat_code_id(self, ocr_rate: float | None) -> int | None:
        """Pick the sales `vat_code_id` for the invoice item.

        Priority: OCR'd `vat_rate` matched against `GET /vat_code_sales`,
        else the active code at the account-standard 8.1% (matches every
        historical Stromproduktion invoice), else the first active code
        (last resort — avoids a guaranteed 422 on an empty vat_code_id
        when we could still supply *something* valid).
        """
        try:
            vat_codes = self._moco_invoices.list_vat_code_sales()
        except Exception:
            logger.exception("energy_credit_note: list_vat_code_sales failed, "
                             "vat_code_id resolution degraded")
            vat_codes = []
        return _pick_vat_code_id(vat_codes, ocr_rate)

    # --- keep-draft path -------------------------------------------------

    def _keep_draft(self, draft_id: int, *,
                    credit: EnergyCreditNoteData | None,
                    reason: str, skipped: str,
                    extra: dict | None = None) -> dict[str, Any]:
        """Alert + comment on the draft, leave it in the inbox, ACK ok.

        The draft stays as the operator's work item — deleting it would
        destroy the only trace of an unbooked credit note.
        """
        self._notify(
            "⚠️ Energie-Gutschrift nicht verbucht — " + reason + "\n"
            + _credit_summary_lines(credit)
            + f"Draft (bleibt bestehen): {self._draft_url(draft_id)}"
        )
        self._post_draft_comment(draft_id, _format_keep_comment(credit, reason))
        result: dict[str, Any] = {"energy_credit_note": True,
                                  "skipped": skipped, "draft_id": draft_id}
        if extra:
            result.update(extra)
        return result

    def _post_draft_comment(self, draft_id: int, text: str) -> None:
        try:
            self._moco.post_comment(
                commentable_id=draft_id,
                commentable_type=COMMENTABLE_TYPE_DRAFT,
                text=text)
        except urlerror.HTTPError as e:
            err_body = "<unreadable>"
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            logger.warning("energy_credit_note: draft comment failed "
                           "draft_id=%s: %s %s", draft_id, e.code, err_body)
        except Exception as e:
            logger.warning("energy_credit_note: draft comment failed "
                           "draft_id=%s: %s", draft_id, e)

    # --- post-create steps ------------------------------------------------

    def _delete_draft_after_create(self, draft_id: int,
                                   invoice_id: int | None) -> None:
        """Remove the draft once the expense + invoice landed.

        Best-effort: the expense + invoice are the authoritative side
        effects, so a failed delete must NOT roll the sync back. 404 =
        already gone (webhook replay), swallowed silently; any other
        failure logs a warning + Telegram so the operator can clean up by
        hand.
        """
        try:
            self._purchases.delete_purchase_draft(draft_id)
        except urlerror.HTTPError as e:
            if e.code == 404:
                logger.info("energy_credit_note: draft %s already gone "
                            "(delete idempotent)", draft_id)
                return
            err_body = "<unreadable>"
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            logger.warning("energy_credit_note: failed to delete draft %s "
                           "after creating invoice %s: %s %s",
                           draft_id, invoice_id, e.code, err_body)
            self._notify_draft_delete_failed(draft_id, invoice_id,
                                             e.code, err_body)
        except Exception as e:
            logger.exception("energy_credit_note: unexpected error deleting "
                             "draft %s", draft_id)
            self._notify_draft_delete_failed(draft_id, invoice_id,
                                             None, str(e))

    def _notify_draft_delete_failed(self, draft_id: int,
                                    invoice_id: int | None,
                                    status_code: int | None,
                                    detail: str) -> None:
        status = f"HTTP {status_code}" if status_code else "Exception"
        self._notify(
            "⚠️ Energie-Gutschrift-Draft konnte nach erfolgreichem Erstellen "
            "nicht gelöscht werden — manuell entfernen:\n"
            f"Draft: {self._draft_url(draft_id)}\n"
            f"Rechnung: {self._invoice_url(invoice_id)}\n"
            f"Detail: {status} {detail}"
        )

    def _notify_success(self, project: dict, credit: EnergyCreditNoteData,
                        invoice_id: int, objekt_used: str | None) -> None:
        confidence_note = (""
                           if credit.confidence >= CONFIDENCE_THRESHOLD
                           else " — bitte prüfen")
        icon = "✅" if credit.confidence >= CONFIDENCE_THRESHOLD else "⚠️"
        # Flag it when the top-level-summary Objekt won the match rather
        # than the (primary) production-section one — worth a glance since
        # it means the production section's own Objekt was unusable for
        # this statement (see `_match_project`).
        fallback_note = ("" if objekt_used == credit.objekt else
                         f" (Objekt via Top-Level-Fallback: {objekt_used!r})")
        invoice_line = ("Rechnung (Status: erstellt — bitte prüfen und "
                        f"manuell auf \"versendet\" setzen): "
                        f"{self._invoice_url(invoice_id)}")
        self._notify(
            f"{icon} Energie-Gutschrift verbucht ({credit.confidence:.0%})"
            f"{confidence_note} — {project.get('name')}{fallback_note}\n"
            + _credit_summary_lines(credit)
            + invoice_line
        )

    def _notify(self, text: str) -> None:
        if self._telegram:
            self._telegram.notify(text)

    def _draft_url(self, draft_id: int) -> str:
        return (f"https://{self._subdomain}.mocoapp.com"
                f"/purchases/drafts/{draft_id}")

    def _invoice_url(self, invoice_id: int | None) -> str:
        return (f"https://{self._subdomain}.mocoapp.com"
                f"/invoices/{invoice_id}")


# --- pure helpers -------------------------------------------------------


def _derive_net_amount(gross_amount: float | None,
                       vat_rate: float | None) -> float | None:
    """Ex-VAT bookable amount: `gross_amount / (1 + vat_rate)`, rounded to
    2dp. `gross_amount` is the document's top-level Gutschriftsbetrag
    (inkl. MWST) — already netting the consumption section against the
    production section (see `specs/SPEC_energy_credit_note.md`, D6).

    None when either operand is missing — callers must treat that as a
    keep-draft failure, never divide by a guessed rate.
    """
    if gross_amount is None or vat_rate is None:
        return None
    return round(gross_amount / (1 + vat_rate), 2)


def _build_expense_payload(credit: EnergyCreditNoteData, pdf_bytes: bytes, *,
                           net_amount: float,
                           leistungszeitraum: str,
                           draft_id: int) -> dict[str, Any]:
    """Construct the POST /projects/{id}/expenses body.

    Field conventions copied from the operator's manual entries on the
    Stromproduktion projects: one "x" unit at the derived net amount, zero
    cost (the fed-back power itself has no purchase cost), billable but NOT
    budget_relevant (matches 5 of 6 historical entries — see
    `specs/SPEC_energy_credit_note.md`), the Abrechnungszeitraum as the
    service period, and the source PDF attached.
    """
    return {
        "date": credit.invoice_date or _today(),
        "title": f"Stromproduktion {leistungszeitraum}",
        "quantity": 1,
        "unit": "x",
        "unit_price": net_amount,
        "unit_cost": 0,
        "billable": True,
        "budget_relevant": False,
        "service_period_from": credit.period_from,
        "service_period_to": credit.period_to,
        "file": {
            "filename": _attachment_filename(credit, draft_id),
            "base64": base64.b64encode(pdf_bytes).decode("ascii"),
        },
    }


def _build_invoice_payload(project: dict, credit: EnergyCreditNoteData, *,
                           expense_id: int, net_amount: float,
                           leistungszeitraum: str,
                           vat_code_id: int | None) -> dict[str, Any]:
    """Construct the POST /invoices body.

    `status="created"` is explicit (not omitted) so the invoice is a real,
    non-draft invoice from the start but is never transitioned to "sent" —
    see the module docstring / SPEC decision D2. `items[].expense_ids`
    links + auto-bills the created project expense.
    """
    customer = project.get("customer") or {}
    item_title = f"Stromproduktion {leistungszeitraum}"
    suffix = _format_period_suffix(credit.period_from, credit.period_to)
    if suffix:
        item_title = f"{item_title} {suffix}"
    # Rechnungsdatum: the source EVU document's own invoice date, not
    # today — the operator wants the Moco invoice dated to match the
    # customer's original Beleg. Falls back to today only when OCR found
    # no date (same posture as `_build_expense_payload`'s `date` field).
    date = credit.invoice_date or _today()
    payload: dict[str, Any] = {
        "status": "created",
        "customer_id": customer.get("id"),
        "project_id": project.get("id"),
        "recipient_address": project.get("billing_address") or "",
        "date": date,
        "due_date": _add_days(date, DUE_DATE_DAYS),
        "title": f"Stromproduktion {leistungszeitraum} – {project.get('name')}",
        "currency": "CHF",
        "tags": ["Stromproduktion"],
        "items": [{
            "type": "item",
            "title": item_title,
            "quantity": 1,
            "unit": "x",
            "unit_price": net_amount,
            "expense_ids": [expense_id],
        }],
    }
    if vat_code_id is not None:
        payload["vat_code_id"] = vat_code_id
    return payload


def _pick_vat_code_id(vat_codes: list[dict],
                      ocr_rate: float | None) -> int | None:
    """Resolve a sales `vat_code_id` from `GET /vat_code_sales`.

    Priority: OCR'd rate match (tolerant of percent-vs-decimal, epsilon
    0.05 for rounding) → the active code at the account-standard 8.1% →
    the first active code. Returns None only when the list itself is
    empty (fetch failed) — Moco will 422 and the caller's existing
    mapping handles it.
    """
    active = [c for c in vat_codes if c.get("active") is not False]
    if ocr_rate is not None:
        candidates = [ocr_rate * 100, ocr_rate]
        for code in active:
            raw = code.get("tax")
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            for target in candidates:
                if abs(value - target) < 0.05:
                    return code.get("id")
    for code in active:
        raw = code.get("tax")
        try:
            if raw is not None and abs(float(raw) - DEFAULT_VAT_RATE) < 0.05:
                return code.get("id")
        except (TypeError, ValueError):
            continue
    if active:
        return active[0].get("id")
    return None


def _format_leistungszeitraum(period_from: str | None) -> str | None:
    """`"2026-04-01"` → `"2026/Q2"`. None when unparseable/missing."""
    if not period_from:
        return None
    try:
        d = dt.date.fromisoformat(period_from)
    except (TypeError, ValueError):
        return None
    quarter = (d.month - 1) // 3 + 1
    return f"{d.year}/Q{quarter}"


def _format_period_suffix(period_from: str | None,
                          period_to: str | None) -> str | None:
    """`("2026-04-01", "2026-06-30")` → `"(04 – 06/2026)"`."""
    if not period_from or not period_to:
        return None
    try:
        d_from = dt.date.fromisoformat(period_from)
        d_to = dt.date.fromisoformat(period_to)
    except (TypeError, ValueError):
        return None
    return f"({d_from.month:02d} – {d_to.month:02d}/{d_to.year})"


def _add_days(date_iso: str, days: int) -> str:
    d = dt.date.fromisoformat(date_iso)
    return (d + dt.timedelta(days=days)).isoformat()


def _attachment_filename(credit: EnergyCreditNoteData, draft_id: int) -> str:
    parts = [credit.invoice_date, "Stromproduktion-Gutschrift",
             credit.invoice_number]
    base = " ".join(p for p in parts if p) or f"draft-{draft_id}"
    base = base.replace("/", "-").replace("\\", "-")
    return f"{base}.pdf"


def _credit_summary_lines(credit: EnergyCreditNoteData | None) -> str:
    """The Objekt/Betrag/Zeitraum block shared by all Telegram messages."""
    if credit is None:
        return ""
    lines: list[str] = []
    if credit.objekt:
        lines.append(f"Objekt: {credit.objekt}")
    if credit.gross_amount is not None:
        lines.append(f"Betrag: CHF {credit.gross_amount:.2f} (brutto, inkl. MWST)")
    net_amount = _derive_net_amount(credit.gross_amount, credit.vat_rate)
    if net_amount is not None:
        lines.append(f"Betrag netto (berechnet): CHF {net_amount:.2f}")
    if credit.period_from and credit.period_to:
        lines.append(f"Zeitraum: {credit.period_from} – {credit.period_to}")
    return "\n".join(lines) + "\n" if lines else ""


def _unmatched_reason(match: StromproduktionProjectMatch) -> str:
    if match.status == "empty":
        return "OCR fand kein Objekt"
    if match.status == "ambiguous":
        names = ", ".join(repr(p.get("name")) for p in match.candidates[:4])
        return (f"Objekt passt auf {match.candidate_count} Projekte "
                f"({names}) (mehrdeutig — Objekt ins Kommission-Feld des "
                "Zielprojekts eintragen)")
    return ("kein Stromproduktion-Projekt des Lieferanten passt zum Objekt "
            "(Lieferant/Projekt-Zuordnung prüfen oder Objekt ins "
            "Kommission-Feld eintragen)")


def _format_keep_comment(credit: EnergyCreditNoteData | None,
                         reason: str) -> str:
    """HTML comment left on the kept draft (Moco-allowed tag subset only)."""
    parts = ["<strong>🤖 Energie-Gutschrift — nicht automatisch "
             f"verbucht:</strong> {escape(reason)}"]
    fields: list[str] = []
    if credit is not None:
        fields.append(_li("Objekt", credit.objekt))
        fields.append(_li("Brutto-Betrag",
                          f"CHF {credit.gross_amount:.2f}"
                          if credit.gross_amount is not None else None))
        net_amount = _derive_net_amount(credit.gross_amount, credit.vat_rate)
        fields.append(_li("Netto-Betrag (berechnet)",
                          f"CHF {net_amount:.2f}"
                          if net_amount is not None else None))
        zeitraum = (f"{credit.period_from} – {credit.period_to}"
                    if credit.period_from and credit.period_to else None)
        fields.append(_li("Abrechnungszeitraum", zeitraum))
        fields.append(_li("Rechnungs-Nr", credit.invoice_number))
        fields = [li for li in fields if li]
    if fields:
        parts.append("<ul>" + "".join(fields) + "</ul>")
    parts.append("<strong>Bitte manuell als Auslage + Rechnung auf dem "
                 "Stromproduktion-Projekt erfassen.</strong>")
    return "<div>" + "<br>".join(parts) + "</div>"


def _li(label: str, value) -> str:
    if value is None or value == "":
        return ""
    return f"<li><strong>{label}:</strong> {escape(str(value))}</li>"


def _today() -> str:
    return dt.date.today().isoformat()
