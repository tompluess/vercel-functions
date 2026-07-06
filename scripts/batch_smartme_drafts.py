#!/usr/bin/env python3
"""Batch validation for the smart-me Energiekostenabrechnung branch.

Lists `GET /purchases/drafts` (newest first), runs `is_smartme_draft` on
each, and drives the detected ones through the smart-me pipeline: PDF
download → energy-bill OCR (Objekt / Netto-Betrag / Abrechnungszeitraum)
→ ZEV/Eigenverbrauch project match → expense payload. Non-smart-me
drafts are reported as skipped rows without spending an OCR call.

Default mode is dry-run: OCR runs against each detected draft's PDF, but
NO expense is created, NO comment is posted, and NO draft is deleted.
`--apply` switches to production behavior — the same
`SmartmeEnergyExpenseService.process_draft` the webhook handler runs
(expense create + draft delete on success; draft comment + keep on
no-match), minus Telegram (batch convention: the table is the audit
surface).

Usage (from the repo root):
    vercel env pull .env.local
    .venv/bin/python scripts/batch_smartme_drafts.py                      # dry-run, newest 10
    .venv/bin/python scripts/batch_smartme_drafts.py --draft-id 3070959   # dry-run, one draft
    .venv/bin/python scripts/batch_smartme_drafts.py --draft-id 3070959 --apply

Required env (same as batch_ocr_drafts.py):
    MOCO_SUBDOMAIN     Moco subdomain (e.g. "solar")
    MOCO_API_KEY       token for the Moco account
    ANTHROPIC_API_KEY  Claude API key

Exit codes: 0 — table printed (per-draft errors are recorded as rows,
not fatal); 2 — missing env / bad args; 3 — could not list/fetch drafts.
"""

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.anthropic_ocr_client import AnthropicOcrClient, AnthropicOcrError
from api.moco_client import MocoClient
from api.moco_purchase_client import MocoPurchaseClient
from api.smartme_energy_expense_service import (
    SmartmeEnergyExpenseService,
    _expense_title,
    is_smartme_draft,
)
from api.smartme_project_matcher import (
    SmartmeProjectMatcher,
    project_energy_label,
)

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("batch_smartme_drafts")

