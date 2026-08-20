"""SupplierInvoiceOcrService — turn a Moco draft into a real Moco purchase
pre-filled with Claude-Vision OCR data.

Approach: Moco's email-import drops supplier invoices in as **draft**
purchases, but those drafts can't be patched via the API (PATCH
/purchases/drafts/{id} → 404). So this service instead:

  1. Downloads the PDF attached to the draft.
  2. Runs OCR via `AnthropicOcrClient` → `InvoiceData`.
  3. Looks up the supplier in Moco's companies list via
     `MocoSupplierMatcher` (exact → substring → normalized token-set,
     each linking only on a unique hit). Ambiguous / no match → leave
     `company_id` empty for the human.
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

The original draft is auto-deleted once the new purchase landed
(best-effort — see `_delete_draft_after_create`). The webhook payload
itself is the draft; we only use its `id`, `title` (notification-email
detection on the no-attachment path) and its `file_url`.

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
import datetime as dt
import logging
import re
from dataclasses import replace
from html import escape
from typing import Any
from urllib import error as urlerror

from api.anthropic_ocr_client import (
    AnthropicOcrClient,
    InvoiceData,
    _lift_creditor_reference_from_purpose,
    _normalize_creditor_reference,
    _normalize_iban,
    _normalize_qr_reference,
)
from api.energy_credit_note_service import (
    EVU_TAG,
    EnergyCreditNoteService,
    is_energy_credit_note,
)
from api.moco_category_resolver import MocoCategoryResolver
from api.moco_project_resolver import MocoProjectResolver, ProjectMatch
from api.moco_purchase_client import MocoPurchaseClient
from api.moco_client import MocoClient
from api.moco_supplier_matcher import MocoSupplierMatcher
from api.smartme_energy_expense_service import (
    SmartmeEnergyExpenseService,
    is_smartme_draft,
)
from api.telegram_notifier import TelegramNotifier

logger = logging.getLogger("supplier_invoice_ocr_service")

CONFIDENCE_THRESHOLD = 0.85
OCR_TAGS = ["OCR", "Review pending"]

# Attachment-less drafts whose subject contains one of these are
# notification emails misrouted into the invoice inbox (e.g. a bank's
# "Sicherheitshinweis" or a delivery portal's "Zustellungshinweis") —
# they get deleted silently instead of triggering the no-attachment alert.
NOTIFICATION_SUBJECT_KEYWORDS = ("sicherheitshinweis", "zustellungshinweis")


class SupplierInvoiceOcrService:
    def __init__(self, *, moco: MocoClient,
                 purchase_client: MocoPurchaseClient,
                 ocr: AnthropicOcrClient,
                 subdomain: str,
                 telegram: TelegramNotifier | None = None,
                 project_resolver: MocoProjectResolver | None = None,
                 category_resolver: MocoCategoryResolver | None = None,
                 smartme: SmartmeEnergyExpenseService | None = None,
                 energy_credit_note: EnergyCreditNoteService | None = None):
        self._moco = moco
        self._purchases = purchase_client
        self._ocr = ocr
        self._subdomain = subdomain
        self._telegram = telegram
        # Optional — when set, drafts detected as smart-me
        # Energiekostenabrechnungen (see `is_smartme_draft`) are delegated
        # to the energy-expense branch instead of the OCR→purchase path.
        # Optional so existing unit tests can omit it.
        self._smartme = smartme
        # Optional — when set, drafts detected as EVU production credit
        # notes (see `is_energy_credit_note`) are delegated to the
        # expense+invoice branch instead of becoming a purchase. Checked
        # after the general OCR pass + supplier lookup, since (unlike
        # smart-me) there's no cheap pre-download signal for this
        # document class. Optional so existing unit tests can omit it.
        self._energy_credit_note = energy_credit_note
        # Optional — when set, the service resolves the OCR'd Kommission
        # to a Moco project and assigns each line item to it after the
        # purchase is created. Optional so existing unit tests that don't
        # care about assignment can omit it without setup churn.
        self._project_resolver = project_resolver
        # Optional — when set, the service picks `category_id` per the
        # Stage-3 chain (project Aufwandkonto → supplier Aufwandkonto →
        # already-paid omit → 4000 fallback). Same optional-collaborator
        # pattern.
        self._category_resolver = category_resolver

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
            self._notify(
                "⚠️ OCR übersprungen — Webhook ohne Draft-ID"
                + _draft_context_suffix(body)
            )
            return {"skipped": "no_purchase_id"}
        # smart-me Energiekostenabrechnungen are our OWN outgoing energy
        # statements, not supplier invoices — they become a project
        # expense, never a purchase. Detection runs before the file_url
        # gate on purpose: an attachment-less smart-me draft must hit the
        # smart-me branch's keep-and-alert path, not the notification
        # silent-delete below.
        if self._smartme is not None and is_smartme_draft(body):
            logger.info("ocr: draft %s detected as smart-me "
                        "Energiekostenabrechnung — routing to "
                        "energy-expense branch", draft_id)
            return self._smartme.process_draft(body)
        if not file_url:
            if _is_notification_subject(body.get("title")):
                # Bank/portal notification mails ("Sicherheitshinweis",
                # "Zustellungshinweis") land in the invoice inbox without
                # an attachment. They're routine noise, not a broken
                # import — delete the draft and stay off Telegram.
                logger.info("ocr: draft %s is a notification email "
                            "(title=%r) — deleting silently",
                            draft_id, body.get("title"))
                self._delete_notification_draft(draft_id)
                return {"skipped": "notification_draft_deleted",
                        "draft_id": draft_id}
            logger.warning("ocr: skipped (no file_url) draft_id=%s", draft_id)
            self._notify(
                "⚠️ OCR übersprungen — Draft ohne Anhang: "
                f"{self._draft_url(draft_id)}"
                + _draft_context_suffix(body)
            )
            return {"skipped": "no_file_url", "draft_id": draft_id}

        # Track OCR result outside the try so a Moco 4xx caught below can
        # still surface what was extracted (used by the batch validation
        # tool). Stays None when the 4xx fires before OCR (e.g. PDF
        # download 403).
        invoice: InvoiceData | None = None
        company_id: int | None = None
        # Set right before delegating to the energy-credit-note branch so
        # the except clause below can tell its HTTPErrors apart from a
        # purchase-creation failure: that branch's errors must propagate
        # to index.py's standard 4xx/5xx mapping (ok=false app error /
        # 502 retry), NOT the purchase-specific "silent skip" this
        # function uses for a routine duplicate-receipt 422.
        in_energy_credit_note_branch = False
        try:
            pdf_bytes = self._moco.download_file(file_url)
            logger.info("ocr: downloaded PDF draft_id=%s bytes=%d",
                        draft_id, len(pdf_bytes))

            invoice = self._ocr.extract(pdf_bytes)
            logger.info("ocr: extracted draft_id=%s confidence=%.2f "
                        "supplier=%r number=%r",
                        draft_id, invoice.confidence,
                        invoice.supplier_name, invoice.invoice_number)
            invoice = _prefer_draft_payment_fields(invoice, body)

            company_id = self._lookup_supplier_company(invoice.supplier_name)
            # Fetch the matched supplier's full record once — the list
            # shape from `list_suppliers` carries neither the default vat
            # code nor custom_properties, and both the vat chain and the
            # category chain (supplier Aufwandkonto) need them.
            company = self._fetch_company(company_id)

            # EVU production credit notes (see `is_energy_credit_note`)
            # become a project expense + Moco invoice, never a purchase —
            # delegate before any purchase-payload work. Detection needs
            # the OCR result + matched supplier company, so it can only
            # run here (unlike the smart-me check, which runs before the
            # PDF is even downloaded). THREE independent signals, any one
            # sufficient: the supplier-type company's own EVU tag; a
            # CUSTOMER-type company matching the supplier name carrying
            # the EVU tag instead (confirmed live for CKW and BKW — the
            # relationship a credit note represents is PVcontracting
            # selling production back to the EVU, i.e. the EVU as a
            # customer); or (fallback, for when neither company record is
            # tagged — confirmed live) a Stromproduktion project actually
            # existing for this supplier.
            if self._energy_credit_note is not None and (
                    is_energy_credit_note(invoice, company)
                    or (invoice.is_credit_note
                        and self._energy_credit_note.is_evu_tagged_customer(
                            invoice.supplier_name))
                    or (invoice.is_credit_note
                        and self._energy_credit_note.has_matching_project(
                            invoice.supplier_name))):
                logger.info("ocr: draft %s detected as EVU production "
                            "credit note — routing to energy-credit-note "
                            "branch", draft_id)
                in_energy_credit_note_branch = True
                return self._energy_credit_note.process(
                    pdf_bytes=pdf_bytes, invoice=invoice, company=company,
                    draft_id=draft_id, body=body)

            vat_code_id = self._resolve_vat_code_id(invoice, company)
            # Resolve the project first so the category lookup can use it
            # (project's / supplier's Aufwandkonto custom-property
            # overrides the 4000 default). The same match feeds the
            # post-create `assign_to_project` loop so we don't resolve
            # twice.
            project_match = self._resolve_project_match(invoice)
            category_id = self._resolve_category_id(invoice, project_match,
                                                    company)

            payload = _build_create_payload(
                invoice, pdf_bytes,
                vat_code_id=vat_code_id,
                company_id=company_id,
                draft_id=draft_id,
                user_id=_user_id_from_draft(body),
                category_id=category_id,
            )
            created = self._purchases.create_purchase(payload)
        except urlerror.HTTPError as e:
            if in_energy_credit_note_branch:
                # Let index.py's standard mapping handle it (4xx -> app
                # error/ok=false, 5xx -> 502 retry) instead of this
                # function's purchase-specific duplicate-receipt swallow.
                raise
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
            self._notify_moco_4xx(draft_id, e.code, err_body, body)
            return {"skipped": "moco_rejected", "draft_id": draft_id,
                    "moco_status": e.code, "moco_error": err_body,
                    # OCR fields are None when the 4xx fired before OCR
                    # ran (e.g. PDF download 403). When OCR did succeed
                    # batch tooling can show supplier/amount even on
                    # rejected rows.
                    "supplier_name": invoice.supplier_name if invoice else None,
                    "total_amount": invoice.total_amount if invoice else None,
                    "currency": invoice.currency if invoice else None,
                    "already_paid_by_card": (invoice.already_paid_by_card
                                              if invoice else False),
                    "company_id": company_id}

        new_purchase_id = created.get("id")
        logger.info("ocr: created purchase id=%s from draft=%s",
                    new_purchase_id, draft_id)

        assign_warnings: list[str] = []
        if new_purchase_id:
            self._post_summary_comments(new_purchase_id, invoice,
                                        draft_id, body)
            assign_warnings = self._assign_resolved_project(
                created, project_match)
            self._delete_draft_after_create(draft_id, new_purchase_id)

        # If we got this far without returning from the energy-credit-note
        # `if` above, all three of its detection signals came back False
        # for this draft — meaningful only when it's actually a credit
        # note and the service was configured to check at all (tests that
        # pass `energy_credit_note=None` intentionally skip detection
        # entirely, so no hint applies there).
        checked_energy_credit_note = self._energy_credit_note is not None
        self._notify_outcome(new_purchase_id, draft_id, invoice,
                             assign_warnings,
                             checked_energy_credit_note=checked_energy_credit_note)

        assigned_project = (project_match.project
                            if project_match and project_match.status == "matched"
                            else None)
        return {
            "draft_id": draft_id,
            "purchase_id": new_purchase_id,
            "confidence": invoice.confidence,
            "company_id": company_id,
            "is_credit_note": invoice.is_credit_note,
            "supplier_name": invoice.supplier_name,
            "total_amount": invoice.total_amount,
            "currency": invoice.currency,
            "already_paid_by_card": invoice.already_paid_by_card,
            "assigned_project_id": (assigned_project.get("id")
                                    if assigned_project else None),
            "assigned_project_name": (assigned_project.get("name")
                                      if assigned_project else None),
        }

    # --- vat code resolution ------------------------------------------------

    def _resolve_vat_code_id(self, invoice: InvoiceData,
                             supplier_company: dict | None) -> int | None:
        """Decide which Moco vat_code_id to put on the new purchase's item.

        Priority order (per the product spec):
          1. The OCR'd `vat_rate`, matched against the values in
             `GET /vat_code_purchases`.
          2. The matched supplier's default vat code (`supplier_company`
             is the full record from `get_company` — the company-list
             shape from `list_suppliers` doesn't carry the default).
          3. The vat_code from `GET /vat_code_purchases` marked as
             `default: true` (most Moco accounts have one designated
             default for purchases).
          4. Give up — return None. `POST /purchases` will 422 and the
             dispatcher fires a Telegram alert + ACKs 200 ok=false. Rare
             in practice, but better than guessing.

        A failure in any *individual* lookup (vat-codes list,
        `_fetch_company`) is logged and treated as "no match in this
        branch" — we don't want a flapping /vat_code_purchases to nuke an
        otherwise-good run when the supplier could still supply a
        fallback.
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

        if supplier_company:
            supplier_default = _supplier_default_vat_code_id(
                supplier_company, vat_codes,
            )
            if supplier_default is not None:
                logger.info("ocr: using supplier default vat_code_id=%s "
                            "(company_id=%s)",
                            supplier_default, supplier_company.get("id"))
                return supplier_default
            logger.info("ocr: supplier id=%s has no default vat_code, "
                        "falling back to account default",
                        supplier_company.get("id"))

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
        """Return a Moco company_id only on a unique tiered match.

        `MocoSupplierMatcher` tries exact → substring → normalized
        token-set matching against the full supplier list; each tier
        links only when exactly one company hits. Ambiguity or no match
        → leave the purchase company-less for the reviewer to assign.
        We prefer "no company" over "wrong company" — a misassigned
        supplier would invisibly skew downstream reporting.
        """
        if not supplier_name:
            return None
        try:
            suppliers = self._moco.list_suppliers()
            match = MocoSupplierMatcher(suppliers).match(supplier_name)
        except Exception:
            # Don't fail the whole sync just because supplier lookup
            # blew up — the purchase is the authoritative side effect;
            # the human can link the company manually.
            logger.exception("ocr: supplier lookup failed name=%r",
                             supplier_name)
            return None
        if match.status == "matched":
            logger.info("ocr: supplier_name=%r → company id=%s name=%r "
                        "(%s tier)", supplier_name,
                        match.company.get("id"), match.company.get("name"),
                        match.tier)
            return match.company.get("id")
        if match.status == "ambiguous":
            logger.info("ocr: supplier_name=%r matched %d companies at the "
                        "%s tier, leaving company_id empty (ambiguous)",
                        supplier_name, match.candidate_count, match.tier)
        else:
            logger.info("ocr: supplier_name=%r had no Moco company match",
                        supplier_name)
        return None

    def _fetch_company(self, company_id: int | None) -> dict | None:
        """Best-effort `get_company` for the matched supplier.

        Feeds both the vat-code chain (supplier default vat code) and the
        category chain (supplier Aufwandkonto). A failed fetch degrades
        those fallbacks but never fails the run — the purchase is still
        created and the reviewer fills the gaps.
        """
        if company_id is None:
            return None
        try:
            return self._moco.get_company(company_id)
        except Exception:
            logger.exception("ocr: get_company failed id=%s — supplier "
                             "vat/category fallbacks degraded", company_id)
            return None

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

    # --- project + category resolution --------------------------------------

    def _resolve_project_match(self, invoice: InvoiceData
                                ) -> ProjectMatch | None:
        """Run the Kommission→project resolver if one is wired. Returns
        None when no resolver was injected, otherwise the resolver's
        ProjectMatch (which may report `matched` / `ambiguous` /
        `no_match` / `empty`)."""
        if self._project_resolver is None:
            return None
        return self._project_resolver.resolve(invoice.commission)

    def _resolve_category_id(self, invoice: InvoiceData,
                              project_match: ProjectMatch | None,
                              supplier_company: dict | None
                              ) -> int | None:
        """Decide which `category_id` to set on the purchase's line item.

        Returns None whenever the caller should OMIT `category_id` from
        the payload (already-paid bill without an override, override
        miss, no 4000-fallback, or no resolver wired). The category
        resolver owns the chain (project Aufwandkonto → supplier
        Aufwandkonto → already-paid omit → 4000); this method just
        bridges to it.
        """
        if self._category_resolver is None:
            return None
        project = (project_match.project
                   if project_match and project_match.status == "matched"
                   else None)
        decision = self._category_resolver.resolve(
            already_paid_by_card=invoice.already_paid_by_card,
            project=project,
            supplier=supplier_company)
        logger.info("ocr: category_id=%s (%s)",
                    decision.category_id, decision.reason)
        return decision.category_id

    # --- project assignment -------------------------------------------------

    def _assign_resolved_project(self, created: dict,
                                  match: ProjectMatch | None) -> list[str]:
        """Link each line item of `created` to the resolved Moco project.

        Best-effort: the created purchase is the authoritative side effect.
        On any per-item failure we collect a short warning string for the
        Telegram alert; the sync still reports ok=true and the operator
        can finish the assignment manually during review.

        Skipped silently when:
          - no project resolver was injected (`match is None`);
          - the resolver returned `empty` / `no_match` / `ambiguous` —
            we prefer leaving the purchase project-less to mis-routing it.
        """
        warnings: list[str] = []
        if match is None:
            return warnings
        if match.status != "matched":
            logger.info("ocr: project assign skipped (status=%s)", match.status)
            return warnings

        purchase_id = created.get("id")
        items = created.get("items") or []
        if not items:
            logger.warning("ocr: created purchase %s has no items — skipping "
                           "project assign", purchase_id)
            return warnings

        project_id = match.project.get("id")
        project_name = match.project.get("name")
        for item in items:
            item_id = item.get("id") if isinstance(item, dict) else None
            if item_id is None:
                continue
            try:
                self._purchases.assign_item_to_project(
                    purchase_id, item_id,
                    project_id=project_id,
                    notify_project_leader=False,
                    billable=True,
                    budget_relevant=True,
                    surcharge=True,
                )
                logger.info("ocr: assigned purchase=%s item=%s to "
                            "project=%s (%r)",
                            purchase_id, item_id, project_id, project_name)
            except urlerror.HTTPError as e:
                err_body = "<unreadable>"
                try:
                    err_body = e.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    pass
                logger.warning("ocr: assign_to_project failed for "
                               "purchase=%s item=%s: HTTP %s %s",
                               purchase_id, item_id, e.code, err_body)
                warnings.append(f"Item {item_id}: HTTP {e.code} {err_body}")
            except Exception as e:
                logger.exception("ocr: assign_to_project error for "
                                 "purchase=%s item=%s", purchase_id, item_id)
                warnings.append(f"Item {item_id}: {e}")
        return warnings

    # --- telegram routing ---------------------------------------------------

    def _notify_outcome(self, purchase_id: int | None, draft_id: int,
                        invoice: InvoiceData,
                        assign_warnings: list[str] | None = None,
                        checked_energy_credit_note: bool = False) -> None:
        if not self._telegram:
            return
        link = (self._purchase_url(purchase_id) if purchase_id
                else self._draft_url(draft_id))
        supplier = invoice.supplier_name or "Unbekannt"
        amount = (f"{invoice.currency or 'CHF'} {invoice.total_amount:.2f}"
                  if invoice.total_amount is not None else "Betrag ?")
        # Append a warning block when one or more `assign_to_project` calls
        # failed after a successful create. The purchase exists, so this is
        # informational rather than a separate error alert — operator can
        # finish the assignment by hand from the same purchase link.
        suffix = ""
        if assign_warnings:
            suffix = ("\n⚠️ Projektzuweisung teilweise fehlgeschlagen "
                      f"({len(assign_warnings)}): "
                      + "; ".join(assign_warnings[:3]))
        if invoice.is_credit_note:
            # Gutschrift always triggers the alert regardless of confidence:
            # the reviewer must flip the sign on the total before approving.
            # When the energy-credit-note branch was actually checked and
            # declined this draft (all three detection signals came back
            # False — see the call site), add a soft, conditional hint: it
            # MIGHT be an EVU production credit that's missing its Moco
            # setup (an EVU tag on either company-type record, or a
            # matching Stromproduktion project), rather than a genuinely
            # unrelated credit note (e.g. a hardware return) that happens
            # to also be a Gutschrift. Phrased as "falls" (if) precisely
            # because we can't tell the difference — see
            # specs/SPEC_energy_credit_note.md.
            hint = ""
            if checked_energy_credit_note:
                hint = (
                    "\nℹ️ Falls dies eine EVU-Produktions-Gutschrift ist: "
                    f"EVU-Tag (\"{EVU_TAG}\") auf der Kunde- oder "
                    f"Lieferant-Firma \"{supplier}\" prüfen, oder ein "
                    "Stromproduktion-Projekt dafür anlegen."
                )
            self._telegram.notify(
                f"⚠️ Gutschrift erkannt ({invoice.confidence:.0%}) — "
                f"{supplier} {amount}\n"
                f"Moco-Purchase erstellt, Vorzeichen prüfen: {link}"
                f"{suffix}{hint}"
            )
            return
        if invoice.confidence >= CONFIDENCE_THRESHOLD:
            self._telegram.notify(
                f"✅ OCR erfolgreich ({invoice.confidence:.0%}) — "
                f"{supplier} {amount}\n"
                f"Moco-Purchase erstellt, bitte prüfen: {link}"
                f"{suffix}"
            )
        else:
            self._telegram.notify(
                f"⚠️ OCR unsicher ({invoice.confidence:.0%}) — "
                f"{supplier} {amount}\n"
                f"Moco-Purchase erstellt, bitte manuell prüfen: {link}"
                f"{suffix}"
            )

    def _notify(self, text: str) -> None:
        if self._telegram:
            self._telegram.notify(text)

    def _delete_notification_draft(self, draft_id: int) -> None:
        """Delete an attachment-less notification-email draft, silently.

        Deliberately quieter than `_delete_draft_after_create`: these
        drafts carry no invoice, so a failed delete just leaves a stale
        entry in Moco's draft list where the operator will see it anyway.
        404 counts as "already gone" (webhook replay); every other
        failure logs a warning — no Telegram in any case.
        """
        try:
            self._purchases.delete_purchase_draft(draft_id)
            logger.info("ocr: deleted notification draft %s", draft_id)
        except urlerror.HTTPError as e:
            if e.code == 404:
                logger.info("ocr: notification draft %s already gone "
                            "(delete idempotent)", draft_id)
                return
            err_body = "<unreadable>"
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            logger.warning("ocr: failed to delete notification draft %s: "
                           "%s %s", draft_id, e.code, err_body)
        except Exception as e:
            logger.warning("ocr: failed to delete notification draft %s: %s",
                           draft_id, e)

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
                         err_body: str, body: dict) -> None:
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
            f"Draft: {self._draft_url(draft_id)}"
            + _draft_context_suffix(body)
            + f"\nDetail: {err_body}"
        )

    def _purchase_url(self, purchase_id: int) -> str:
        return (f"https://{self._subdomain}.mocoapp.com"
                f"/purchases/{purchase_id}")

    def _draft_url(self, draft_id: int) -> str:
        return (f"https://{self._subdomain}.mocoapp.com"
                f"/purchases/drafts/{draft_id}")


