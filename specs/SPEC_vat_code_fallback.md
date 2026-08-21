# SPEC — VAT-code fallback for OCR'd purchases

Status: implemented on branch `ocr-vat-fallback`.

## Problem

Moco's `POST /purchases` requires a `vat_code_id` on every line item. The
OCR flow resolves it in three tiers (OCR'd `vat_rate` → matched supplier's
default → account-wide default flag). When all three miss, the field is
omitted, Moco answers `422`, and no purchase is created — the operator gets
a Telegram alert and has to type the whole receipt in by hand.

That is not a rare corner. Observed live on skyr draft
[3216692](https://skyr.mocoapp.com/purchases/drafts/3216692) — a CHF 15.00
lunch receipt from "Ligu Lehm", paid at the terminal:

- the slip prints no VAT line at all (Swiss receipts below CHF 400 need
  none), so OCR returns `vat_rate: null`;
- "Ligu Lehm" has no company record in Moco, so there is no supplier
  default;
- **neither** live Moco account (solar, skyr) flags any code as the
  account-wide default, so tier 3 can never fire either.

So for this whole document class the chain has no floor and every such
receipt 422s.

A second, latent defect sits in the same lookup. Both accounts return two
active `tax: 0.0` codes, and the **reverse-charge one sorts first**:

| account | id | tax | description | reverse_charge |
|---|---|---|---|---|
| skyr  | 33681  | 0.0 | (Ausland) | **true** |
| skyr  | 33679  | 0.0 |           | false |
| solar | 110533 | 0.0 | (Ausland) | **true** |
| solar | 110532 | 0.0 |           | false |

`_find_vat_code_by_rate` returns the first active code whose `tax` matches,
so an invoice OCR'd as 0% VAT already books to reverse charge today.

## Decisions

**D1 — the floor splits on payment method.** A fourth and final tier picks
a code by rate rather than giving up:

- `already_paid_by_card` (card / POS slip) → the active **0%** domestic
  code. These slips genuinely often carry no VAT breakdown; booking 0%
  claims no input tax, which understates the deduction and never
  overstates it — the safe direction under Swiss VAT, where the deduction
  needs a receipt that actually shows the tax.
- everything else (bank transfer / QR bill) → the active **8.1%** code,
  Switzerland's standard rate and the rate Moco itself defaults to. A
  supplier invoice paid by transfer essentially always carries it.

If the account has no active code at the wanted rate, the field is omitted
exactly as before (422 → Telegram alert). The tier never invents a code.

**D2 — special-scheme codes are never auto-picked.** A code with
`reverse_charge: true` or `intra_eu: true` is excluded from *every* rate
match, not just the new tier. This fixes the ordering defect above: tier 1
(OCR'd rate) and tier 2 (supplier default, which translates a rate) stop
silently selecting the "(Ausland)" code for a domestic 0% invoice. A
special code is still reachable — but only when it is the *sole* match for
the rate, which is the case where the account genuinely has nothing else.

**D3 — a guessed rate holds the purchase for review.** `PurchaseReviewGate`
gains a condition: when the VAT code came from the D1 fallback rather than
from the document, the supplier or the account, the purchase is tagged
`Review pending` with a German reason naming the guessed rate. Without this
a matched-supplier, high-confidence bank-transfer bill could auto-release
to `bexio-expense-sync` carrying a rate nobody ever read off the document.
Card receipts are held by the existing category chain anyway
(`MocoCategoryResolver` short-circuits at `already_paid`), so in practice
this new condition only bites transfer bills.

**D4 — the chain moves into `api/vat_code_resolver.py`.** `VatCodeResolver`
+ `VatDecision`, mirroring `MocoCategoryResolver` + `CategoryDecision`. Two
reasons beyond file size: the gate needs to know *which* tier produced the
code and cannot import it from the service (the service imports the gate),
and `scripts/batch_ocr_drafts.py` currently carries a hand-written **mirror**
of the chain (`_resolve_vat_code_with_tier`) that would drift the moment
this spec lands. Both operator scripts call the real class instead — the
same anti-drift argument that put `PurchaseReviewGate` in its own file.

`VatDecision.source` is one of `ocr` / `supplier` / `account_default` /
`fallback_zero` / `fallback_standard` / `None`.

**D5 — the webhook keeps ACKing 200 on a Moco 4xx.** Already the behavior:
`SupplierInvoiceOcrService.process` swallows a 4xx from the purchase flow
into `{"skipped": "moco_rejected"}` and the endpoint returns
`200 ok=true`, so Moco stops retrying. It was covered only at the service
level, so an endpoint-level regression test is added to pin the full
request → response contract.

## Out of scope

Backfilling the ~unknown number of drafts that already 422'd. The operator
re-triggers those through `scripts/batch_ocr_drafts.py`.
