#!/usr/bin/env python3
"""Batch OCR validation across all Moco draft purchases.

Lists `GET /purchases/drafts` (newest first), runs the same in-process
pipeline as scripts/test_ocr_moco.py against each draft, and prints a
summary table with one row per draft. Per-draft live logs surface PDF
size, OCR latency, supplier-lookup outcome, VAT-code resolution tier,
chosen payment method, and confidence / Gutschrift flags as each draft
is processed — so the operator can spot anomalies before the table
even prints.

Default mode is dry-run: OCR runs against each draft's PDF, but NO Moco
purchase is created and NO draft is deleted. `--apply` switches to
production behavior — POST /purchases + comments + draft-delete for
every draft, exactly like the webhook handler.

Usage (from the repo root):
    vercel env pull .env.local
    .venv/bin/python scripts/batch_ocr_drafts.py --max 5          # dry-run
    .venv/bin/python scripts/batch_ocr_drafts.py --max 5 --apply  # real writes

Required env (same as test_ocr_moco.py):
    MOCO_SOURCE_ACCOUNT_URL    source subdomain (e.g. "solar")
    MOCO_SOURCE_API_KEY        token for the source Moco account
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
from api.moco_purchase_client import MocoPurchaseClient
from api.source_moco_client import SourceMocoClient
from api.supplier_invoice_ocr_service import (
    CONFIDENCE_THRESHOLD,
    SupplierInvoiceOcrService,
    _account_default_vat_code,
    _build_create_payload,
    _find_vat_code_by_rate,
    _is_qr_iban,
    _payment_method_for,
    _prefer_draft_payment_fields,
    _supplier_default_vat_code_id,
)

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
    so card/POS receipts stand out next to open invoices. `result` is a
    short human-readable summary; we truncate it at print time so the
    table stays readable.
    """
    draft_id: int
    purchase_id: int | None
    supplier: str | None
    supplier_matched: bool
    amount: str | None
    already_paid: bool
    result: str


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


def _resolve_vat_code_with_tier(invoice, company_id: int | None,
                                vat_codes: list[dict],
                                source_moco: SourceMocoClient
                                ) -> tuple[int | None, str]:
    """Mirror of `SupplierInvoiceOcrService._resolve_vat_code_id` that
    additionally reports which tier of the 4-step chain won.

    Why a copy rather than a refactor on the service: the production
    service returns only the id (the tier is purely operator-facing); the
    refactor would touch every call site. Keeping the chain mirrored here
    is ~20 lines and easy to diff against the service when it changes.
    """
    if invoice.vat_rate is not None:
        match = _find_vat_code_by_rate(vat_codes, invoice.vat_rate)
        if match is not None:
            return match.get("id"), f"matched OCR rate {invoice.vat_rate*100:.1f}%"
    if company_id is not None:
        try:
            company = source_moco.get_company(company_id)
        except Exception:
            company = None
        if company:
            sid = _supplier_default_vat_code_id(company, vat_codes)
            if sid is not None:
                return sid, "supplier default"
    account_default = _account_default_vat_code(vat_codes)
    if account_default is not None:
        return account_default.get("id"), "account default"
    return None, "unresolved — Moco will 422"


