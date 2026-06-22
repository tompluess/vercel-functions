#!/usr/bin/env python3
"""Run AnthropicOcrClient against a real Moco supplier-invoice PDF.

Used to validate OCR quality against 3–5 real PVcontracting invoices before
wiring up the `supplier-invoice-ocr` endpoint (SPEC Implementation Order
step 1). Exercises the same code path production will use; no test fakes.

Flow:
  1. GET /api/v1/purchases/{id} on the source Moco account → get `file_url`.
  2. Download the pre-signed `file_url` → raw PDF bytes.
  3. AnthropicOcrClient.extract(pdf_bytes) → InvoiceData.
  4. Pretty-print the result so the operator can eyeball field accuracy.

Usage (from the repo root):
    # Pull the keys once (writes .env.local with MOCO_*, ANTHROPIC_API_KEY, ...)
    vercel env pull .env.local

    # Then for each purchase you want to OCR:
    python scripts/test_ocr_real.py 3001069
    python scripts/test_ocr_real.py 3001069 --model claude-opus-4-7   # try a different model
    python scripts/test_ocr_real.py 3001069 --save-pdf /tmp/inv.pdf   # also dump the PDF for inspection

Required env (read from .env.local if present, else from the shell):
    MOCO_SOURCE_SUBDOMAIN   — e.g. "solar"
    MOCO_SOURCE_API_KEY     — token for the source Moco account
    ANTHROPIC_API_KEY       — Claude API key

Exits 0 on success, 1 on Anthropic OCR error, 2 on missing env / bad args, 3
on Moco fetch errors. The point is manual review — there's no assertion of
"correct" output, just a human-readable dump of every InvoiceData field.
"""

import argparse
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.anthropic_ocr_client import AnthropicOcrClient, AnthropicOcrError
from api.source_moco_client import SourceMocoClient

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("test_ocr_real")


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so the script works straight after `vercel env pull`
    without requiring python-dotenv. Ignores comments and blank lines; does
    NOT overwrite existing shell env (so `KEY=x python script.py` still wins).
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _get_purchase(moco: SourceMocoClient, purchase_id: int) -> dict:
    """GET /purchases/{id} on the source Moco account.

    SourceMocoClient doesn't expose this directly (it was written for
    /companies and /projects only), so we reach in to its base URL + auth
    headers and reuse its urlopen. Acceptable for a one-off validation
    script — production reads go through a dedicated MocoPurchaseClient
    (SPEC step 2).
    """
    from urllib import request as urlrequest
    url = f"{moco._base_url}/purchases/drafts/{purchase_id}"
    log.info("GET %s", url)
    req = urlrequest.Request(url, headers=moco._auth_headers)
    with urlrequest.urlopen(req, timeout=moco.HTTP_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("purchase_id", type=int,
                        help="Moco purchase id (the numeric tail of the URL)")
    parser.add_argument("--model", default=None,
                        help="Override the Claude model (default: claude-sonnet-4-6)")
    parser.add_argument("--save-pdf", type=Path, default=None,
                        help="Also write the downloaded PDF to this path for inspection")
    parser.add_argument("--env-file", type=Path,
                        default=Path(__file__).resolve().parent.parent / ".env.local",
                        help="Path to a .env file to load (default: .env.local at repo root)")
    args = parser.parse_args()

    _load_dotenv(args.env_file)

    subdomain = os.environ.get("MOCO_SOURCE_SUBDOMAIN")
    moco_key = os.environ.get("MOCO_SOURCE_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    missing = [k for k, v in {
        "MOCO_SOURCE_SUBDOMAIN": subdomain,
        "MOCO_SOURCE_API_KEY": moco_key,
        "ANTHROPIC_API_KEY": anthropic_key,
    }.items() if not v]
    if missing:
        print(f"Missing env vars: {', '.join(missing)}. "
              f"Run `vercel env pull {args.env_file}` first, or export them.",
              file=sys.stderr)
        return 2

    moco = SourceMocoClient(subdomain=subdomain, api_key=moco_key)
    ocr = AnthropicOcrClient(api_key=anthropic_key, model=args.model)

    log.info("fetching Moco purchase %s from %s.mocoapp.com",
             args.purchase_id, subdomain)
    try:
        purchase = _get_purchase(moco, args.purchase_id)
    except Exception as e:
        print(f"Failed to fetch Moco purchase {args.purchase_id}: {e}",
              file=sys.stderr)
        return 3

    file_url = purchase.get("file_url")
    company_name = (purchase.get("company") or {}).get("name") or "(no company)"
    print("\n--- Moco purchase metadata "
          + "-" * 40)
    print(f"id:                  {purchase.get('id')}")
    print(f"company:             {company_name}")
    print(f"receipt_identifier:  {purchase.get('receipt_identifier')!r}")
    print(f"date / due_date:     {purchase.get('date')} / {purchase.get('due_date')}")
    print(f"gross_total / net:   {purchase.get('gross_total')} / {purchase.get('net_total')}")
    print(f"iban:                {purchase.get('iban')!r}")
    print(f"file_url:            {'<present>' if file_url else '<MISSING — nothing to OCR>'}")

    if not file_url:
        print("\nNo file_url on this purchase — nothing to OCR.", file=sys.stderr)
        return 3

    log.info("downloading PDF from signed file_url")
    try:
        pdf_bytes = moco.download_file(file_url)
    except Exception as e:
        print(f"Failed to download PDF: {e}", file=sys.stderr)
        return 3
    log.info("PDF size: %d bytes", len(pdf_bytes))

    if args.save_pdf:
        args.save_pdf.write_bytes(pdf_bytes)
        log.info("PDF saved to %s", args.save_pdf)

    log.info("running OCR via Anthropic (%s)", ocr._model)
    try:
        invoice = ocr.extract(pdf_bytes)
    except AnthropicOcrError as e:
        print(f"\nOCR failed: {e} (status_code={e.status_code})", file=sys.stderr)
        return 1

    print("\n--- OCR result " + "-" * 50)
    for field in dataclasses.fields(invoice):
        value = getattr(invoice, field.name)
        print(f"{field.name:<18} {value!r}")


    print(f"\nconfidence: {invoice.confidence:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
