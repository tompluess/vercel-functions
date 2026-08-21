#!/usr/bin/env python3
"""Batch OCR validation across all Moco draft purchases.

Lists `GET /purchases/drafts` (newest first), runs the same in-process
pipeline as scripts/test_ocr_create_purchase.py against each draft, and prints a
summary table with one row per draft. Per-draft live logs surface PDF
size, OCR latency, supplier-lookup outcome, VAT-code resolution tier,
chosen payment method, and confidence / Gutschrift flags as each draft
is processed — so the operator can spot anomalies before the table
even prints.

Default mode is dry-run: OCR runs against each draft's PDF, but NO Moco
purchase is created and NO draft is deleted. `--apply` switches to
production behavior — POST /purchases + comments + draft-delete for
every draft, exactly like the webhook handler. Attachment-less drafts
whose subject marks a notification email ("Sicherheitshinweis" /
"Zustellungshinweis") are deleted in apply mode (webhook parity); in
dry-run the row only reports that the draft would be deleted.

Usage (from the repo root):
    vercel env pull .env.local
    .venv/bin/python scripts/batch_ocr_drafts.py --max 5              # dry-run
    .venv/bin/python scripts/batch_ocr_drafts.py --max 5 --apply      # real writes
    .venv/bin/python scripts/batch_ocr_drafts.py --draft-id 3143995   # one draft, dry-run

Required env (same as test_ocr_create_purchase.py):
    MOCO_SUBDOMAIN    source subdomain (e.g. "solar")
    MOCO_API_KEY        token for the Moco account
    ANTHROPIC_API_KEY          Claude API key

Exit codes: 0 — table printed (per-draft errors are recorded as rows,
not fatal); 2 — missing env / bad args; 3 — could not list drafts.
"""

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import error as urlerror

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.anthropic_ocr_client import AnthropicOcrClient, AnthropicOcrError
from api.energy_credit_note_service import (
    EnergyCreditNoteService,
    _derive_net_amount,
    is_energy_credit_note,
)
from api.moco_category_resolver import CategoryDecision, MocoCategoryResolver
from api.purchase_review_gate import PurchaseReviewGate, ReviewDecision
from api.moco_invoice_client import MocoInvoiceClient
from api.moco_project_resolver import MocoProjectResolver, ProjectMatch
from api.moco_purchase_client import MocoPurchaseClient
from api.moco_client import MocoClient
from api.moco_supplier_matcher import MocoSupplierMatcher
from api.stromproduktion_project_matcher import (
    StromproduktionProjectMatch,
    StromproduktionProjectMatcher,
)
from api.supplier_invoice_ocr_service import (
    CONFIDENCE_THRESHOLD,
    SupplierInvoiceOcrService,
    _build_create_payload,
    _is_notification_subject,
    _is_qr_iban,
    _payment_method_for,
    _prefer_draft_payment_fields,
    _user_id_from_draft,
)
from api.vat_code_resolver import VatCodeResolver, VatDecision

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("batch_ocr_drafts")


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(),
                              value.strip().strip('"').strip("'"))


@dataclass
class Row:
    """One line in the output table.

    `purchase_id` is `None` when no purchase was created (dry-run, OCR
    error, Moco rejection, missing attachment). `supplier` / `amount`
    come from the OCR result when available (None on OCR failure or
    no-attachment skip). `supplier_matched` is True when the supplier
    was uniquely resolved against Moco's company list — rendered with a
    ✓ in the Lieferant column so the operator can spot at a glance which
    rows will land company-less. `already_paid` mirrors the OCR's
    `already_paid_by_card` flag — rendered with a ✓ in the Betrag column
    so card/POS receipts stand out next to open invoices.

    `kommission_raw` is the OCR-extracted Kommission string (None when
    OCR returned nothing). `kommission_status` is the resolver outcome
    (`matched` / `ambiguous` / `no_match` / `empty`) and
    `kommission_candidate_count` carries the candidate count so the
    Kommission column can render `✗ ambiguous (N)`. `result` is a
    short human-readable summary; we truncate it at print time so the
    table stays readable.

    `category` is the pre-formatted KATEGORIE cell (see
    `_format_category_cell`); defaults to "-" for the early-skip rows
    that never reach category resolution.

    `review` is the pre-formatted REVIEW cell (see `_format_review_cell`)
    showing what `PurchaseReviewGate` would decide — AUTO for a purchase
    that skips human review, or HOLD plus the failing conditions. This is
    the pre-flight column: run the script over recent drafts and read it
    BEFORE enabling auto-release, so the historical hit rate is known
    rather than discovered.
    """
    draft_id: int
    purchase_id: int | None
    supplier: str | None
    supplier_matched: bool
    amount: str | None
    already_paid: bool
    kommission_raw: str | None
    kommission_status: str
    kommission_candidate_count: int
    result: str
    category: str = "-"
    review: str = "-"


