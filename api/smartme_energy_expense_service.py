"""SmartmeEnergyExpenseService — book a smart-me Energiekostenabrechnung
as a billable expense on the matching Moco project.

smart-me (the metering platform) emails periodic "Energiekostenabrechnung"
statements to the same inbox that feeds the supplier-invoice OCR flow.
These are NOT supplier invoices — they are PVcontracting's own outgoing
energy statements for ZEV / Eigenverbrauch installations. Running them
through the generic OCR→purchase path would create bogus supplier
purchases, so `SupplierInvoiceOcrService.process` detects them (see
`is_smartme_draft`) and delegates here instead. This service:

  1. Downloads the attached PDF from the draft's signed `file_url`.
  2. OCRs it with the energy-bill schema (`extract_energy_bill`) →
     Objekt / Netto-Betrag / Abrechnungszeitraum / Rechnungsdatum.
  3. Matches the Objekt to exactly one ZEV/Eigenverbrauch-labeled project
     (`SmartmeProjectMatcher`, best-token-overlap).
  4. Creates a project expense via `POST /projects/{id}/expenses` with
     the field conventions the operator uses manually: quantity 1,
     unit "Netto", unit_price = Netto-Betrag, unit_cost 0, billable,
     budget_relevant, service_period = Abrechnungszeitraum, PDF attached.
     The title depends on the project label: ZEV → "Solar- und Netzstrom
     gemäss Beilage", Eigenverbrauch → "Solarstrom Eigenverbrauch gemäss
     Beilage" (ZEV wins when both labels are present).
  5. On success: best-effort deletes the draft (404 = already gone) and
     sends a Telegram summary — there is no "Review pending" tag on
     expenses, so the Telegram message is the review hook.

Anything that prevents a confident booking — no attachment, blank/unknown
Objekt, ambiguous match, missing Netto-Betrag — takes the *keep* path:
Telegram alert + best-effort comment on the draft itself
(`commentable_type="PurchaseDraft"`), the draft stays in the inbox for
manual handling, and the webhook ACKs ok=true (a retry can't fix it).
OCR and Moco errors propagate so `index.py`'s existing mapping applies
(4xx → 200 ok=false + Telegram, 5xx/URLError → 502 retry).
"""

import base64
import logging
from html import escape
from typing import Any
from urllib import error as urlerror

from api.anthropic_ocr_client import AnthropicOcrClient, EnergyBillData
from api.moco_client import MocoClient
from api.moco_purchase_client import MocoPurchaseClient
from api.smartme_project_matcher import (
    SmartmeProjectMatch,
    SmartmeProjectMatcher,
    project_energy_label,
)
from api.telegram_notifier import TelegramNotifier

logger = logging.getLogger("smartme_energy_expense_service")

SMARTME_SENDER = "no-reply@smart-me.com"
TITLE_ZEV = "Solar- und Netzstrom gemäss Beilage"
TITLE_EIGENVERBRAUCH = "Solarstrom Eigenverbrauch gemäss Beilage"
# Draft purchases are their own polymorphic comment target — "Purchase"
# with a draft id 404s (drafts live in a separate URL space, see
# MocoPurchaseClient.get_purchase_draft).
COMMENTABLE_TYPE_DRAFT = "PurchaseDraft"
CONFIDENCE_THRESHOLD = 0.85


def is_smartme_draft(body: dict) -> bool:
    """True when the draft is a smart-me Energiekostenabrechnung.

    Two out of three signals must fire (all case-insensitive), so a
    coincidental keyword in an unrelated invoice subject can't hijack the
    branch, while a forwarded mail (where `email_from` is the forwarder,
    not smart-me) still detects via title + body markers:

      (a) `title` contains "energiekostenabrechnung"
      (b) `email_from` or `email_body` contains "no-reply@smart-me.com"
      (c) `email_body` contains "objektname" or "abrechnungszeitraum"
    """
    title = _lowered(body.get("title"))
    email_from = _lowered(body.get("email_from"))
    email_body = _lowered(body.get("email_body"))

    signals = 0
    if "energiekostenabrechnung" in title:
        signals += 1
    if SMARTME_SENDER in email_from or SMARTME_SENDER in email_body:
        signals += 1
    if "objektname" in email_body or "abrechnungszeitraum" in email_body:
        signals += 1
    return signals >= 2