def _process_draft(draft: dict, *,
                   source_moco: SourceMocoClient,
                   purchases: MocoPurchaseClient,
                   ocr: AnthropicOcrClient,
                   service: SupplierInvoiceOcrService,
                   apply: bool,
                   idx: int,
                   total: int) -> Row:
    """Run one draft through the pipeline with live step-by-step logs.

    Inlining the pipeline (rather than calling `service.process()` for
    apply mode) lets us emit each operator-facing log line at exactly the
    moment it's relevant AND avoids a double-OCR cost. For apply mode the
    post-create steps (comments, draft delete) are delegated to the
    service's own methods so the live behavior matches the webhook
    exactly — comment text / delete fallback / soft-failure semantics
    stay in one place.
    """
    draft_id = draft.get("id")
    file_url = draft.get("file_url")
    print(f"  [{idx}/{total}] draft {draft_id}", flush=True)
    if not file_url:
        _step("Skipped: no file_url on draft")
        return Row(draft_id, None, None, False, None, False,
                   "Skipped: no_file_url")

    # --- download + OCR ----------------------------------------------------
    try:
        pdf_bytes = source_moco.download_file(file_url)
    except Exception as e:
        _step(f"PDF download failed: {e}")
        return Row(draft_id, None, None, False, None, False,
                   f"PDF download failed: {e}")

    t0 = time.monotonic()
    try:
        invoice = ocr.extract(pdf_bytes)
    except AnthropicOcrError as e:
        ocr_secs = time.monotonic() - t0
        _step(f"PDF {len(pdf_bytes) / 1024:.0f} KB, OCR failed after "
              f"{ocr_secs:.1f}s ({e})")
        status = e.status_code if e.status_code is not None else "parse"
        return Row(draft_id, None, None, False, None, False,
                   f"OCR error {status}: {e}")
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
    company_id: int | None = None
    matched = False
    if invoice.supplier_name:
        try:
            matches = source_moco.search_suppliers(invoice.supplier_name)
        except Exception as e:
            matches = []
            _step(f"supplier '{invoice.supplier_name}' → lookup failed: {e}")
        else:
            if len(matches) == 1:
                company_id = matches[0].get("id")
                matched = True
                _step(f"supplier '{invoice.supplier_name}' → id={company_id} "
                      f"(matched)")
            elif matches:
                _step(f"supplier '{invoice.supplier_name}' → ambiguous "
                      f"({len(matches)} hits, leaving company empty)")
            else:
                _step(f"supplier '{invoice.supplier_name}' → no match")
    else:
        _step("supplier: OCR returned no supplier_name")

    # --- vat-code resolution ----------------------------------------------
    try:
        vat_codes = purchases.list_vat_codes()
    except Exception as e:
        vat_codes = []
        log.warning("list_vat_codes failed: %s", e)
    vat_code_id, vat_tier = _resolve_vat_code_with_tier(
        invoice, company_id, vat_codes, source_moco)
    _step(f"vat_code {vat_code_id if vat_code_id else '?'} ({vat_tier})")

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

    amount_cell = _format_amount(invoice.currency, invoice.total_amount)
    paid = invoice.already_paid_by_card

    if not apply:
        return Row(draft_id, None, invoice.supplier_name, matched, amount_cell,
                   paid,
                   f"Dry-run OK (would create, confidence={invoice.confidence:.0%})")

    # --- apply: create + comments + delete draft --------------------------
    payload = _build_create_payload(
        invoice, pdf_bytes, vat_code_id=vat_code_id,
        company_id=company_id, draft_id=draft_id)
    try:
        created = purchases.create_purchase(payload)
    except urlerror.HTTPError as e:
        if not (400 <= e.code < 500):
            _step(f"POST failed: HTTP {e.code} {e.reason}")
            return Row(draft_id, None, invoice.supplier_name, matched,
                       amount_cell, paid, f"HTTP {e.code} {e.reason}")
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        err_body = err_body.replace("\n", " ")
        _step(f"Moco rejected: {e.code} {err_body}")
        return Row(draft_id, None, invoice.supplier_name, matched, amount_cell,
                   paid, f"Moco {e.code}: {err_body}")

    new_purchase_id = created.get("id")
    if new_purchase_id:
        # Delegate the post-create wrap-up to the service — same comment
        # body, same soft-failure semantics, same draft-delete idempotency
        # as the webhook flow. Using the service's existing methods (rather
        # than re-implementing) keeps batch and webhook in lockstep.
        service._post_summary_comments(new_purchase_id, invoice,
                                        draft_id, draft)
        service._delete_draft_after_create(draft_id, new_purchase_id)
        _step(f"created purchase id={new_purchase_id}")
    return Row(draft_id, new_purchase_id, invoice.supplier_name, matched,
               amount_cell, paid,
               f"Created (confidence={invoice.confidence:.0%})")


SUPPLIER_MAX_CHARS = 32   # ample for typical Swiss supplier names, fits 80-col

# Upper bound on how many drafts we fetch from Moco before sorting + applying
# --max N. Fixed (not configurable) because 100 covers the realistic backlog
# and keeps a single pagination request enough.
DRAFT_FETCH_CAP = 100


