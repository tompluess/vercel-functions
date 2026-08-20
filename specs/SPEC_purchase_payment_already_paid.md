# SPEC: Register a Moco payment for already-paid purchases

Spec for automatically registering a **purchase payment** (German: "Ausgaben
/ Zahlungen") in Moco when the OCR flow creates a purchase from a receipt
that was already settled at the point of sale — credit card, debit card /
EC-Karte, Maestro, Visa, Mastercard, TWINT, or a POS / EFT terminal slip.

Read `CLAUDE.md` first for project conventions and the existing
`SupplierInvoiceOcrService` flow — this feature is a small extension to that
service's post-create side-effect chain and reuses its best-effort posture.

---

## Problem

`SupplierInvoiceOcrService` already detects already-settled card receipts:
`InvoiceData.already_paid_by_card` drives `payment_method="credit_card"`,
suppresses `due_date` / `iban` / `reference`, and makes
`MocoCategoryResolver` decline to guess a Buchhaltungs-Konto.

But the created purchase still lands in Moco with `status: "pending"` and
`payments: []` — i.e. Moco believes money is still owed. The operator has to
open each card receipt and register the payment by hand just to move it to
`paid`, which is pure bookkeeping noise: the receipt itself is proof the
money already left the account.

Nothing else in the pipeline is affected — the Bexio expense sync is fed by
`Purchase:create|update` webhooks from a *different* endpoint and does not
read `payments`.

---

## Moco API (confirmed against the official docs)

Purchase payments are a **top-level** collection, not a sub-resource of a
purchase:

```
GET    /api/v1/purchases/payments          (filters: purchase_id, date_from, date_to)
GET    /api/v1/purchases/payments/{id}
POST   /api/v1/purchases/payments
POST   /api/v1/purchases/payments/bulk
PUT    /api/v1/purchases/payments/{id}
DELETE /api/v1/purchases/payments/{id}
```

`POST /purchases/payments` fields:

| field         | required | notes                                                       |
|---------------|----------|-------------------------------------------------------------|
| `date`        | yes      | `"2018-10-20"`                                              |
| `total`       | yes      | e.g. `1000` — the gross amount paid                         |
| `purchase_id` | no*      | \*required in our case; the alternative is a `description`   |
| `description` | no       | only valid when `purchase_id` is **not** set — not our case |

Response shape:

```json
{
  "id": 123,
  "date": "2022-03-01",
  "purchase": {"id": 12345, "identifier": "E2203-001", "title": "…"},
  "total": "1999.00",
  "created_at": "…",
  "updated_at": "…"
}
```

Source: <https://everii-group.github.io/mocoapp-api-docs/sections/purchase_payments.html>

Note there is **no idempotency key**. A duplicate POST creates a second
payment row, which would over-settle the purchase. See "Idempotency" below.

---

## Design

### 1. Transport — `MocoPurchaseClient.create_payment`

One new method on the existing `api/moco_purchase_client.py` (it belongs
there: purchase-domain write, same auth, same `/purchases` URL space). No new
class — this is a single endpoint, not a collaborator with logic of its own.

```python
def create_payment(self, *, purchase_id: int, date: str,
                   total: float) -> dict:
    """POST /purchases/payments — register a payment against a purchase."""
```

Pure transport, consistent with the rest of the class: caller owns the
decision, errors propagate.

### 2. Trigger — inside `SupplierInvoiceOcrService`

Runs in the existing post-create block in `process()`, alongside
`_post_summary_comments` / `_assign_resolved_project` /
`_delete_draft_after_create`:

```
if new_purchase_id:
    self._post_summary_comments(...)
    assign_warnings = self._assign_resolved_project(...)
    payment_warning = self._register_payment_if_already_paid(created, invoice)
    self._delete_draft_after_create(...)
```

Order: **after** `assign_to_project`, **before** the draft delete. Project
assignment mutates line items and is the more failure-prone step; the
payment is a leaf write that shouldn't sit between the item mutations.

### 3. Gate

Register a payment **only** when all of these hold:

1. `invoice.already_paid_by_card` is `True` — the single OCR signal that the
   document says "settled". Deliberately not widened to "no IBAN present"
   or "due_date in the past": a bill with no IBAN is a MANUAL-transfer bill,
   not a paid one, and mis-settling an open bill is worse than leaving a
   settled one open (the operator sees the latter; the former silently
   disappears from the "was ist offen" view).
2. A payable amount is resolvable (see field mapping) — a `0.0` or `None`
   total is skipped.
3. `invoice.is_credit_note` is `False`. A card *refund* is conceivable but
   we have no live example, the sign convention is unverified, and credit
   notes already route through their own review alert. Out of scope —
   revisit if one shows up.

Everything else (bank transfers, QR bills, unmatched drafts) is untouched.

### 4. Field mapping

| Moco payment field | value                                                       |
|--------------------|-------------------------------------------------------------|
| `purchase_id`      | `created["id"]` — the newly created purchase                |
| `date`             | `created["date"]` (the purchase date, already resolved from `invoice.invoice_date or today`); falls back to `_today()` |
| `total`            | `created["gross_total"]` when present, else `invoice.total_amount` |

**`total` prefers the server's `gross_total`** over the OCR'd
`invoice.total_amount`: Moco recomputes gross from the line item + VAT code,
so its own figure is the one that will make `status` flip to `paid`. Using
the OCR figure risks a rounding-cent mismatch that leaves the purchase
half-settled. The OCR value is only a fallback for the (unexpected) case
where the create response omits `gross_total`.

**`date` uses the purchase date, not today.** For a card receipt, the
document date *is* the payment date — that's what "already paid" means. Using
today's date would mis-date the payment into a later accounting period when a
receipt is imported late.

### 5. Failure posture — best-effort, mirrors `_assign_resolved_project`

The created purchase is the authoritative side effect. A failed payment
registration must not fail the sync:

- `HTTPError` → tidy `logger.warning` with status + truncated body (per
  `feedback_soft_failure_logging` — no traceback), collect a short warning
  string.
- any other `Exception` → `logger.exception`, collect a warning string.
- The warning is surfaced on the existing Telegram outcome alert as a
  separate line, so the operator can register the payment manually from the
  same purchase link:

  ```
  ⚠️ Zahlung nicht registriert: HTTP 422 {...}
  ```

- `process()` still returns `ok=true`.

The alert reuses `_notify_outcome`'s existing `suffix` mechanism (a second
optional suffix line) rather than firing a separate Telegram message — one
message per draft stays the rule.