def _newest_first(drafts: list[dict]) -> list[dict]:
    """Client-side sort by `created_at` descending — newest draft first.

    Drafts don't carry the user-facing `date` field (that's set when the
    real purchase is created); ordering by ingestion timestamp matches the
    "process the freshest arrivals first" intent. Drafts with no
    parseable timestamp sink to the bottom rather than disappearing.
    """
    return sorted(drafts,
                  key=lambda d: d.get("created_at") or "",
                  reverse=True)


def _format_amount(currency: str | None, total: float | None) -> str | None:
    """Compact `CHF 1234.50`-style string for the table, None when missing."""
    if total is None:
        return None
    return f"{currency or 'CHF'} {total:.2f}"


def _step(msg: str) -> None:
    """Indented per-draft live log line. Plain print so it always shows
    (logging is set to WARNING to keep urllib3 / other noise out)."""
    print(f"      {msg}", flush=True)


def _format_vat_tier(vat: VatDecision) -> str:
    """Operator-facing description of which tier produced the vat code.

    Purely a presentation layer over `VatDecision.source` — the chain
    itself is `VatCodeResolver`, the same class the webhook service runs,
    so this preview cannot drift from the real rule. (It used to be a
    hand-copied mirror of the chain; see `specs/SPEC_vat_code_fallback.md`
    D4 for why that had to go.)
    """
    rate = f"{vat.rate:g}%" if vat.rate is not None else "?"
    if vat.vat_code_id is None:
        return "unresolved — Moco will 422"
    return {
        "ocr": f"matched OCR rate {rate}",
        "supplier": "supplier default",
        "account_default": "account default",
        "fallback_zero": f"fallback {rate} (already paid by card)",
        "fallback_standard": f"fallback {rate} (standard rate)",
    }.get(vat.source, vat.source or "?")


def _format_review_cell(decision: ReviewDecision) -> str:
    """Compact REVIEW cell from a ReviewDecision.

    `AUTO` when the purchase would skip human review entirely, otherwise
    `HOLD (reason, reason)` naming the conditions that held it. Calls the
    same `PurchaseReviewGate` the webhook service uses, so this preview
    cannot drift from the real rule — which is the whole point of it.
    """
    if not decision.review_pending:
        return "AUTO"
    reasons = decision.reason_text()
    if len(reasons) > REVIEW_MAX_CHARS:
        reasons = reasons[:REVIEW_MAX_CHARS - 1] + "…"
    return f"HOLD ({reasons})"


def _format_category_cell(decision: CategoryDecision) -> str:
    """Compact KATEGORIE cell from a CategoryDecision.

    `✓ 4500 (project)` on a hit, `✗ 4999 (project)` when the winning
    tier named an account that isn't in the catalog (field OMITTED —
    operator fixes the custom field or picks by hand; also covers the
    missing-4000 edge as `✗ 4000 (default)`), `- paid` for the
    no-default card-receipt omit. Mirrors the ✓/✗ marker style of the
    other columns.
    """
    if decision.category_id is not None:
        return f"✓ {decision.credit_account} ({decision.source})"
    if decision.source == "already_paid":
        return "- paid"
    if decision.credit_account is not None:
        return f"✗ {decision.credit_account} ({decision.source})"
    return "-"


def _format_kommission_log(raw: str | None, match: ProjectMatch) -> str:
    """Build the per-draft Kommission live-log line.

    Mirrors the supplier-lookup log line style: shows the raw OCR'd value,
    then the resolver outcome. `empty` reports that OCR found nothing so
    the operator knows the resolver wasn't given a chance.
    """
    if match.status == "empty":
        return "Kommission: OCR returned no value"
    label = f"Kommission '{raw}'"
    if match.status == "matched":
        proj = match.project or {}
        return (f"{label} → project '{proj.get('name')}' "
                f"(id={proj.get('id')}, {match.tier})")
    if match.status == "ambiguous":
        return (f"{label} → ambiguous ({match.candidate_count} project "
                f"candidates at {match.tier}, leaving project empty)")
    return f"{label} → no project match"


def _format_stromproduktion_match_log(objekt: str | None,
                                      match: StromproduktionProjectMatch) -> str:
    """Build the per-draft Stromproduktion project-resolution live-log line.

    Mirrors `_format_kommission_log`'s style, but additionally names the
    tied candidates on an `ambiguous` outcome (not just a count) — same
    diagnostic depth as the supplier-lookup log's ambiguous branch, so the
    operator can see at a glance which projects tied and why (e.g. a
    generic short token like a bare house number colliding across two
    unrelated projects).
    """
    if match.status == "empty":
        return "Stromproduktion project: OCR returned no Objekt"
    label = f"Stromproduktion project for Objekt {objekt!r}"
    if match.status == "matched":
        proj = match.project or {}
        return (f"{label} → project '{proj.get('name')}' "
                f"(id={proj.get('id')}, {match.tier} tier)")
    if match.status == "ambiguous":
        names = ", ".join(repr(p.get("name")) for p in match.candidates[:4])
        return (f"{label} → ambiguous ({match.candidate_count} candidates "
                f"at {match.tier} tier: {names} — leaving project empty)")
    return (f"{label} → no match (no Stromproduktion project of this "
            "supplier overlaps the Objekt)")