def _lowered(value: object) -> str:
    return value.lower() if isinstance(value, str) else ""


class SmartmeEnergyExpenseService:
    def __init__(self, *, moco: MocoClient,
                 purchase_client: MocoPurchaseClient,
                 ocr: AnthropicOcrClient,
                 matcher: SmartmeProjectMatcher,
                 subdomain: str,
                 telegram: TelegramNotifier | None = None):
        self._moco = moco
        self._purchases = purchase_client
        self._ocr = ocr
        self._matcher = matcher
        self._subdomain = subdomain
        self._telegram = telegram

    def process_draft(self, body: dict) -> dict[str, Any]:
        """Drive one detected smart-me draft through OCR + expense create.

        The caller (`SupplierInvoiceOcrService.process`) has already
        validated event=create and the presence of a draft id.
        """
        draft_id = body.get("id")
        file_url = body.get("file_url")
        if not file_url:
            # Unlike notification emails, an attachment-less smart-me
            # draft is a real Abrechnung whose PDF went missing — keep it
            # and alert instead of deleting.
            logger.warning("smartme: draft %s has no attachment", draft_id)
            return self._keep_draft(
                draft_id, bill=None, reason="Draft ohne PDF-Anhang",
                skipped="smartme_no_attachment")

        pdf_bytes = self._moco.download_file(file_url)
        logger.info("smartme: downloaded PDF draft_id=%s bytes=%d",
                    draft_id, len(pdf_bytes))

        bill = self._ocr.extract_energy_bill(pdf_bytes)
        logger.info("smartme: extracted draft_id=%s confidence=%.2f "
                    "objekt=%r net=%r period=%r..%r",
                    draft_id, bill.confidence, bill.objekt,
                    bill.net_amount, bill.period_from, bill.period_to)

        match = self._matcher.match(bill.objekt)
        if match.status != "matched":
            logger.warning("smartme: draft %s objekt=%r not matched "
                           "(status=%s candidates=%d)",
                           draft_id, bill.objekt, match.status,
                           match.candidate_count)
            return self._keep_draft(
                draft_id, bill=bill,
                reason=_unmatched_reason(bill, match),
                skipped="smartme_project_unmatched",
                extra={"match_status": match.status,
                       "objekt": bill.objekt})
        if bill.net_amount is None:
            logger.warning("smartme: draft %s has no Netto-Betrag", draft_id)
            return self._keep_draft(
                draft_id, bill=bill,
                reason="OCR fand keinen Netto-Betrag",
                skipped="smartme_no_net_amount",
                extra={"objekt": bill.objekt})

        project = match.project
        project_id = project.get("id")
        title = _expense_title(project)
        payload = _build_expense_payload(bill, pdf_bytes, title=title,
                                         draft_id=draft_id)
        # HTTPError propagates — index.py maps 4xx → app error (Telegram +
        # 200 ok=false) and 5xx → 502 retry. There's no known routine 4xx
        # here (no duplicate-detection like receipt_identifier), so no
        # service-internal swallow.
        created = self._moco.create_project_expense(project_id, payload)
        expense_id = created.get("id")
        logger.info("smartme: created expense id=%s project=%s (%r) "
                    "from draft=%s", expense_id, project_id,
                    project.get("name"), draft_id)

        self._delete_draft_after_create(draft_id, project_id, expense_id)
        self._notify_success(project, bill, draft_id)

        return {
            "smartme": True,
            "draft_id": draft_id,
            "expense_id": expense_id,
            "project_id": project_id,
            "project_name": project.get("name"),
            "expense_title": title,
            "net_amount": bill.net_amount,
            "period_from": bill.period_from,
            "period_to": bill.period_to,
            "confidence": bill.confidence,
        }

    # --- keep-draft path ------------------------------------------------

    def _keep_draft(self, draft_id: int, *, bill: EnergyBillData | None,
                    reason: str, skipped: str,
                    extra: dict | None = None) -> dict[str, Any]:
        """Alert + comment on the draft, leave it in the inbox, ACK ok.

        The draft stays as the operator's work item — deleting it would
        destroy the only trace of an unbooked Abrechnung. The comment
        mirrors the alert so the context is visible inside Moco too.
        """
        self._notify(
            "⚠️ smart-me Energiekostenabrechnung nicht verbucht — "
            f"{reason}\n"
            + _bill_summary_lines(bill)
            + f"Draft (bleibt bestehen): {self._draft_url(draft_id)}"
        )
        self._post_draft_comment(draft_id, _format_keep_comment(bill, reason))
        result: dict[str, Any] = {"smartme": True, "skipped": skipped,
                                  "draft_id": draft_id}
        if extra:
            result.update(extra)
        return result

    def _post_draft_comment(self, draft_id: int, text: str) -> None:
        """Best-effort comment on the draft itself.

        A failed comment must not escalate — the Telegram alert already
        carries the same information (per `feedback_soft_failure_logging`:
        tidy warning, no traceback).
        """
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
            logger.warning("smartme: draft comment failed draft_id=%s: "
                           "%s %s", draft_id, e.code, err_body)
        except Exception as e:
            logger.warning("smartme: draft comment failed draft_id=%s: %s",
                           draft_id, e)

    # --- post-create steps ------------------------------------------------

    def _delete_draft_after_create(self, draft_id: int, project_id: int,
                                   expense_id: int | None) -> None:
        """Remove the draft once the expense landed.

        Best-effort mirror of the OCR service's `_delete_draft_after_create`:
        the expense is the authoritative side effect, so a failed delete
        must NOT roll the sync back. 404 = already gone (webhook replay),
        swallowed silently; any other failure logs a warning + Telegram so
        the operator can clean up by hand.
        """
        try:
            self._purchases.delete_purchase_draft(draft_id)
        except urlerror.HTTPError as e:
            if e.code == 404:
                logger.info("smartme: draft %s already gone "
                            "(delete idempotent)", draft_id)
                return
            err_body = "<unreadable>"
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            logger.warning("smartme: failed to delete draft %s after "
                           "creating expense %s: %s %s",
                           draft_id, expense_id, e.code, err_body)
            self._notify_draft_delete_failed(draft_id, project_id,
                                             e.code, err_body)
        except Exception as e:
            logger.exception("smartme: unexpected error deleting draft %s",
                             draft_id)
            self._notify_draft_delete_failed(draft_id, project_id,
                                             None, str(e))

    def _notify_draft_delete_failed(self, draft_id: int, project_id: int,
                                    status_code: int | None,
                                    detail: str) -> None:
        status = f"HTTP {status_code}" if status_code else "Exception"
        self._notify(
            "⚠️ smart-me Draft konnte nach erfolgreichem Expense-Create "
            "nicht gelöscht werden — manuell entfernen:\n"
            f"Draft: {self._draft_url(draft_id)}\n"
            f"Auslagen: {self._project_expenses_url(project_id)}\n"
            f"Detail: {status} {detail}"
        )

    def _notify_success(self, project: dict, bill: EnergyBillData,
                        draft_id: int) -> None:
        confidence_note = (""
                           if bill.confidence >= CONFIDENCE_THRESHOLD
                           else " — bitte prüfen")
        icon = "✅" if bill.confidence >= CONFIDENCE_THRESHOLD else "⚠️"
        self._notify(
            f"{icon} smart-me Energiekostenabrechnung verbucht "
            f"({bill.confidence:.0%}){confidence_note} — "
            f"{project.get('name')}\n"
            + _bill_summary_lines(bill)
            + f"Auslagen: {self._project_expenses_url(project.get('id'))}"
        )

    def _notify(self, text: str) -> None:
        if self._telegram:
            self._telegram.notify(text)

    def _draft_url(self, draft_id: int) -> str:
        return (f"https://{self._subdomain}.mocoapp.com"
                f"/purchases/drafts/{draft_id}")

    def _project_expenses_url(self, project_id: int) -> str:
        return (f"https://{self._subdomain}.mocoapp.com"
                f"/projects/{project_id}/expenses")