### 6. Result payload

`process()`'s return dict gains one key so the batch script
(`scripts/batch_ocr_drafts.py`) and endpoint tests can assert on it:

```python
"payment_registered": bool
```

`True` only when the POST succeeded. `False` for skipped-by-gate and for
failed, which is fine — the field answers "is this purchase settled in
Moco", not "why not".

### 7. Idempotency

Moco offers no idempotency key on this endpoint, so a webhook replay could
in principle double-register. In practice it can't: the payment is only
created immediately after a *successful* `POST /purchases`, and a replay of
the same draft hits the `receipt_identifier: ["ist bereits vergeben"]` 422
in `create_purchase` and returns `skipped: "moco_rejected"` before reaching
the payment step. Receipt-number-less drafts are the one theoretical gap —
they'd create a duplicate purchase too, and the duplicate purchase is the
louder problem. **No pre-flight `GET /purchases/payments?purchase_id=…`
check**: it costs a round-trip on every card receipt to defend against a
condition that already implies a duplicate purchase.

---

## Non-goals

- Payments for bank-transfer purchases. Those are settled by the actual
  bank transfer, which the Bexio outgoing-payment flow already initiates;
  registering a Moco payment at *creation* time would claim money moved
  before it did.
- `POST /purchases/payments/bulk`. One draft = one purchase = one payment;
  the bulk endpoint buys nothing.
- Reconciling / updating / deleting existing payments.
- Payments on project expenses (`POST /projects/{id}/expenses` — the smart-me
  and energy-credit-note flows). Those are outgoing revenue, a different
  domain; `/purchases/payments` does not apply.

---

## Testing

Service-level, with the existing in-memory fakes (`FakeMocoPurchases` gains
a `create_payment` recorder) in
`tests/test_supplier_invoice_ocr_service.py`:

1. `already_paid_by_card=True` → one payment POSTed with `purchase_id`,
   `date`, and `total` taken from the create response's `gross_total`.
2. `already_paid_by_card=False` → no payment POSTed.
3. `already_paid_by_card=True` but `is_credit_note=True` → no payment POSTed.
4. create response missing `gross_total` → payment total falls back to
   `invoice.total_amount`.
5. `create_payment` raises `HTTPError(422)` → sync still returns ok,
   `payment_registered` is `False`, Telegram text contains the
   "Zahlung nicht registriert" line.

Wrapper-level in `tests/test_moco_purchase_client.py` (or the existing
purchase-client test module): `create_payment` hits
`/api/v1/purchases/payments` with a POST and the three-field JSON body.

---

## Decisions

- **D1 — one OCR signal, not a heuristic.** The gate is
  `already_paid_by_card` alone. Widening it (missing IBAN, past due date)
  would silently settle genuinely open bills.
- **D2 — server `gross_total` over OCR total.** Avoids rounding-cent
  mismatches that leave a purchase partially paid.
- **D3 — purchase date, not today.** The card receipt's date is the payment
  date; late imports must not drift into the wrong period.
- **D4 — best-effort, one Telegram message.** Consistent with
  `assign_to_project`; the purchase is authoritative and the operator gets
  the failure on the alert they already read.
- **D5 — no pre-flight duplicate check.** The duplicate-purchase 422 already
  guards the realistic replay path.
- **D6 — credit notes excluded.** No live example of a card refund; the sign
  convention is unverified.
