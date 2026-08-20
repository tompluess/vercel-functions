# SPEC: Register a Moco payment for already-paid purchases

Spec for automatically registering a **purchase payment** (German: "Ausgaben
/ Zahlungen") in Moco when a purchase was already settled at the point of
sale — credit card, debit card / EC-Karte, Maestro, Visa, Mastercard, TWINT,
or a POS / EFT terminal slip.

Read `CLAUDE.md` first for project conventions. This feature adds a sixth
webhook endpoint whose downstream is Moco itself (same as
`/api/supplier-invoice-ocr`), reusing the existing
`_handle_moco_dispatch_webhook` plumbing unchanged.

---

## Problem

`SupplierInvoiceOcrService` already detects already-settled card receipts:
`InvoiceData.already_paid_by_card` drives `payment_method="credit_card"`,
suppresses `due_date` / `iban` / `reference`, and makes
`MocoCategoryResolver` decline to guess a Buchhaltungs-Konto.

But the purchase still lands in Moco with `payments: []` — Moco believes the
money is still owed and the purchase shows an open balance. The operator has
to register the payment by hand on every card receipt, which is pure
bookkeeping noise: the receipt itself is proof the money already left the
account. The same applies to card purchases entered by hand in Moco's UI.

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

Source: <https://everii-group.github.io/mocoapp-api-docs/sections/purchase_payments.html>

### Two constraints that shape the whole design

1. **A purchase's `status` is NOT a payment status.** It is `pending`
   (= Inbox) or `archived` (= Archive), nothing else. Registering a payment
   does not change `status`; Moco derives the open balance from
   `sum(payments)` vs `gross_total`. (An earlier draft of this spec claimed
   a payment flips the purchase to `paid` — that was wrong.)

2. **A purchase with a payment can no longer be deleted.** Per the docs,
   `DELETE /purchases/{id}` *"is possible only if the status is `pending`
   and no payments have been registered."*

Constraint 2 is why the payment is registered **after** review rather than
at creation time — see "Trigger" below.

Note there is no idempotency key on `POST /purchases/payments`; a duplicate
POST creates a second payment row and over-settles the purchase. The
`payments == []` gate is what makes this safe (see "Idempotency").

---

## Interaction with the existing review → Bexio flow (verified, unaffected)

The `Review pending` tag workflow is untouched. `BexioExpenseSyncService.sync`
reads only `tags`, `company`, `items[0].category.credit_account`,
`receipt_identifier`, `iban`, `reference`, `date`, `due_date`, `gross_total`,
`title`, `info`, and `user.firstname`. It never reads `payments` or `status`,
so a registered payment cannot change its behaviour. Stripping the tag still
produces a `Purchase:update` with no `Review pending` → the Bexio sync
proceeds exactly as today.

Also already safe: an already-paid card receipt carries no IBAN (the OCR
payload suppresses it), so its Bexio bill goes down the **MANUAL** branch,
which skips book + outgoing-payment silently. No double payment in Bexio.

---

## Design

### 1. Transport — `MocoPurchaseClient.create_payment`

One new method on `api/moco_purchase_client.py` (purchase-domain write, same
auth, same `/purchases` URL space). No new client class — a single endpoint,
pure transport, errors propagate:

```python
def create_payment(self, *, purchase_id: int, date: str,
                   total: float) -> dict:
    """POST /purchases/payments — register a payment against a purchase."""
```

### 2. New endpoint — `POST /api/moco-purchase-payment`

A separate endpoint rather than piggybacking on `/api/bexio-expense-sync`,
for three reasons: that handler returns early on its own skips (`no_company`,
`no_account`, `bill_not_draft`) which have nothing to do with settling a card
receipt; a Bexio outage must not block a Moco-only write; and the repo's
established shape is one endpoint = one job (`/api/supplier-invoice-ocr` is
already a Moco→Moco endpoint, so the precedent exists).

It reuses `_handle_moco_dispatch_webhook` **with no changes to that helper**:

```python
@app.post("/api/moco-purchase-payment")
async def moco_purchase_payment_webhook(request: Request) -> dict[str, Any]:
    return await _handle_moco_dispatch_webhook(
        request,
        required_env=REQUIRED_ENV_PURCHASE_PAYMENT,
        expected_target="Purchase",
        upstream_label="moco",
        build_service=lambda cfg, notifier: MocoPurchasePaymentService(
            purchases=MocoPurchaseClient(
                subdomain=cfg["MOCO_SUBDOMAIN"],
                api_key=cfg["MOCO_API_KEY"],
            ),
            subdomain=cfg["MOCO_SUBDOMAIN"],
            telegram=notifier,
        ),
    )
```

`REQUIRED_ENV_PURCHASE_PAYMENT = ["MOCO_WEBHOOK_SECRET", "MOCO_SUBDOMAIN",
"MOCO_API_KEY", *REQUIRED_ENV_TELEGRAM]` — no new secrets, all four already
exist in the Vercel project.

**Operator step:** a new Moco webhook on `Purchase` / `create`+`update`
pointing at this path, with `x-moco-target: Purchase`.

### 3. Service — `MocoPurchasePaymentService`

New file `api/moco_purchase_payment_service.py`, one class (per
`feedback_one_class_per_file`), exposing `sync(body) -> dict` like every other
dispatch service.

### 4. Gate

Register a payment only when **all** hold:

1. `payment_method == "credit_card"` — the marker for "already settled".
   The OCR flow sets it from `already_paid_by_card`; hand-entered card
   purchases carry it directly. Deliberately not widened to "no IBAN" or
   "due date passed": a bill with no IBAN is a MANUAL-transfer bill, not a
   paid one, and mis-settling an open bill is worse than leaving a settled
   one open (the operator sees the latter; the former silently disappears
   from the "was ist offen" view).
2. `payments == []` — nothing registered yet. This is the idempotency guard.
3. `"Review pending"` **not** in `tags` — while the OCR result is unreviewed
   the amount may still change and the operator may want to delete the
   purchase outright (constraint 2 above). Reuses the same case-insensitive,
   whitespace-trimmed match as `BexioExpenseSyncService._has_review_pending_tag`;
   the helper moves to a shared location so both endpoints use one
   implementation. Hand-entered purchases have no such tag and pass
   immediately.
4. `gross_total` is present and `> 0`. Zero is nothing to settle; **negative**
   means a credit note / refund, where the sign convention for a payment is
   unverified and no live example exists — skipped deliberately.

Each failed gate returns a distinct silent skip (`{"skipped": "..."}`, INFO
log, no Telegram) so Moco ACKs 200 and stops retrying. These fire on most
`Purchase` webhooks — the endpoint is quiet by design.

### 5. Field mapping

| Moco payment field | value              |
|--------------------|--------------------|
| `purchase_id`      | `body["id"]`       |
| `date`             | `body["date"]`     |
| `total`            | `body["gross_total"]` |

**`total` uses the webhook's `gross_total`**, i.e. Moco's own server-side
figure, not an OCR value. Moco recomputes gross from the line item + VAT
code, and the open balance is `sum(payments)` vs `gross_total`, so any other
number leaves a rounding-cent residual. Registering after review means this
is the amount the operator actually approved.

**`date` is the purchase date, not today.** For a card receipt the document
date *is* the payment date. Using today would mis-date the payment into a
later accounting period whenever a receipt is imported or reviewed late.

### 6. Failure posture — best-effort

The purchase is the authoritative record; a failed payment registration must
not break anything:

- `HTTPError` with 4xx → tidy `logger.warning` with status + truncated body
  (per `feedback_soft_failure_logging` — no traceback), Telegram alert with
  the Moco purchase deep-link, return `{"skipped": "payment_failed", ...}`
  so Moco ACKs 200 and doesn't retry.
- `HTTPError` 5xx / `URLError` → propagate; `_handle_moco_dispatch_webhook`
  maps them to 502 and Moco retries. Safe to retry: the `payments == []`
  gate re-evaluates on the retry.

### 7. Result payload

```python
{"payment_id": int, "purchase_id": int, "total": float}   # registered
{"skipped": "not_card_payment" | "already_paid" | "review_pending"
            | "no_amount" | "payment_failed"}
```

### 8. Idempotency

The `payments == []` gate is the guard, and it is evaluated against the
webhook body Moco just sent. A webhook replay, a second unrelated
`Purchase:update` (e.g. the operator edits the title), or a 502 retry all
re-read `payments` and find the existing row → `{"skipped": "already_paid"}`.

The one true race is two `Purchase:update` webhooks delivered concurrently
for the same purchase, both observing `payments: []`. Accepted: Moco
serialises webhook delivery per entity in practice, the operator sees a
doubled payment immediately in the purchase's balance, and defending against
it would need a `GET /purchases/payments?purchase_id=…` round-trip on every
card purchase.

### 9. Telegram

One message on successful registration, so the operator can see settlements
happening without opening Moco:

```
💳 Zahlung erfasst — <title> CHF <total>
Bereits per Karte bezahlt: <purchase link>
```

Silent on every gate skip (they fire constantly). Alert on registration
failure only, per §6.

---

## Non-goals

- Payments for bank-transfer purchases. Those are settled by the actual bank
  transfer, which the Bexio outgoing-payment flow already initiates.
- Credit notes / refunds (negative `gross_total`) — sign convention unverified.
- `POST /purchases/payments/bulk`; reconciling, updating, or deleting existing
  payments.
- Payments on project expenses (the smart-me and energy-credit-note flows).
  Those are outgoing revenue; `/purchases/payments` does not apply.
- Partial payments.

---

## Testing

Service-level (`tests/test_moco_purchase_payment_service.py`) with an
in-memory `FakeMocoPurchases` + `FakeTelegram`:

1. card purchase, no payments, no review tag → one `create_payment` with
   `purchase_id` / `date` / `total` from the body; Telegram fired.
2. `payment_method="bank_transfer"` → `not_card_payment`, no call.
3. `payments: [{...}]` already present → `already_paid`, no call.
4. `tags: ["OCR", "Review pending"]` on a card purchase → `review_pending`,
   no call.
5. `gross_total` missing / `0` / negative → `no_amount`, no call.
6. `create_payment` raises `HTTPError(422)` → `payment_failed`, sync returns
   normally, Telegram alert fired.
7. tag match is case-insensitive + trimmed (`" review PENDING "`).

Endpoint-level (`tests/test_endpoint.py` or a new module), with
`urlopen` stubbed as elsewhere: signature/target/event rejections behave like
the sibling endpoints, and a valid card-purchase body returns
`{"ok": true, "event": "update", "payment_id": ...}`.

Wrapper-level (`tests/test_moco_purchase_client.py`): `create_payment` POSTs
to `/api/v1/purchases/payments` with the three-field JSON body.

---

## Decisions

- **D1 — register after review, not at creation.** `DELETE /purchases/{id}`
  is refused once a payment exists, so registering at create time would make
  every OCR'd card receipt undeletable exactly during the review window when
  binning a bad OCR result is most likely. It would also leave the payment
  total stale if the operator corrects the amount.
- **D2 — `payments == []` as the idempotency guard.** Free (already in the
  webhook body), and correct across replays, retries, and unrelated updates.
- **D3 — its own endpoint, not a step in bexio-expense-sync.** That handler's
  early-return skips are unrelated to settling a receipt, and a Bexio outage
  must not block a Moco-only write.
- **D4 — fires for any card purchase, not just OCR-created ones.** Matches
  the intent ("if expenses are created that are already paid"); hand-entered
  card purchases have the same manual-settlement chore. The `Review pending`
  gate still protects the OCR path.
- **D5 — `payment_method == "credit_card"` is the only signal.** Widening it
  (missing IBAN, past due date) would silently settle genuinely open bills.
- **D6 — server `gross_total`, not an OCR figure.** The open balance is
  computed against it; anything else leaves a residual.
- **D7 — purchase date, not today.** The receipt's date is the payment date;
  late review must not drift the payment into the wrong period.
- **D8 — negative totals excluded.** No live example of a card refund and the
  payment sign convention is unverified.