# --- pure helpers -------------------------------------------------------


def _expense_title(project: dict) -> str:
    """ZEV → Netzstrom title, otherwise the Eigenverbrauch title.

    `project_energy_label` already prefers ZEV when both labels are
    present; the matcher only surfaces labeled projects, so the fallback
    branch is Eigenverbrauch by construction.
    """
    if project_energy_label(project) == "ZEV":
        return TITLE_ZEV
    return TITLE_EIGENVERBRAUCH


def _build_expense_payload(bill: EnergyBillData, pdf_bytes: bytes, *,
                           title: str, draft_id: int) -> dict[str, Any]:
    """Construct the POST /projects/{id}/expenses body.

    Field conventions copied from the operator's manual entries on the
    ZEV/Eigenverbrauch projects: one "Netto" unit at the net amount,
    zero cost (the solar power itself has no purchase cost), billable +
    budget_relevant, the Abrechnungszeitraum as the service period, and
    the source PDF attached. The service period is only sent when both
    ends were extracted — half a period would render misleadingly in
    Moco's UI.
    """
    payload: dict[str, Any] = {
        "date": bill.invoice_date or _today(),
        "title": title,
        "quantity": 1,
        "unit": "Netto",
        "unit_price": bill.net_amount,
        "unit_cost": 0,
        "billable": True,
        "budget_relevant": True,
        "file": {
            "filename": _attachment_filename(bill, draft_id),
            "base64": base64.b64encode(pdf_bytes).decode("ascii"),
        },
    }
    if bill.period_from and bill.period_to:
        payload["service_period_from"] = bill.period_from
        payload["service_period_to"] = bill.period_to
    return payload