def _print_table(rows: list[Row], *, result_width: int = 60) -> None:
    """Aligned five-column table on stdout.

    Columns: DRAFT ID | PURCHASE ID | LIEFERANT | BETRAG | RESULT.
    Lieferant gets a leading ✓ when the supplier was uniquely matched in
    Moco's company list, so the operator sees at a glance which rows
    will land company-less.

    Truncates Lieferant + Result so a long supplier name or a verbose
    Moco error body don't blow up the layout. Full text stays in the
    underlying Row objects.
    """
    headers = ("DRAFT ID", "PURCHASE ID", "LIEFERANT", "BETRAG", "RESULT")

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

    def _trim(s: str) -> str:
        return s if len(s) <= result_width else s[:result_width - 1] + "…"

    supplier_cells = [_supplier_cell(r) for r in rows]
    amount_cells = [_amount_cell(r) for r in rows]

    draft_w = max(len(headers[0]),
                  max((len(str(r.draft_id)) for r in rows), default=0))
    purchase_w = max(len(headers[1]),
                     max((len(str(r.purchase_id) if r.purchase_id else "-")
                          for r in rows), default=0))
    supplier_w = max(len(headers[2]),
                     max((len(c) for c in supplier_cells), default=0))
    amount_w = max(len(headers[3]),
                   max((len(c) for c in amount_cells), default=0))

    print()
    print(f"{headers[0]:<{draft_w}}  "
          f"{headers[1]:<{purchase_w}}  "
          f"{headers[2]:<{supplier_w}}  "
          f"{headers[3]:<{amount_w}}  "
          f"{headers[4]}")
    print(f"{'-' * draft_w}  {'-' * purchase_w}  "
          f"{'-' * supplier_w}  {'-' * amount_w}  "
          f"{'-' * len(headers[4])}")
    for r, supplier, amount in zip(rows, supplier_cells, amount_cells):
        purchase = str(r.purchase_id) if r.purchase_id else "-"
        print(f"{r.draft_id:<{draft_w}}  "
              f"{purchase:<{purchase_w}}  "
              f"{supplier:<{supplier_w}}  "
              f"{amount:<{amount_w}}  "
              f"{_trim(r.result)}")

    created = sum(1 for r in rows if r.purchase_id is not None)
    failed = sum(1 for r in rows if r.purchase_id is None
                 and not r.result.startswith(("Dry-run", "Skipped")))
    skipped = sum(1 for r in rows if r.result.startswith("Skipped"))
    dry = sum(1 for r in rows if r.result.startswith("Dry-run"))
    matched = sum(1 for r in rows if r.supplier_matched)
    paid = sum(1 for r in rows if r.already_paid)
    print()
    print(f"Total: {len(rows)}   created: {created}   "
          f"dry-run: {dry}   skipped: {skipped}   failed: {failed}   "
          f"supplier-matched: {matched}   already-paid: {paid}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max", dest="max_drafts", type=int, default=10,
                        help="Maximum number of drafts to process "
                             "(newest first). Default: 10.")
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

    subdomain = os.environ.get("MOCO_SOURCE_ACCOUNT_URL")
    moco_key = os.environ.get("MOCO_SOURCE_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    missing = [k for k, v in {
        "MOCO_SOURCE_ACCOUNT_URL": subdomain,
        "MOCO_SOURCE_API_KEY": moco_key,
        "ANTHROPIC_API_KEY": anthropic_key,
    }.items() if not v]
    if missing:
        print(f"Missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 2

    source_moco = SourceMocoClient(subdomain=subdomain, api_key=moco_key)
    purchases = MocoPurchaseClient(subdomain=subdomain, api_key=moco_key)
    ocr = AnthropicOcrClient(api_key=anthropic_key, model=args.model)

    # Always fetch the full draft pool (up to 100) so the --max N cap is
    # applied to the freshest N AFTER newest-first sorting. Limiting the
    # API call directly would just take whatever order Moco returns the
    # first N in, which isn't guaranteed to be newest-first.
    print(f"Listing drafts from "
          f"https://{subdomain}.mocoapp.com/api/v1/purchases/drafts …")
    try:
        drafts = purchases.list_purchase_drafts(limit=DRAFT_FETCH_CAP)
    except Exception as e:
        print(f"Failed to list drafts: {e}", file=sys.stderr)
        return 3
    drafts = _newest_first(drafts)[:args.max_drafts]
    print(f"Got {len(drafts)} draft(s) (newest first, capped at --max="
          f"{args.max_drafts}). "
          f"Mode: {'APPLY (real writes)' if args.apply else 'DRY-RUN'}")

    # Telegram intentionally NOT wired in. Batch runs touch dozens of drafts
    # at a time and would spam the chat with one alert per row; the table is
    # the audit surface here. Production webhook traffic still notifies as
    # usual.
    service = SupplierInvoiceOcrService(
        source_moco=source_moco, purchase_client=purchases, ocr=ocr,
        source_account_url=subdomain, telegram=None)

    rows: list[Row] = []
    for i, draft in enumerate(drafts, start=1):
        rows.append(_process_draft(
            draft, source_moco=source_moco, purchases=purchases, ocr=ocr,
            service=service, apply=args.apply, idx=i, total=len(drafts)))

    _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