def _is_notification_subject(title: object) -> bool:
    """True when the draft subject marks a notification email.

    Case-insensitive substring match so forwarded-subject prefixes
    ("WG: Sicherheitshinweis …") and all-caps variants still hit.
    """
    if not isinstance(title, str):
        return False
    lowered = title.lower()
    return any(kw in lowered for kw in NOTIFICATION_SUBJECT_KEYWORDS)


_DRAFT_CONTEXT_FIELD_MAX = 120


def _draft_context_suffix(body: dict) -> str:
    """Format the Betreff/Absender lines appended to skip notifications.

    Each line is omitted when its source field is empty/missing, so a
    manually-uploaded draft with no email metadata produces an empty
    suffix rather than `Betreff: —` noise. Long forwarded subject chains
    are truncated to keep the Telegram message readable.
    """
    lines: list[str] = []
    title = _clean_context_field(body.get("title"))
    if title:
        lines.append(f"Betreff: {title}")
    sender = _clean_context_field(body.get("email_from"))
    if sender:
        lines.append(f"Absender: {sender}")
    return "\n" + "\n".join(lines) if lines else ""


def _clean_context_field(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    if len(cleaned) > _DRAFT_CONTEXT_FIELD_MAX:
        cleaned = cleaned[:_DRAFT_CONTEXT_FIELD_MAX - 1] + "…"
    return cleaned


def _user_id_from_draft(body: dict) -> int | None:
    """Extract the Moco user id from a draft purchase body.

    Webhook bodies carry the user as a nested object: `{"user": {"id":
    933719334, "firstname": …}}` (same shape as Activity / Contact /
    Invoice events — see fixtures). Returns None when absent or
    malformed, so callers can omit the `user_id` field rather than push
    a junk value into Moco.
    """
    user = body.get("user")
    if isinstance(user, dict):
        uid = user.get("id")
        if isinstance(uid, int):
            return uid
    return None


# --- payload construction ---------------------------------------------------

def _build_create_payload(invoice: InvoiceData, pdf_bytes: bytes, *,
                          vat_code_id: int | None,
                          company_id: int | None,
                          draft_id: int,
                          user_id: int | None = None,
                          category_id: int | None = None) -> dict[str, Any]:
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
    # `category_id` is the Buchhaltungs-Konto. Omitted when the resolver
    # couldn't pick one (already-paid card receipt without an override,
    # project/supplier Aufwandkonto miss, missing 4000 fallback) — Moco
    # accepts the purchase with its own default and the reviewer picks
    # an account during approval.
    if category_id is not None:
        item["category_id"] = category_id

    payment_method = _payment_method_for(invoice)
    reference_value, info_value = _resolve_reference_and_info(
        invoice, payment_method)
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

    # Already-paid card receipts: skip due_date + IBAN entirely. There's
    # nothing to schedule and no transfer target — surfacing an IBAN on a
    # closed bill would be misleading to anyone scanning the Moco UI.
    if not invoice.already_paid_by_card:
        due_date = _resolve_due_date(payload["date"], invoice.due_date)
        if due_date:
            payload["due_date"] = due_date
    if invoice.invoice_number:
        payload["receipt_identifier"] = invoice.invoice_number
    if invoice.iban and not invoice.already_paid_by_card:
        payload["iban"] = invoice.iban
    if reference_value:
        payload["reference"] = reference_value
    if info_value:
        payload["info"] = info_value
    if company_id is not None:
        payload["company_id"] = company_id
    # Carry the draft's user across to the created purchase when present —
    # email-imported drafts are usually associated with the inbox owner
    # (or whoever forwarded the mail), and propagating that keeps Moco's
    # "Mein Aufwand" filter and per-user reports correct. None falls back
    # to whatever default Moco assigns to API-created purchases.
    if user_id is not None:
        payload["user_id"] = user_id

    return payload


def _resolve_reference_and_info(invoice: InvoiceData,
                                payment_method: str) -> tuple[str | None, str | None]:
    """Pick the value for Moco's `reference` field and adjust `info` to match.

    Moco's purchase `reference` field accepts two distinct creditor-reference
    formats, each tied to a payment method:
      - QR-bill (Swiss): 27-digit numeric `qr_reference`, only valid alongside
        a QR-IBAN and `payment_method=bank_transfer_swiss_qr_esr`.
      - Plain bank transfer (any IBAN): ISO 11649 SCOR / structured creditor
        reference, `RF<dd><alnum>`.

    Selection priority:
      1. QR-reference when paired with a QR-IBAN — Swiss QR-bill path.
      2. SCOR creditor reference — works with any IBAN. The model sometimes
         echoes the SCOR into `payment_purpose` even after the prompt update;
         strip it from the resulting `info` so the reviewer doesn't see the
         same string twice.
      3. None — leave the reference field unset; the human reviewer can fill
         it during approval.

    A QR-reference present alongside a non-QR-IBAN is dropped (no `reference`
    set) because Moco would 422 the QR-ESR path. The SCOR fallback can still
    fire on the same invoice if the model also extracted one.
    """
    info = invoice.payment_purpose
    if payment_method == "credit_card":
        # Bill is already settled — no outbound payment to reconcile, so
        # neither the QR-reference nor the SCOR creditor reference belongs
        # in the Moco purchase. The Zahlungszweck stays in `info` if the
        # model extracted it (useful context for the reviewer).
        return None, info
    if invoice.qr_reference and payment_method == "bank_transfer_swiss_qr_esr":
        return invoice.qr_reference, info
    if invoice.qr_reference and not _is_qr_iban(invoice.iban):
        logger.warning("ocr: extracted qr_reference=%r but iban=%r is not a "
                       "QR-IBAN — dropping the QR-reference; will try a SCOR "
                       "fallback", invoice.qr_reference, invoice.iban)
    if invoice.creditor_reference:
        # Double-strip defense: `_to_invoice_data` already lifts SCOR out of
        # `payment_purpose` on the OCR layer, but a draft-override path
        # (`_prefer_draft_payment_fields`) can populate creditor_reference
        # after that lift ran, leaving the SCOR text in the OCR's
        # payment_purpose untouched. Re-run the lift here as a safety net so
        # the info field never duplicates what's already in `reference`.
        if info:
            _, info = _lift_creditor_reference_from_purpose(info)
        return invoice.creditor_reference, info
    return None, info


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


def _supplier_default_vat_code_id(company: dict,
                                  vat_codes: list[dict]) -> int | None:
    """Resolve the supplier's default vat_code_id.

    Per Moco's company docs the relevant field is `supplier_vat`, a nested
    object with a `tax` percentage (e.g. `{"supplier_vat": {"tax": 8.1}}`).
    There's no direct `vat_code_id` on the company — we have to translate
    the rate by looking it up in the same `/vat_code_purchases` list used
    for OCR's `vat_rate` match.

    Defensive fallback: a couple of older / alternate field names
    (`default_vat_code_purchase_id`, `vat_code_purchase_id`) are also
    tried in case some accounts return a direct id. The first hit wins.
    """
    supplier_vat = company.get("supplier_vat")
    if isinstance(supplier_vat, dict) and supplier_vat.get("tax") is not None:
        try:
            rate = float(supplier_vat["tax"])
        except (TypeError, ValueError):
            rate = None
        if rate is not None:
            match = _find_vat_code_by_rate(vat_codes, rate)
            if match is not None:
                return match.get("id")
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

    raw_reference = draft.get("reference")
    draft_qr_reference = _normalize_qr_reference(raw_reference)
    if draft_qr_reference:
        if invoice.qr_reference and invoice.qr_reference != draft_qr_reference:
            logger.info("ocr: overriding OCR qr_reference=%s with draft "
                        "reference=%s",
                        invoice.qr_reference, draft_qr_reference)
        updates["qr_reference"] = draft_qr_reference
    else:
        # Same field, different shape — Moco's email-import puts whatever it
        # parsed from the Zahlteil into `reference`, which for non-QR-bill
        # invoices is the ISO 11649 SCOR string.
        draft_scor = _normalize_creditor_reference(raw_reference)
        if draft_scor:
            if (invoice.creditor_reference
                    and invoice.creditor_reference != draft_scor):
                logger.info("ocr: overriding OCR creditor_reference=%s with "
                            "draft reference=%s",
                            invoice.creditor_reference, draft_scor)
            updates["creditor_reference"] = draft_scor

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


def _resolve_due_date(invoice_date: str | None,
                      ocr_due_date: str | None) -> str | None:
    """Decide the `due_date` to send on the new Moco purchase.

    Rules (in priority order):
      1. Use the OCR-extracted due_date when present.
      2. Otherwise compute `invoice_date + 30 days`.
      3. If the resulting date falls on a Saturday or Sunday, roll back
         to the preceding Friday — supplier payment runs are weekday-only
         and a weekend due_date would either bounce or settle late.

    Returns None if neither input yields a parseable ISO date (the
    payload then simply omits the `due_date` field, which Moco accepts).
    """
    candidate = ocr_due_date
    if not candidate:
        try:
            base = dt.date.fromisoformat(invoice_date) if invoice_date else None
        except (TypeError, ValueError):
            base = None
        if base is None:
            return None
        candidate = (base + dt.timedelta(days=30)).isoformat()
    try:
        d = dt.date.fromisoformat(candidate)
    except (TypeError, ValueError):
        # Couldn't parse (OCR returned a non-ISO string?). Surface as-is
        # — Moco will validate and we'll see the error.
        return candidate
    # Saturday=5, Sunday=6 → roll back to Friday.
    if d.weekday() == 5:
        d -= dt.timedelta(days=1)
    elif d.weekday() == 6:
        d -= dt.timedelta(days=2)
    return d.isoformat()


def _payment_method_for(invoice: InvoiceData) -> str:
    """Pick the Moco payment_method enum from what OCR found.

    Priority:
      1. `already_paid_by_card` → `credit_card`. The bill is closed; we
         expose card-paid AND POS-terminal cases as the same enum (Moco
         doesn't have a dedicated POS / EFT value and the operator only
         cares that no outbound transfer is owed).
      2. QR-ESR — requires a QR-IBAN AND a QR-reference together; Moco
         enforces both. A QR-reference with a regular (non-QR) IBAN is a
         common OCR misread and would 422; fall through to plain
         `bank_transfer` rather than push a guaranteed failure.
      3. Plain `bank_transfer` (default).
    """
    if invoice.already_paid_by_card:
        return "credit_card"
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

    # Build the Betrag cell so an already-paid card / POS bill carries the
    # marker inline with the amount it modifies (rather than as a separate
    # top-of-comment banner) — visually couples "what was paid" with "how
    # it was paid", and keeps the reviewer's eye on a single field.
    if invoice.total_amount is not None:
        betrag = f"{invoice.currency or 'CHF'} {invoice.total_amount:.2f}"
        if invoice.already_paid_by_card:
            betrag += " — 💳 bereits bezahlt (Karte / Terminal)"
    else:
        betrag = None

    fields: list[str] = []
    fields.append(_li("Kommission", invoice.commission))
    fields.append(_li("Lieferadresse", invoice.delivery_address))
    fields.append(_li("Lieferant", invoice.supplier_name))
    fields.append(_li("Adresse", invoice.supplier_address))
    fields.append(_li("Betrag", betrag))
    fields.append(_li("Datum", invoice.invoice_date))
    fields.append(_li("Fällig", invoice.due_date))
    fields.append(_li("Rechnungs-Nr", invoice.invoice_number))
    fields.append(_li("IBAN", invoice.iban))
    fields.append(_li("QR-Ref", invoice.qr_reference))
    fields.append(_li("Referenz", invoice.creditor_reference))
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
        body = _normalize_email_whitespace(email_body)
        original_len = len(body)
        truncated = ""
        if len(body) > EMAIL_BODY_MAX_CHARS:
            body = body[:EMAIL_BODY_MAX_CHARS]
            truncated = (f"\n[…gekürzt von {original_len} auf "
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


def _normalize_email_whitespace(body: str) -> str:
    """Strip noise whitespace from a forwarded email body, keep structure.

    Forwarded emails from Outlook / webmail clients often arrive with
    massive runs of `\\r\\n\\t\\t…` indentation, soft-hyphen / zero-width
    invisible chars sprinkled by email tracking, and stretches of
    non-breaking spaces. Posting that verbatim into a Moco comment
    drowns the actual content. This normalizer keeps the meaningful
    structure (line breaks at sentence/paragraph boundaries, single
    spaces inside text) but removes the noise:

      - CRLF / CR → LF
      - tabs → single space
      - zero-width / soft-hyphen / BOM chars → removed
      - non-breaking spaces / figure spaces → regular space
      - runs of spaces → one space
      - trailing spaces per line → stripped
      - three or more blank lines → one blank line
      - leading / trailing blank lines → stripped
    """
    if not body:
        return body
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body = body.translate(str.maketrans({
        "\u200B": "",       # ZERO WIDTH SPACE
        "\u200C": "",       # ZERO WIDTH NON-JOINER (the `\u034F`-looking
                          # combiner-glyph common in tracker-bloated mails)
        "\u200D": "",       # ZERO WIDTH JOINER
        "\uFEFF": "",       # BYTE ORDER MARK
        "\u034F": "",       # COMBINING GRAPHEME JOINER (the `\u034f` glyph)
        "\xAD":  "",       # SOFT HYPHEN
        "\u2007": " ",      # FIGURE SPACE -> regular space
        "\xA0":  " ",      # NO-BREAK SPACE -> regular space
        "\t":    " ",
    }))
    # strip() per line: leading whitespace on a body line is almost always
    # email-noise (Outlook tab indentation) — not deliberate code-block
    # style. Run-of-spaces inside the line collapses to one first so
    # `re.sub` doesn't have to fight `strip()`.
    lines = [re.sub(r" +", " ", line).strip() for line in body.split("\n")]
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if line:
            out.append(line)
            blank_run = 0
        else:
            blank_run += 1
            if blank_run == 1:
                out.append("")
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


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