def _attachment_filename(bill: EnergyBillData, draft_id: int) -> str:
    parts = [bill.invoice_date, "Energiekostenabrechnung",
             bill.invoice_number]
    base = " ".join(p for p in parts if p) or f"draft-{draft_id}"
    base = base.replace("/", "-").replace("\\", "-")
    return f"{base}.pdf"


def _bill_summary_lines(bill: EnergyBillData | None) -> str:
    """The Objekt/Betrag/Zeitraum block shared by all Telegram messages.

    Ends with a newline when non-empty so callers can append their link
    line unconditionally. Only extracted fields are rendered — the
    no-attachment path has no bill at all and produces "".
    """
    if bill is None:
        return ""
    lines: list[str] = []
    if bill.objekt:
        lines.append(f"Objekt: {bill.objekt}")
    if bill.net_amount is not None:
        lines.append(f"Betrag: CHF {bill.net_amount:.2f} (netto)")
    if bill.period_from and bill.period_to:
        lines.append(f"Zeitraum: {bill.period_from} – {bill.period_to}")
    return "\n".join(lines) + "\n" if lines else ""


def _unmatched_reason(bill: EnergyBillData,
                      match: SmartmeProjectMatch) -> str:
    if match.status == "empty":
        return "OCR fand kein Objekt"
    if match.status == "ambiguous":
        return (f"Objekt passt auf {match.candidate_count} Projekte "
                "(mehrdeutig — smart-me Objektname ins Kommission-Feld "
                "des Zielprojekts eintragen)")
    return ("kein ZEV/Eigenverbrauch-Projekt passt zum Objekt "
            "(smart-me Objektname ins Kommission-Feld des Zielprojekts "
            "eintragen)")


def _format_keep_comment(bill: EnergyBillData | None, reason: str) -> str:
    """HTML comment left on the kept draft (Moco-allowed tag subset only)."""
    parts = ["<strong>🤖 smart-me Energiekostenabrechnung — nicht "
             f"automatisch verbucht:</strong> {escape(reason)}"]
    fields: list[str] = []
    if bill is not None:
        fields.append(_li("Objekt", bill.objekt))
        fields.append(_li("Netto-Betrag",
                          f"CHF {bill.net_amount:.2f}"
                          if bill.net_amount is not None else None))
        zeitraum = (f"{bill.period_from} – {bill.period_to}"
                    if bill.period_from and bill.period_to else None)
        fields.append(_li("Abrechnungszeitraum", zeitraum))
        fields.append(_li("Rechnungs-Nr", bill.invoice_number))
        fields = [li for li in fields if li]
    if fields:
        parts.append("<ul>" + "".join(fields) + "</ul>")
    parts.append("<strong>Bitte manuell als Auslage auf dem "
                 "ZEV/Eigenverbrauch-Projekt erfassen.</strong>")
    return "<div>" + "<br>".join(parts) + "</div>"


def _li(label: str, value) -> str:
    if value is None or value == "":
        return ""
    return f"<li><strong>{label}:</strong> {escape(str(value))}</li>"


def _today() -> str:
    import datetime as dt
    return dt.date.today().isoformat()
