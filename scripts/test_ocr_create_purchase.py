#!/usr/bin/env python3
"""End-to-end OCR + Moco-create-purchase validation against a real draft.

Drafts can't be patched via the API, so the production flow is:
  draft → download PDF → OCR → POST /purchases (with base64 attachment,
  tags ["OCR", "Review pending"], optional company_id from supplier lookup)
  → comment on the new purchase with OCR summary.

This script drives that exact pipeline against a real draft id. Two modes:
  - dry run (default): show what the POST /purchases payload + comment
    would be, do NOT touch Moco (besides the GET that fetches the draft).
  - --apply: actually create the purchase + post the comment.

Telegram is opt-in via --notify so accidental dry-runs don't ping.

Usage (from the repo root):
    vercel env pull .env.local
    .venv/bin/python scripts/test_ocr_create_purchase.py 3001069                 # dry run
    .venv/bin/python scripts/test_ocr_create_purchase.py 3001069 --apply         # actually create
    .venv/bin/python scripts/test_ocr_create_purchase.py 3001069 --apply --notify  # + Telegram

Required env:
    MOCO_SUBDOMAIN          source subdomain (e.g. "solar")
    MOCO_API_KEY              token for the Moco account
    ANTHROPIC_API_KEY                Claude API key
Optional env (only with --notify):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

The VAT code is resolved per invoice from `GET /vat_code_purchases`
(OCR vat_rate match → supplier company default → account default).

The Moco project (from `commission` → `Kommission`/`Aufwandkonto` custom-
properties on `GET /projects`) and the booking category (from
`GET /purchases/categories` keyed by `credit_account`) are resolved the
same way the production webhook does. Dry-run shows both — including
the `POST /purchases/{id}/assign_to_project` preview body.

Exit codes: 0 ok, 1 OCR error, 2 missing env / bad args, 3 Moco fetch error,
4 POST /purchases error.
"""

import argparse
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path
from urllib import error as urlerror

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.anthropic_ocr_client import AnthropicOcrClient, AnthropicOcrError
from api.moco_category_resolver import MocoCategoryResolver
from api.moco_project_resolver import MocoProjectResolver
from api.moco_purchase_client import MocoPurchaseClient
from api.moco_client import MocoClient
from api.moco_supplier_matcher import MocoSupplierMatcher
from api.supplier_invoice_ocr_service import (
    SupplierInvoiceOcrService,
    _build_create_payload,
    _format_email_source_comment,
    _format_ocr_comment,
    _user_id_from_draft,
)
from api.telegram_notifier import TelegramNotifier

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("test_ocr_create_purchase")


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


def _print_section(title: str, char: str = "-", width: int = 70) -> None:
    print(f"\n--- {title} " + char * max(0, width - len(title) - 5))


def _print_invoice(invoice) -> None:
    _print_section("OCR result")
    for field in dataclasses.fields(invoice):
        value = getattr(invoice, field.name)
        print(f"  {field.name:<18} {value!r}")


def _print_payload_summary(payload: dict) -> None:
    """Show the payload that would be POSTed without dumping the base64 PDF
    blob (which would flood the terminal)."""
    redacted = dict(payload)
    if "file" in redacted:
        file_obj = redacted["file"]
        redacted["file"] = {
            "filename": file_obj.get("filename"),
            "base64": f"<{len(file_obj.get('base64', ''))} chars elided>",
        }
    print(json.dumps(redacted, indent=2, ensure_ascii=False))


def _redact_payload_for_log(payload: dict) -> dict:
    """Return a copy of the payload safe to print (no base64 blob)."""
    out = dict(payload)
    if "file" in out:
        out["file"] = {
            "filename": out["file"].get("filename"),
            "base64_chars": len(out["file"].get("base64", "")),
        }
    return out