def _process_energy_credit_note(draft: dict, *, pdf_bytes: bytes, invoice,
                                company: dict | None,
                                supplier_matched: bool,
                                service: EnergyCreditNoteService,
                                apply: bool) -> Row:
    """Preview or apply the energy-credit-note branch for one draft.

    Mirrors how the rest of this script reuses `service.process()` / its
    internals for apply mode (see the docstring on `_process_draft`) —
    dry-run peeks at the SAME `ocr`/`matcher` objects the production
    webhook uses (via the service's own collaborators) without writing
    anything to Moco; apply mode calls `service.process()` directly, the
    exact call the webhook makes.

    Reuses the `Row.purchase_id` column for the created invoice's id
    (labeled explicitly in the `result` text) rather than adding a new
    table column for this one row type. Likewise reuses the KOMMISSION
    column for the credit note's OCR'd Objekt + the
    `StromproduktionProjectMatcher` outcome — same `matched`/`ambiguous`/
    `no_match`/`empty` vocabulary as the generic Kommission→project
    resolver, so `_kommission_cell`'s existing rendering (✓/✗ ambiguous
    (N)/plain) applies unchanged.
    """
    draft_id = draft.get("id")
    credit = service._ocr.extract_energy_credit_note(pdf_bytes)
    net_amount = _derive_net_amount(credit.gross_amount, credit.vat_rate)
    amount_cell = _format_amount("CHF", net_amount)
    _step(f"energy credit note: objekt={credit.objekt!r} "
          f"objekt_top_level={credit.objekt_top_level!r} "
          f"gross={credit.gross_amount} vat_rate={credit.vat_rate} "
          f"net={net_amount} confidence={credit.confidence:.0%}")

    # Resolved here (not just inside service.process()) so the diagnostic
    # log line prints in BOTH dry-run and apply mode — pure/cheap
    # (in-memory only), so recomputing it in apply mode alongside the
    # service's own internal call is not wasteful. Uses the service's own
    # `_match_project` (production-section Objekt first, top-level-summary
    # Objekt as fallback — see `specs/SPEC_energy_credit_note.md`, D7) so
    # the preview matches production behavior exactly.
    match, objekt_used = service._match_project(invoice.supplier_name,
                                                 credit, draft_id=draft_id)
    _step(_format_stromproduktion_match_log(objekt_used, match))

    if not apply:
        if match.status == "matched":
            result = ("Dry-run OK (energy credit note → project "
                      f"{match.project.get('name')!r})")
        elif match.status == "ambiguous":
            names = ", ".join(repr(p.get("name"))
                              for p in match.candidates[:4])
            result = (f"Dry-run: energy credit note, ambiguous "
                      f"({match.candidate_count}: {names})")
        else:
            result = f"Dry-run: energy credit note, project {match.status}"
        return Row(draft_id, None, invoice.supplier_name, supplier_matched,
                   amount_cell, False, objekt_used, match.status,
                   match.candidate_count, result)

    outcome = service.process(pdf_bytes=pdf_bytes, invoice=invoice,
                              company=company, draft_id=draft_id, body=draft)
    if outcome.get("skipped"):
        _step(f"energy credit note kept draft: {outcome['skipped']}")
        return Row(draft_id, None, invoice.supplier_name, supplier_matched,
                   amount_cell, False, objekt_used, match.status,
                   match.candidate_count, f"Skipped: {outcome['skipped']}")
    _step(f"created invoice id={outcome.get('invoice_id')} "
          f"expense id={outcome.get('expense_id')}")
    return Row(draft_id, outcome.get("invoice_id"), invoice.supplier_name,
               supplier_matched, amount_cell, False, objekt_used,
               match.status, match.candidate_count,
               "Created energy-credit-note invoice (status=created, "
               f"expense={outcome.get('expense_id')})")