DRAFT_FETCH_CAP = 100


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

    `smartme` is the detection outcome — non-detected drafts short-circuit
    with everything else None. `project` is `✓ <name>` on a unique match,
    `✗ ambiguous (N)` / `✗ no match` otherwise. `label` is ZEV / EV.
    `expense_id` is set only in apply mode after a successful create.
    """
    draft_id: int
    smartme: bool
    objekt: str | None
    project: str | None
    label: str | None
    amount: str | None
    zeitraum: str | None
    expense_id: int | None
    result: str


def _newest_first(drafts: list[dict]) -> list[dict]:
    return sorted(drafts,
                  key=lambda d: d.get("created_at") or "",
                  reverse=True)


def _step(msg: str) -> None:
    print(f"      {msg}", flush=True)


def _project_cell(match) -> str:
    if match.status == "matched":
        return f"✓ {match.project.get('name')}"
    if match.status == "ambiguous":
        return f"✗ ambiguous ({match.candidate_count})"
    if match.status == "empty":
        return "✗ kein Objekt"
    return "✗ no match"


def _process_draft(draft: dict, *, moco: MocoClient,
                   ocr: AnthropicOcrClient,
                   matcher: SmartmeProjectMatcher,
                   service: SmartmeEnergyExpenseService,
                   apply: bool, idx: int, total: int) -> Row:
    draft_id = draft.get("id")
    title = (draft.get("title") or "")[:60]
    print(f"[{idx}/{total}] Draft {draft_id} — {title!r}", flush=True)

    if not is_smartme_draft(draft):
        _step("not a smart-me Energiekostenabrechnung — skipped")
        return Row(draft_id, False, None, None, None, None, None, None,
                   "Skipped: not smart-me")

    file_url = draft.get("file_url")
    if not file_url:
        _step("smart-me draft WITHOUT attachment")
        if apply:
            result = service.process_draft(draft)
            return Row(draft_id, True, None, None, None, None, None, None,
                       f"kept + commented ({result.get('skipped')})")
        return Row(draft_id, True, None, None, None, None, None, None,
                   "Dry-run: no attachment (would keep + comment)")

    try:
        pdf_bytes = moco.download_file(file_url)
        _step(f"PDF: {len(pdf_bytes) / 1024:.0f} KB")
        t0 = time.monotonic()
        bill = ocr.extract_energy_bill(pdf_bytes)
        _step(f"OCR: {time.monotonic() - t0:.1f}s, "
              f"confidence={bill.confidence:.0%}, objekt={bill.objekt!r}, "
              f"netto={bill.net_amount}, "
              f"zeitraum={bill.period_from}..{bill.period_to}")
    except AnthropicOcrError as e:
        _step(f"OCR FAILED: {e}")
        return Row(draft_id, True, None, None, None, None, None, None,
                   f"OCR error: {e}")
    except Exception as e:
        _step(f"download/OCR FAILED: {e}")
        return Row(draft_id, True, None, None, None, None, None, None,
                   f"error: {e}")

    match = matcher.match(bill.objekt)
    _step(f"project: {_project_cell(match)} "
          f"(tier={match.tier}, score={match.score})")
    label = (project_energy_label(match.project)
             if match.status == "matched" else None)
    amount = (f"CHF {bill.net_amount:.2f}"
              if bill.net_amount is not None else None)
    zeitraum = (f"{bill.period_from}..{bill.period_to}"
                if bill.period_from and bill.period_to else None)

    if not apply:
        if match.status == "matched" and bill.net_amount is not None:
            title = _expense_title(match.project)
            _step(f"would create expense: {title!r} on "
                  f"project {match.project.get('id')}")
            result = "Dry-run: would create expense + delete draft"
        else:
            result = "Dry-run: would keep draft + comment"
        return Row(draft_id, True, bill.objekt, _project_cell(match),
                   label, amount, zeitraum, None, result)

    # Apply mode: webhook parity via the service (re-downloads the PDF and
    # re-runs OCR — one duplicate Anthropic call per draft is the price of
    # exercising the exact production code path).
    outcome = service.process_draft(draft)
    if outcome.get("expense_id"):
        _step(f"created expense {outcome['expense_id']}, draft deleted")
        return Row(draft_id, True, bill.objekt, _project_cell(match),
                   label, amount, zeitraum, outcome["expense_id"],
                   f"created: {outcome.get('expense_title')}")
    _step(f"kept: {outcome.get('skipped')}")
    return Row(draft_id, True, bill.objekt, _project_cell(match),
               label, amount, zeitraum, None,
               f"kept + commented ({outcome.get('skipped')})")


def _trim(value: str | None, width: int = 44) -> str:
    if not value:
        return "-"
    return value if len(value) <= width else value[:width - 1] + "…"


def _print_table(rows: list[Row]) -> None:
    headers = ("DRAFT ID", "SMART-ME", "OBJEKT", "PROJEKT", "LABEL",
               "BETRAG", "ZEITRAUM", "RESULT")
    cells = [(str(r.draft_id),
              "✓" if r.smartme else "-",
              _trim(r.objekt, 34),
              _trim(r.project, 40),
              r.label or "-",
              r.amount or "-",
              r.zeitraum or "-",
              _trim(r.result))
             for r in rows]
    widths = [max(len(h), *(len(c[i]) for c in cells)) if cells else len(h)
              for i, h in enumerate(headers)]
    print()
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for c in cells:
        print("  ".join(v.ljust(w) for v, w in zip(c, widths)))

    detected = sum(1 for r in rows if r.smartme)
    created = sum(1 for r in rows if r.expense_id is not None)
    matched = sum(1 for r in rows if r.project and r.project.startswith("✓"))
    print()
    print(f"Total: {len(rows)}   smart-me: {detected}   "
          f"project-matched: {matched}   expenses created: {created}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max", dest="max_drafts", type=int, default=10,
                        help="Maximum number of drafts to process "
                             "(newest first). Default: 10.")
    parser.add_argument("--draft-id", type=int, default=None,
                        help="Process exactly this draft (bypasses the "
                             "listing).")
    parser.add_argument("--apply", action="store_true",
                        help="Actually create expenses / post comments / "
                             "delete drafts (default: dry-run, OCR only).")
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

    print("Loading active Moco projects for ZEV/Eigenverbrauch matching …")
    try:
        all_projects = moco.list_projects()
    except Exception as e:
        print(f"  WARN: list_projects failed ({e}); matching against an "
              "empty index.", file=sys.stderr)
        all_projects = []
    matcher = SmartmeProjectMatcher(all_projects)
    print(f"  {len(all_projects)} project(s) returned, "
          f"{matcher.indexed_count()} labeled ZEV/Eigenverbrauch.")

    # Telegram intentionally NOT wired in (batch convention — the table is
    # the audit surface; production webhook traffic still notifies).
    service = SmartmeEnergyExpenseService(
        moco=moco, purchase_client=purchases, ocr=ocr,
        matcher=matcher, subdomain=subdomain, telegram=None)

    rows: list[Row] = []
    for i, draft in enumerate(drafts, start=1):
        rows.append(_process_draft(
            draft, moco=moco, ocr=ocr, matcher=matcher, service=service,
            apply=args.apply, idx=i, total=len(drafts)))

    _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