def _render_html_for_console(html: str) -> str:
    """Render a Moco-flavoured HTML comment as readable terminal text.

    The comment body in production is HTML (Moco strips all but a small
    tag set), but a single-line `<div>...<ul>...<li>...` blob is unreadable
    when echoed to the terminal. This helper converts the Moco-allowed
    subset (div, strong, em, u, ul, ol, li, br) into ANSI-styled plain
    text purely for display — does NOT alter what's actually sent to Moco.
    Bold via ANSI ESC, list items as bulleted lines.
    """
    import re
    from html import unescape

    bold_start, bold_end = "\x1b[1m", "\x1b[22m"
    out = html
    # <strong>X</strong> → ANSI-bold X. Same for <u>/<em> (rendered bold-ish
    # so the terminal at least signals emphasis).
    out = re.sub(r"<strong>(.*?)</strong>",
                 lambda m: f"{bold_start}{m.group(1)}{bold_end}", out,
                 flags=re.DOTALL)
    out = re.sub(r"<(?:em|u)>(.*?)</(?:em|u)>",
                 lambda m: f"{bold_start}{m.group(1)}{bold_end}", out,
                 flags=re.DOTALL)
    # Lists: insert a newline before each <li>, render as bullet, drop ul/ol.
    out = re.sub(r"</?(?:ul|ol)>", "\n", out)
    out = re.sub(r"<li>(.*?)</li>", r"\n  • \1", out, flags=re.DOTALL)
    # Line break + paragraph-ish wrappers.
    out = out.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    out = re.sub(r"</?(?:div|pre)>", "", out)
    # Any leftover unknown tags get stripped (defensive — shouldn't happen
    # since the service only emits the Moco-allowed subset).
    out = re.sub(r"<[^>]+>", "", out)
    # HTML entity decode (&amp; / &lt; / &gt; etc).
    out = unescape(out)
    # Forwarded HTML emails often render as a wall of single-space /
    # &nbsp;-padded blank lines between content blocks (`<br>&nbsp;<br>`,
    # `<div>&nbsp;</div>`). For the console preview we don't need that
    # spacing — flatten whitespace-only lines (incl. non-breaking space
    # from `&nbsp;` decode) to empty, then collapse any double+ newlines
    # to a single newline. `[^\S\n\r]+` = any whitespace EXCEPT newlines.
    out = re.sub(r"^[^\S\n\r]+$", "", out, flags=re.MULTILINE)
    out = re.sub(r"\n{2,}", "\n", out).strip()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("draft_id", type=int,
                        help="Moco draft purchase id")
    parser.add_argument("--apply", action="store_true",
                        help="Actually POST /purchases + post comment "
                             "(default: dry run, no Moco writes)")
    parser.add_argument("--notify", action="store_true",
                        help="Send the Telegram alert (requires TELEGRAM_* "
                             "env). Default: off so dry-runs don't ping.")
    parser.add_argument("--model", default=None,
                        help="Override the Claude model")
    parser.add_argument("--env-file", type=Path,
                        default=Path(__file__).resolve().parent.parent / ".env.local")
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

    telegram = None
    if args.notify:
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
        if not (tg_token and tg_chat):
            print("--notify needs TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID",
                  file=sys.stderr)
            return 2
        telegram = TelegramNotifier(bot_token=tg_token, chat_id=tg_chat)

    moco = MocoClient(subdomain=subdomain, api_key=moco_key)
    purchases = MocoPurchaseClient(subdomain=subdomain, api_key=moco_key)
    ocr = AnthropicOcrClient(api_key=anthropic_key, model=args.model)

    # 1. fetch the draft (this is the only read we always do)
    log.info("fetching Moco draft purchase %s", args.draft_id)
    try:
        draft = purchases.get_purchase_draft(args.draft_id)
    except Exception as e:
        print(f"Failed to fetch draft: {e}", file=sys.stderr)
        return 3

    file_url = draft.get("file_url")
    _print_section("Moco draft (source of the OCR)")
    print(f"  id:                 {draft.get('id')}")
    print(f"  company:            {(draft.get('company') or {}).get('name')!r}")
    print(f"  date:               {draft.get('date')}")
    print(f"  gross_total:        {draft.get('gross_total')}")
    print(f"  receipt_identifier: {draft.get('receipt_identifier')!r}")
    # IBAN + reference come from Moco's email-import QR-bill parser; the
    # service prefers these over OCR when present (see
    # `_prefer_draft_payment_fields`), so seeing them here makes it
    # obvious which IBAN will end up on the created purchase.
    print(f"  iban:               {draft.get('iban')!r}")
    print(f"  reference:          {draft.get('reference')!r}")
    # email_from / email_body are also populated by the email-import flow.
    # The service forwards them into the Moco comment on the new purchase.
    print(f"  email_from:         {draft.get('email_from')!r}")
    email_body = draft.get("email_body") or ""
    preview = email_body if len(email_body) <= 200 else email_body[:200] + "…"
    print(f"  email_body:         {preview!r} ({len(email_body)} chars total)")
    print(f"  file_url:           {'<present>' if file_url else '<MISSING>'}")
    if not file_url:
        print("\nNo file_url — nothing to OCR.", file=sys.stderr)
        return 3

    # 2. OCR
    log.info("downloading PDF and running OCR via %s", ocr._model)
    try:
        pdf_bytes = moco.download_file(file_url)
        invoice = ocr.extract(pdf_bytes)
    except AnthropicOcrError as e:
        print(f"\nOCR failed: {e} (status_code={e.status_code})",
              file=sys.stderr)
        return 1

    _print_invoice(invoice)

    # 3. supplier lookup — same three-tier matcher as the webhook flow
    # (exact → substring → normalized token-set, unique hit required).
    log.info("looking up supplier company in Moco")
    try:
        suppliers = moco.list_suppliers()
    except Exception as e:
        log.warning("supplier list failed: %s", e)
        suppliers = []
    match = MocoSupplierMatcher(suppliers).match(invoice.supplier_name)
    if match.status == "matched":
        company_id = match.company.get("id")
        print(f"\nSupplier match: id={company_id} "
              f"name={match.company.get('name')!r} ({match.tier} tier)")
    elif match.status == "ambiguous":
        company_id = None
        print(f"\nSupplier ambiguous ({match.candidate_count} candidates "
              f"at {match.tier} tier) — leaving company_id empty:")
        for c in match.candidates:
            print(f"  - id={c.get('id')} name={c.get('name')!r}")
    else:
        company_id = None
        print(f"\nNo supplier match for {invoice.supplier_name!r} — "
              "purchase will be created without company_id")

    # 4. resolve the vat code via /vat_code_purchases + supplier default
    try:
        vat_codes = purchases.list_vat_codes()
    except Exception as e:
        log.warning("list_vat_codes failed: %s", e)
        vat_codes = []
    vat_summary = [
        {"id": c.get("id"),
         "tax": c.get("tax"),
         "code": c.get("code"),
         "active": c.get("active"),
         "default": c.get("default") or c.get("is_default")}
        for c in vat_codes
    ]
    # print(f"\nAvailable vat_code_purchases: {vat_summary}")

    # Same resolution chain the service uses, exposed via the import.
    from api.supplier_invoice_ocr_service import (
        _account_default_vat_code,
        _find_vat_code_by_rate,
        _supplier_default_vat_code_id,
    )
    # Full company record, fetched once — feeds the vat chain (supplier
    # default vat code) and the category chain (supplier Aufwandkonto).
    full_company = None
    if company_id is not None:
        try:
            full_company = moco.get_company(company_id)
        except Exception as e:
            log.warning("get_company failed: %s", e)
    vat_code_id = None
    if invoice.vat_rate is not None:
        match = _find_vat_code_by_rate(vat_codes, invoice.vat_rate)
        if match:
            vat_code_id = match.get("id")
            print(f"  → matched OCR vat_rate={invoice.vat_rate} to "
                  f"vat_code_id={vat_code_id}")
    if vat_code_id is None and full_company:
        vat_code_id = _supplier_default_vat_code_id(full_company, vat_codes)
        if vat_code_id is not None:
            print(f"  → using supplier-default vat_code_id={vat_code_id}")
    if vat_code_id is None:
        account_default = _account_default_vat_code(vat_codes)
        if account_default is not None:
            vat_code_id = account_default.get("id")
            print(f"  → using account-default vat_code_id={vat_code_id}")
    if vat_code_id is None:
        print("  → no vat_code_id resolved (Moco will reject the POST)")

    # 5. resolve Moco project from the OCR'd Kommission
    _print_section("Project (Kommission) resolution")
    log.info("listing Moco projects for Kommission resolution")
    try:
        all_projects = moco.list_projects()
    except Exception as e:
        log.warning("list_projects failed: %s — resolver will be empty", e)
        all_projects = []
    project_resolver = MocoProjectResolver(all_projects)
    print(f"  {len(all_projects)} project(s) returned, "
          f"{project_resolver.indexed_count()} indexed.")
    kommission_match = project_resolver.resolve(invoice.commission)
    if invoice.commission is None or invoice.commission == "":
        print("  OCR returned no commission — nothing to resolve.")
    elif kommission_match.status == "matched":
        proj = kommission_match.project
        print(f"  Kommission {invoice.commission!r} → project id={proj.get('id')}"
              f" name={proj.get('name')!r} (tier={kommission_match.tier})")
    elif kommission_match.status == "ambiguous":
        print(f"  Kommission {invoice.commission!r} → AMBIGUOUS "
              f"({kommission_match.candidate_count} candidates, "
              f"tier={kommission_match.tier}) — purchase will NOT be assigned")
    else:
        print(f"  Kommission {invoice.commission!r} → no_match — "
              "purchase will NOT be assigned")

    # 6. resolve category (Buchhaltungs-Konto) from the project's or
    #    supplier's Aufwandkonto, or the hardcoded 4000 fallback
    _print_section("Category (Buchhaltungs-Konto) resolution")
    log.info("listing Moco purchase categories")
    try:
        all_categories = purchases.list_categories()
    except Exception as e:
        log.warning("list_categories failed: %s — resolver will be empty", e)
        all_categories = []
    category_resolver = MocoCategoryResolver(all_categories)
    print(f"  {len(all_categories)} category(s) returned, "
          f"{category_resolver.indexed_count()} indexed by credit_account.")
    matched_project = (kommission_match.project
                       if kommission_match.status == "matched" else None)
    category_decision = category_resolver.resolve(
        already_paid_by_card=invoice.already_paid_by_card,
        project=matched_project,
        supplier=full_company)
    print(f"  category_id={category_decision.category_id} "
          f"({category_decision.reason})")

    # 7. show what would be POSTed
    payload = _build_create_payload(
        invoice, pdf_bytes,
        vat_code_id=vat_code_id,
        company_id=company_id,
        draft_id=args.draft_id,
        user_id=_user_id_from_draft(draft),
        category_id=category_decision.category_id,
    )
    email_comment = _format_email_source_comment(
        email_from=draft.get("email_from"),
        email_body=draft.get("email_body"),
    )
    ocr_comment = _format_ocr_comment(invoice)
    _print_section("POST /purchases payload (base64 blob elided)")
    _print_payload_summary(payload)
    # The service posts these as two separate Moco comments. Render each
    # independently so the operator sees what each entry will look like.
    if email_comment:
        _print_section("Comment 1: 📧 Email-Quelle (rendered)")
        print(_render_html_for_console(email_comment))
    else:
        _print_section("Comment 1: 📧 Email-Quelle — SKIPPED (no email fields on draft)")
    _print_section("Comment 2: 🤖 OCR-Extraktion (rendered)")
    print(_render_html_for_console(ocr_comment))

    # Preview the assign_to_project call(s) the service would make after
    # create. Skipped silently when no project resolved (no call would
    # fire). Purchase id and item id are placeholders — the real ones
    # come back from Moco's create response in --apply mode.
    _print_section("POST /purchases/{id}/assign_to_project (preview)")
    if matched_project is None:
        print("  — skipped (no project resolved)")
    else:
        preview_assign = {
            "item_id": "<assigned by Moco>",
            "project_id": matched_project.get("id"),
            "notify_project_leader": False,
            "billable": True,
            "budget_relevant": True,
            "surcharge": True,
        }
        print(f"  for each created line item, would POST to "
              f"/purchases/<new-id>/assign_to_project with:")
        print(json.dumps(preview_assign, indent=2, ensure_ascii=False))
        print(f"  → links the new purchase to "
              f"project id={matched_project.get('id')} "
              f"name={matched_project.get('name')!r}")

    if not args.apply:
        print("\n[dry run — no purchase created. Re-run with --apply.]")
        return 0

    # 8. actually go through the service so production behavior is exercised
    _print_section("Apply with production behavior")

    service = SupplierInvoiceOcrService(
        moco=moco,
        purchase_client=purchases,
        ocr=ocr,
        subdomain=subdomain,
        telegram=telegram,
        # Reuse the same resolvers built above — service will re-resolve
        # internally during process() but the index data is the same, so
        # the chosen project and category match what the dry-run preview
        # showed (modulo any /projects or /categories changes in the
        # seconds between this call and the service's own list).
        project_resolver=project_resolver,
        category_resolver=category_resolver,
    )
    log.info("creating real Moco purchase from draft %s", args.draft_id)
    try:
        # The service re-runs OCR on the same PDF — one extra Anthropic
        # call, acceptable for a one-off validation tool and keeps the
        # code path identical to the production webhook handler.
        result = service.process("create", {"id": args.draft_id,
                                            "file_url": file_url})
    except urlerror.HTTPError as e:
        # Moco 422 typically returns a JSON body like
        # `{"errors": {"items": ["vat_code_id is required"]}}`. Log status
        # + body so the operator can see exactly which field Moco refused.
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = "<unreadable>"
        print(f"\nMoco API error: HTTP {e.code} {e.reason}\n"
              f"URL: {e.url}\n"
              f"Body: {body}", file=sys.stderr)
        return 4
    except Exception as e:
        print(f"\nPOST /purchases failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 4

    _print_section("CREATED")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result.get("purchase_id"):
        print(f"\nNew purchase: https://{subdomain}.mocoapp.com"
              f"/purchases/{result['purchase_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