def _process_draft(draft: dict, *,
                   moco: MocoClient,
                   purchases: MocoPurchaseClient,
                   ocr: AnthropicOcrClient,
                   service: SupplierInvoiceOcrService,
                   resolver: MocoProjectResolver,
                   category_resolver: MocoCategoryResolver,
                   supplier_matcher: MocoSupplierMatcher,
                   energy_credit_note_service: EnergyCreditNoteService | None,
                   apply: bool,
                   idx: int,
                   total: int) -> Row:
    """Run one draft through the pipeline with live step-by-step logs.

    Inlining the pipeline (rather than calling `service.process()` for
    apply mode) lets us emit each operator-facing log line at exactly the
    moment it's relevant AND avoids a double-OCR cost. For apply mode the
    post-create steps (comments, project assign, payment registration,
    draft delete) are delegated to the service's own methods so the live
    behavior matches the webhook exactly — comment text / delete fallback
    / soft-failure semantics stay in one place.

    **Maintenance hazard**: because the pipeline is inlined, any NEW
    post-create step added to `service.process()` must be mirrored in the
    apply block below or this script silently diverges from production.
    That already bit once — `_register_payment` was added to the service
    and not here, so `--apply` created purchases without settling
    already-paid receipts.
    """
    draft_id = draft.get("id")
    file_url = draft.get("file_url")
    print(f"  [{idx}/{total}] draft {draft_id}", flush=True)
    # Kommission fields default to the "OCR didn't run" state. Populated
    # post-OCR; the early-skip rows below keep these defaults so the table
    # still renders a Kommission cell ("-") in every row.
    kommission_raw: str | None = None
    kommission_status: str = "empty"
    kommission_candidate_count: int = 0
    if not file_url:
        # Same rule as the webhook flow: attachment-less drafts whose
        # subject marks a notification email ("Sicherheitshinweis" /
        # "Zustellungshinweis") get deleted rather than reported as a
        # broken import. Dry-run only announces the would-be delete.
        if _is_notification_subject(draft.get("title")):
            title = draft.get("title")
            if apply:
                _step(f"notification email ({title!r}) — deleting draft")
                service._delete_notification_draft(draft_id)
                result = "Skipped: notification email (draft deleted)"
            else:
                _step(f"notification email ({title!r}) — would delete "
                      "draft (dry-run)")
                result = "Skipped: notification email (would delete draft)"
            return Row(draft_id, None, None, False, None, False,
                       kommission_raw, kommission_status,
                       kommission_candidate_count, result)
        _step("Skipped: no file_url on draft")
        return Row(draft_id, None, None, False, None, False,
                   kommission_raw, kommission_status,
                   kommission_candidate_count, "Skipped: no_file_url")

    # --- download + OCR ----------------------------------------------------
    try:
        pdf_bytes = moco.download_file(file_url)
    except Exception as e:
        _step(f"PDF download failed: {e}")
        return Row(draft_id, None, None, False, None, False,
                   kommission_raw, kommission_status,
                   kommission_candidate_count, f"PDF download failed: {e}")

    t0 = time.monotonic()
    try:
        invoice = ocr.extract(pdf_bytes)
    except AnthropicOcrError as e:
        ocr_secs = time.monotonic() - t0
        _step(f"PDF {len(pdf_bytes) / 1024:.0f} KB, OCR failed after "
              f"{ocr_secs:.1f}s ({e})")
        status = e.status_code if e.status_code is not None else "parse"
        return Row(draft_id, None, None, False, None, False,
                   kommission_raw, kommission_status,
                   kommission_candidate_count, f"OCR error {status}: {e}")
    ocr_secs = time.monotonic() - t0
    _step(f"PDF {len(pdf_bytes) / 1024:.0f} KB, OCR {ocr_secs:.1f}s")

    # --- confidence + Gutschrift -------------------------------------------
    confidence_flag = " ⚠ low" if invoice.confidence < CONFIDENCE_THRESHOLD else ""
    kind = ("Gutschrift ⚠ Vorzeichen prüfen" if invoice.is_credit_note
            else "Rechnung")
    _step(f"confidence {invoice.confidence:.0%}{confidence_flag}, {kind}")

    # The service overrides OCR iban/qr_reference with whatever Moco's
    # email-import parsed off the Zahlteil. Apply that same override here
    # so the payment-method log line reflects the iban that will actually
    # be POSTed.
    invoice = _prefer_draft_payment_fields(invoice, draft)

    # --- supplier lookup ---------------------------------------------------
    # Same three-tier matcher as the webhook flow (exact → substring →
    # normalized token-set), built once per run against the full supplier
    # list. The live log names the winning tier so the operator can see
    # how "close" the OCR'd name was.
    company_id: int | None = None
    matched = False
    if invoice.supplier_name:
        m = supplier_matcher.match(invoice.supplier_name)
        if m.status == "matched":
            company_id = m.company.get("id")
            matched = True
            _step(f"supplier '{invoice.supplier_name}' → id={company_id} "
                  f"'{m.company.get('name')}' ({m.tier} tier)")
        elif m.status == "ambiguous":
            names = ", ".join(repr(c.get("name")) for c in m.candidates[:4])
            _step(f"supplier '{invoice.supplier_name}' → ambiguous "
                  f"({m.candidate_count} hits at {m.tier} tier: {names} — "
                  "leaving company empty)")
        else:
            _step(f"supplier '{invoice.supplier_name}' → no match")
    else:
        _step("supplier: OCR returned no supplier_name")

    # Full company record, fetched once — feeds the vat chain (supplier
    # default vat code) and the category chain (supplier Aufwandkonto).
    # The list shape behind the matcher carries neither field.
    full_company: dict | None = None
    if company_id is not None:
        try:
            full_company = moco.get_company(company_id)
        except Exception as e:
            log.warning("get_company failed id=%s: %s", company_id, e)

    # --- energy credit note detection --------------------------------------
    # EVU production credit notes (see energy_credit_note_service.py)
    # short-circuit to their own expense+invoice flow — mirrors the
    # production webhook's dispatch point exactly (right after the
    # supplier company is resolved, before Kommission/VAT/category
    # resolution for the generic purchase path). Three independent
    # signals, any one sufficient — see the identical check in
    # supplier_invoice_ocr_service.py.
    is_energy_credit = energy_credit_note_service is not None and (
        is_energy_credit_note(invoice, full_company)
        or (invoice.is_credit_note
            and energy_credit_note_service.is_evu_tagged_customer(
                invoice.supplier_name))
        or (invoice.is_credit_note
            and energy_credit_note_service.has_matching_project(
                invoice.supplier_name)))
    if is_energy_credit:
        return _process_energy_credit_note(
            draft, pdf_bytes=pdf_bytes, invoice=invoice, company=full_company,
            supplier_matched=matched, service=energy_credit_note_service,
            apply=apply)

    # --- Kommission → Moco project ----------------------------------------
    # Resolver-only (Stage 1): we surface the would-be project but DON'T
    # wire it into the create payload yet. Stage 2 will pass project_id to
    # `_build_create_payload` once we've confirmed the matcher hit-rate
    # over a real batch.
    kommission_raw = invoice.commission
    kommission_match = resolver.resolve(kommission_raw)
    kommission_status = kommission_match.status
    kommission_candidate_count = kommission_match.candidate_count
    _step(_format_kommission_log(kommission_raw, kommission_match))

    # --- vat-code resolution ----------------------------------------------
    try:
        vat_codes = purchases.list_vat_codes()
    except Exception as e:
        vat_codes = []
        log.warning("list_vat_codes failed: %s", e)
    vat = VatCodeResolver(vat_codes).resolve(invoice, full_company)
    _step(f"vat_code {vat.vat_code_id if vat.vat_code_id else '?'} "
          f"({_format_vat_tier(vat)})")

    # --- payment method + IBAN tail ---------------------------------------
    method = _payment_method_for(invoice)
    if method == "credit_card":
        # Bill already settled — the IBAN line would be misleading here
        # since the service suppresses iban/reference/due_date on the
        # credit_card branch. Surface the marker prominently instead.
        _step(f"payment: {method}  💳 bereits bezahlt "
              "(Karte / Terminal — IBAN & due_date suppressed)")
    else:
        iban_tail = invoice.iban[-4:] if invoice.iban else "----"
        qr_note = "  (QR-IBAN)" if _is_qr_iban(invoice.iban) else ""
        _step(f"payment: {method}  IBAN …{iban_tail}{qr_note}")

    # --- category resolution ----------------------------------------------
    # Mirrors the webhook flow: project's Aufwandkonto override first,
    # then the supplier's, then 4000 fallback — OMIT on any override miss
    # or on already-paid without an override. Resolved in dry-run too so
    # the inline log and the KATEGORIE column show what an --apply run
    # would book.
    matched_project = (kommission_match.project
                       if kommission_match.status == "matched"
                       else None)
    category_decision = category_resolver.resolve(
        already_paid_by_card=invoice.already_paid_by_card,
        project=matched_project,
        supplier=full_company)
    _step(f"category_id={category_decision.category_id} "
          f"({category_decision.reason})")
    category_cell = _format_category_cell(category_decision)

    # --- review gate ------------------------------------------------------
    # Same collaborator the webhook service uses, on the same inputs, so
    # the REVIEW column is the real decision rather than a re-implementation.
    review_decision = PurchaseReviewGate().evaluate(
        invoice=invoice, company_id=company_id,
        category=category_decision, project_match=kommission_match,
        vat=vat)
    _step(f"review: {'HOLD' if review_decision.review_pending else 'AUTO'}"
          f"{' — ' + review_decision.reason_text() if review_decision.reasons else ''}")
    review_cell = _format_review_cell(review_decision)

    amount_cell = _format_amount(invoice.currency, invoice.total_amount)
    paid = invoice.already_paid_by_card

    if not apply:
        return Row(draft_id, None, invoice.supplier_name, matched, amount_cell,
                   paid, kommission_raw, kommission_status,
                   kommission_candidate_count,
                   f"Dry-run OK (would create, confidence={invoice.confidence:.0%})",
                   category=category_cell, review=review_cell)

    # --- apply: create + comments + delete draft --------------------------
    payload = _build_create_payload(
        invoice, pdf_bytes, vat_code_id=vat.vat_code_id,
        company_id=company_id, draft_id=draft_id,
        user_id=_user_id_from_draft(draft),
        category_id=category_decision.category_id,
        tags=review_decision.tags)
    try:
        created = purchases.create_purchase(payload)
    except urlerror.HTTPError as e:
        if not (400 <= e.code < 500):
            _step(f"POST failed: HTTP {e.code} {e.reason}")
            return Row(draft_id, None, invoice.supplier_name, matched,
                       amount_cell, paid, kommission_raw, kommission_status,
                       kommission_candidate_count,
                       f"HTTP {e.code} {e.reason}", category=category_cell,
                       review=review_cell)
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        err_body = err_body.replace("\n", " ")
        _step(f"Moco rejected: {e.code} {err_body}")
        return Row(draft_id, None, invoice.supplier_name, matched, amount_cell,
                   paid, kommission_raw, kommission_status,
                   kommission_candidate_count, f"Moco {e.code}: {err_body}",
                   category=category_cell, review=review_cell)

    new_purchase_id = created.get("id")
    if new_purchase_id:
        # Delegate the post-create wrap-up to the service — same comment
        # body, same soft-failure semantics, same draft-delete idempotency
        # as the webhook flow. Using the service's existing methods (rather
        # than re-implementing) keeps batch and webhook in lockstep.
        service._post_summary_comments(new_purchase_id, invoice,
                                        draft_id, draft,
                                        review=review_decision,
                                        company=full_company,
                                        project_match=kommission_match,
                                        category=category_decision,
                                        vat=vat)
        assign_warnings = service._assign_resolved_project(
            created, kommission_match)
        if assign_warnings:
            for w in assign_warnings:
                _step(f"assign warning: {w}")
        # Settle already-paid receipts, same as the webhook flow. Must stay
        # in this list: every post-create step `process()` grows has to be
        # mirrored here or apply mode silently diverges from production.
        registered, payment_warning = service._register_payment(created,
                                                                invoice)
        if registered:
            _step("payment registered (already paid)")
        elif payment_warning:
            _step(f"payment warning: {payment_warning}")
        service._delete_draft_after_create(draft_id, new_purchase_id)
        _step(f"created purchase id={new_purchase_id}")
    return Row(draft_id, new_purchase_id, invoice.supplier_name, matched,
               amount_cell, paid, kommission_raw, kommission_status,
               kommission_candidate_count,
               f"Created (confidence={invoice.confidence:.0%})",
               category=category_cell, review=review_cell)


SUPPLIER_MAX_CHARS = 20  # ample for typical Swiss supplier names, fits 80-col
KOMMISSION_MAX_CHARS = 20  # raw OCR'd value can run long with "BV-XYZ" prefixes
REVIEW_MAX_CHARS = 15     # HOLD can name up to four failing conditions

# Upper bound on how many drafts we fetch from Moco before sorting + applying
# --max N. Fixed (not configurable) because 100 covers the realistic backlog
# and keeps a single pagination request enough.
DRAFT_FETCH_CAP = 100


def _print_table(rows: list[Row], *, result_width: int = 60) -> None:
    """Aligned eight-column table on stdout.

    Columns: DRAFT ID | PURCHASE ID | LIEFERANT | BETRAG | KOMMISSION |
    KATEGORIE | REVIEW | RESULT.
    Lieferant gets a leading ✓ when the supplier was uniquely matched in
    Moco's company list; Betrag gets a leading ✓ on already-paid bills;
    Kommission gets a leading ✓ when the OCR'd value resolved to exactly
    one Moco project, or a trailing `✗ ambiguous (N)` when more than one
    matched. For energy-credit-note rows this column is repurposed to show
    the credit note's OCR'd Objekt + `StromproduktionProjectMatcher`
    outcome instead (see `_process_energy_credit_note`) — same status
    vocabulary, so the same rendering applies unchanged. Kategorie carries
    the pre-formatted `_format_category_cell`
    outcome (✓ account (source) / ✗ unmapped account / `- paid`). Review
    carries `_format_review_cell` — AUTO when the purchase would skip
    human review, HOLD (reasons) otherwise.

    Truncates Lieferant / Kommission / Result so long values don't blow
    up the layout. Full text stays in the underlying Row objects.
    """
    headers = ("DRAFT ID", "PURCHASE ID", "LIEFERANT", "BETRAG",
               "KOMMISSION", "KATEGORIE", "REVIEW", "RESULT")

    def _supplier_cell(r: Row) -> str:
        if not r.supplier:
            return "-"
        mark = "✓ " if r.supplier_matched else "  "
        text = r.supplier
        if len(text) > SUPPLIER_MAX_CHARS:
            text = text[:SUPPLIER_MAX_CHARS - 1] + "…"
        return mark + text

    def _amount_cell(r: Row) -> str:
        # ✓ prefix mirrors the Lieferant column: marker = condition met,
        # blank = condition absent. For Betrag the condition is "OCR
        # detected this bill as already paid via card / POS" — visually
        # flags receipts that won't be transferred from any bank account.
        if not r.amount:
            return "-"
        mark = "✓ " if r.already_paid else "  "
        return mark + r.amount

    def _kommission_cell(r: Row) -> str:
        # Status discriminates: "empty" → "-" (OCR found nothing);
        # "matched" → "✓ <raw>"; "no_match" → "  <raw>" (still show what
        # OCR'd so the operator can see what didn't match); "ambiguous"
        # → "<raw> ✗ ambiguous (N)" with the candidate count.
        if r.kommission_status == "empty" or not r.kommission_raw:
            return "-"
        text = r.kommission_raw
        if len(text) > KOMMISSION_MAX_CHARS:
            text = text[:KOMMISSION_MAX_CHARS - 1] + "…"
        if r.kommission_status == "matched":
            return "✓ " + text
        if r.kommission_status == "ambiguous":
            return f"  {text} ✗ ambiguous ({r.kommission_candidate_count})"
        return "  " + text

    def _trim(s: str) -> str:
        return s if len(s) <= result_width else s[:result_width - 1] + "…"

    supplier_cells = [_supplier_cell(r) for r in rows]
    amount_cells = [_amount_cell(r) for r in rows]
    kommission_cells = [_kommission_cell(r) for r in rows]
    category_cells = [r.category for r in rows]
    review_cells = [r.review for r in rows]

    draft_w = max(len(headers[0]),
                  max((len(str(r.draft_id)) for r in rows), default=0))
    purchase_w = max(len(headers[1]),
                     max((len(str(r.purchase_id) if r.purchase_id else "-")
                          for r in rows), default=0))
    supplier_w = max(len(headers[2]),
                     max((len(c) for c in supplier_cells), default=0))
    amount_w = max(len(headers[3]),
                   max((len(c) for c in amount_cells), default=0))
    kommission_w = max(len(headers[4]),
                       max((len(c) for c in kommission_cells), default=0))
    category_w = max(len(headers[5]),
                     max((len(c) for c in category_cells), default=0))
    review_w = max(len(headers[6]),
                   max((len(c) for c in review_cells), default=0))

    print()
    print(f"{headers[0]:<{draft_w}}  "
          f"{headers[1]:<{purchase_w}}  "
          f"{headers[2]:<{supplier_w}}  "
          f"{headers[3]:<{amount_w}}  "
          f"{headers[4]:<{kommission_w}}  "
          f"{headers[5]:<{category_w}}  "
          f"{headers[6]:<{review_w}}  "
          f"{headers[7]}")
    print(f"{'-' * draft_w}  {'-' * purchase_w}  "
          f"{'-' * supplier_w}  {'-' * amount_w}  "
          f"{'-' * kommission_w}  {'-' * category_w}  "
          f"{'-' * review_w}  {'-' * len(headers[7])}")
    for r, supplier, amount, kommission, category, review in zip(
            rows, supplier_cells, amount_cells, kommission_cells,
            category_cells, review_cells):
        purchase = str(r.purchase_id) if r.purchase_id else "-"
        print(f"{r.draft_id:<{draft_w}}  "
              f"{purchase:<{purchase_w}}  "
              f"{supplier:<{supplier_w}}  "
              f"{amount:<{amount_w}}  "
              f"{kommission:<{kommission_w}}  "
              f"{category:<{category_w}}  "
              f"{review:<{review_w}}  "
              f"{_trim(r.result)}")

    created = sum(1 for r in rows if r.purchase_id is not None)
    failed = sum(1 for r in rows if r.purchase_id is None
                 and not r.result.startswith(("Dry-run", "Skipped")))
    skipped = sum(1 for r in rows if r.result.startswith("Skipped"))
    dry = sum(1 for r in rows if r.result.startswith("Dry-run"))
    matched = sum(1 for r in rows if r.supplier_matched)
    paid = sum(1 for r in rows if r.already_paid)
    kommission_matched = sum(1 for r in rows
                             if r.kommission_status == "matched")
    auto_released = sum(1 for r in rows if r.review == "AUTO")
    print()
    print(f"Total: {len(rows)}   created: {created}   "
          f"dry-run: {dry}   skipped: {skipped}   failed: {failed}   "
          f"supplier-matched: {matched}   already-paid: {paid}   "
          f"kommission-matched: {kommission_matched}   "
          f"auto-release: {auto_released}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max", dest="max_drafts", type=int, default=10,
                        help="Maximum number of drafts to process "
                             "(newest first). Default: 10.")
    parser.add_argument("--draft-id", type=int, default=None,
                        help="Process exactly this draft (bypasses the "
                             "listing) — the way to dry-run one specific "
                             "draft through the full webhook dispatch "
                             "(generic purchase / Gutschrift / energy "
                             "credit note routing all included).")
    parser.add_argument("--apply", action="store_true",
                        help="Actually POST /purchases for each draft "
                             "(default: dry-run, OCR only, no Moco writes).")
    parser.add_argument("--model", default=None,
                        help="Override the Claude model")
    parser.add_argument("--env-file", type=Path,
                        default=Path(__file__).resolve().parent.parent
                        / ".env.local")
    args = parser.parse_args()

    _load_dotenv(args.env_file)

    subdomain = os.environ.get("MOCO_SUBDOMAIN")
    moco_key = os.environ.get("MOCO_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    missing = [k for k, v in {
        "MOCO_SUBDOMAIN": subdomain,
        "MOCO_API_KEY": moco_key,
        "ANTHROPIC_API_KEY": anthropic_key,
    }.items() if not v]
    if missing:
        print(f"Missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 2

    moco = MocoClient(subdomain=subdomain, api_key=moco_key)
    purchases = MocoPurchaseClient(subdomain=subdomain, api_key=moco_key)
    ocr = AnthropicOcrClient(api_key=anthropic_key, model=args.model)

    if args.draft_id is not None:
        print(f"Fetching draft {args.draft_id} …")
        try:
            drafts = [purchases.get_purchase_draft(args.draft_id)]
        except Exception as e:
            print(f"Failed to fetch draft {args.draft_id}: {e}",
                  file=sys.stderr)
            return 3
    else:
        # Always fetch the full draft pool (up to 100) so the --max N cap
        # is applied to the freshest N AFTER newest-first sorting. Limiting
        # the API call directly would just take whatever order Moco
        # returns the first N in, which isn't guaranteed to be newest-first.
        print(f"Listing drafts from "
              f"https://{subdomain}.mocoapp.com/api/v1/purchases/drafts …")
        try:
            drafts = purchases.list_purchase_drafts(limit=DRAFT_FETCH_CAP)
        except Exception as e:
            print(f"Failed to list drafts: {e}", file=sys.stderr)
            return 3
        drafts = _newest_first(drafts)[:args.max_drafts]
    print(f"Got {len(drafts)} draft(s). "
          f"Mode: {'APPLY (real writes)' if args.apply else 'DRY-RUN'}")

    # Build the Kommission → Moco project resolver once per run. The
    # resolver index is reused across all drafts; a new project added in
    # Moco mid-run won't be seen until the next invocation, which is
    # fine for a short-lived batch script.
    print("Loading active Moco projects for Kommission resolution …")
    try:
        all_projects = moco.list_projects()
    except Exception as e:
        # A failed project list shouldn't kill the batch — fall back to an
        # empty resolver so every draft reports "no project match" but the
        # OCR pipeline still runs and produces the rest of the table.
        print(f"  WARN: list_projects failed ({e}); resolving against an "
              "empty index.", file=sys.stderr)
        all_projects = []
    resolver = MocoProjectResolver(all_projects)
    print(f"  {len(all_projects)} project(s) returned, "
          f"{resolver.indexed_count()} indexed by Kommission.")

    # Categories drive the per-item `category_id` (Buchhaltungs-Konto)
    # resolution. Same per-run fetch + graceful-empty-on-failure pattern
    # as projects above; an empty catalog means every category lookup
    # OMITs the field and the operator picks during review.
    print("Loading Moco purchase categories …")
    try:
        all_categories = purchases.list_categories()
    except Exception as e:
        print(f"  WARN: list_categories failed ({e}); category resolution "
              "disabled.", file=sys.stderr)
        all_categories = []
    category_resolver = MocoCategoryResolver(all_categories)
    print(f"  {len(all_categories)} category(s) returned, "
          f"{category_resolver.indexed_count()} indexed by credit_account.")

    # Supplier list feeds the three-tier name matcher (exact → substring
    # → normalized). Same per-run fetch + graceful-empty-on-failure
    # pattern as projects/categories; an empty matcher means every draft
    # reports "no match" and lands company-less.
    print("Loading Moco suppliers for company matching …")
    try:
        all_suppliers = moco.list_suppliers()
    except Exception as e:
        print(f"  WARN: list_suppliers failed ({e}); matching against an "
              "empty supplier list.", file=sys.stderr)
        all_suppliers = []
    supplier_matcher = MocoSupplierMatcher(all_suppliers)
    print(f"  {len(all_suppliers)} supplier(s) returned, "
          f"{supplier_matcher.indexed_count()} matchable by name.")

    # Customer-type company list feeds the third energy-credit-note
    # detection signal (`is_evu_tagged_customer`) — some EVUs only carry
    # the EVU tag on their type=customer record (confirmed live: CKW,
    # BKW). Same per-run fetch + graceful-empty-on-failure pattern.
    print("Loading Moco customers for EVU customer-tag detection …")
    try:
        all_customers = moco.list_customers()
    except Exception as e:
        print(f"  WARN: list_customers failed ({e}); EVU customer-tag "
              "detection signal disabled.", file=sys.stderr)
        all_customers = []
    customer_matcher = MocoSupplierMatcher(all_customers)
    print(f"  {len(all_customers)} customer(s) returned, "
          f"{customer_matcher.indexed_count()} matchable by name.")

    # Telegram intentionally NOT wired in. Batch runs touch dozens of drafts
    # at a time and would spam the chat with one alert per row; the table is
    # the audit surface here. Production webhook traffic still notifies as
    # usual.
    moco_invoices = MocoInvoiceClient(subdomain=subdomain, api_key=moco_key)
    energy_credit_note_service = EnergyCreditNoteService(
        moco=moco, moco_invoices=moco_invoices, purchase_client=purchases,
        ocr=ocr, matcher=StromproduktionProjectMatcher(all_projects),
        customer_matcher=customer_matcher,
        subdomain=subdomain, telegram=None)
    service = SupplierInvoiceOcrService(
        moco=moco, purchase_client=purchases, ocr=ocr,
        subdomain=subdomain, telegram=None,
        project_resolver=resolver,
        category_resolver=category_resolver,
        energy_credit_note=energy_credit_note_service)

    rows: list[Row] = []
    for i, draft in enumerate(drafts, start=1):
        rows.append(_process_draft(
            draft, moco=moco, purchases=purchases, ocr=ocr,
            service=service, resolver=resolver,
            category_resolver=category_resolver,
            supplier_matcher=supplier_matcher,
            energy_credit_note_service=energy_credit_note_service,
            apply=args.apply, idx=i, total=len(drafts)))

    _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
