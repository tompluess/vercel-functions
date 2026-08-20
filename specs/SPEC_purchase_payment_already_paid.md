# SPEC: Auto-release resolved purchases + register payments for already-paid ones

Two coupled changes to `SupplierInvoiceOcrService`, no new endpoint:

1. **Conditional review tag** — stamp `Review pending` only when the OCR
   result could *not* be fully resolved automatically. Fully-resolved
   purchases are released straight to the existing Bexio expense sync.
2. **Automatic purchase payment** — when a created purchase was already
   settled at the point of sale (card / TWINT / POS terminal), register a
   Moco purchase payment right after `POST /purchases`, so the purchase
   doesn't sit in Moco with a phantom open balance.

Read `CLAUDE.md` first. Everything below lives inside the existing
`/api/supplier-invoice-ocr` flow and its collaborators.

---

## Problem

**Payments.** `InvoiceData.already_paid_by_card` already drives
`payment_method="credit_card"` and suppresses `due_date` / `iban` /
`reference`. But the purchase lands with `payments: []`, so Moco shows an
open balance on money that already left the account. The operator settles
every card receipt by hand.

**Review.** Every OCR'd purchase is stamped `["OCR", "Review pending"]`, and
`BexioExpenseSyncService` refuses to sync until a human strips the tag. When
OCR resolved everything — supplier company matched, expense account
determined — that review is busywork.

---

## Moco API (confirmed against the official docs)

Purchase payments are a **top-level** collection, not a sub-resource:

```
GET/POST  /api/v1/purchases/payments
POST      /api/v1/purchases/payments/bulk
GET/PUT/DELETE /api/v1/purchases/payments/{id}
```

`POST /purchases/payments` fields:

| field         | required | notes                                                       |
|---------------|----------|-------------------------------------------------------------|
| `date`        | yes      | `"2018-10-20"`                                              |
| `total`       | yes      | e.g. `1000` — the gross amount paid                         |
| `purchase_id` | no*      | \*required in our case; the alternative is a `description`   |
| `description` | no       | only valid when `purchase_id` is **not** set — not our case |

Source: <https://everii-group.github.io/mocoapp-api-docs/sections/purchase_payments.html>

Two facts that shape the design:

1. **A purchase's `status` is NOT a payment status.** It is `pending`
   (= Inbox) or `archived` (= Archive). Registering a payment does not
   change it; Moco derives the open balance from `sum(payments)` vs
   `gross_total`.
2. **A purchase with a payment can no longer be deleted.** Per the docs,
   `DELETE /purchases/{id}` *"is possible only if the status is `pending`
   and no payments have been registered."* Accepted cost — see D5.

---

## Design

### 1. Transport — `MocoPurchaseClient.create_payment`

One new method on `api/moco_purchase_client.py` (purchase-domain write, same
auth, same URL space). Pure transport, errors propagate:

```python
def create_payment(self, *, purchase_id: int, date: str,
                   total: float) -> dict:
    """POST /purchases/payments — register a payment against a purchase."""
```

### 2. Review-tag decision

`OCR_TAGS` stops being a constant list. The `OCR` tag is always applied
(it's the operator's filter for machine-created purchases); `Review pending`
becomes conditional.

A purchase is **auto-released** (no `Review pending` tag) only when *all*
hold:

| condition | rationale |
|---|---|
| `company_id` is not None | `MocoSupplierMatcher` produced a unique tiered match. Also means Bexio's own `no_company` gate can't fire downstream. |
| the category is *trusted* (see below) | An expense account was determined by a route we're willing to skip review on. Also means Bexio's `no_account` gate can't fire. |
| `invoice.confidence >= AUTO_RELEASE_CONFIDENCE` (0.90) | **My addition — see D2.** The only signal that speaks to whether the *amount* and *IBAN* were read correctly. |
| `not invoice.is_credit_note` | Credit notes always need a human to check the sign; today's Telegram alert says so unconditionally. |

Anything else keeps `Review pending` and behaves exactly as today.

**Tags.** `OCR_TAGS` is replaced by three constants — `OCR_TAG = "OCR"`,
`REVIEW_PENDING_TAG = "Review pending"`, `AUTO_TAG = "Auto"` — assembled per
purchase:

| outcome | tags |
|---|---|
| auto-released | `["OCR", "Auto"]` |
| held for review | `["OCR", "Review pending"]` |
| credit note (always held) | `["OCR", "Review pending", "Gutschrift"]` |

The `Auto` tag exists so Moco's UI can filter everything that bypassed human
review — the spot-check list once this is live. Without it, an auto-released
purchase is indistinguishable from one a human reviewed and cleared (both are
just `["OCR"]`). Nothing downstream reads it; `BexioExpenseSyncService` looks
only for `Review pending` (D11).

**Confidence constant.** `AUTO_RELEASE_CONFIDENCE = 0.90` is a *new* constant,
deliberately separate from the existing `CONFIDENCE_THRESHOLD = 0.85`. The two
do different jobs — one picks a Telegram emoji, the other can let money move —
so tuning alert noise must not silently retune the money gate. It starts
stricter than the alert bar; lower it once the batch preview (§8) shows the
real distribution (D10).

**The already-paid category rule is unchanged and load-bearing.**
`MocoCategoryResolver`'s chain is project `Aufwandkonto` → supplier
`Aufwandkonto` → **already-paid ⇒ omit** → `4000` default. For a card
receipt the chain short-circuits *before* the default, so `category_id` is
non-None only when an explicit `Aufwandkonto` override exists. No resolver
change is needed. This is deliberate: a card receipt must never be
auto-released on the strength of a guessed 4000 booking.

It also gives the operator a direct lever — set `Aufwandkonto` on a frequent
card supplier (SBB, a fuel card, a hardware shop) and its receipts start
auto-releasing; leave it unset and they keep going through review.

`SupplierInvoiceOcrService._resolve_category_id` currently returns a bare
`int | None`; it changes to return the full `CategoryDecision` (which already
carries `.source` ∈ `project` / `supplier` / `default` / `already_paid`) so
the tag decision can be logged with its reason.

**The policy itself lives in a new pure collaborator**, `api/purchase_review_gate.py`
— one class per file, same shape as `MocoCategoryResolver` /
`MocoSupplierMatcher`: no I/O, fully unit-testable, and reusable by the two
operator scripts that already hold every input it needs (D9).

```python
@dataclass(frozen=True)
class ReviewDecision:
    review_pending: bool
    reasons: list[str]   # operator-facing, e.g. ["keine Firma", "Konfidenz 0.72"]
    tags: list[str]      # the final tag list for the purchase

class PurchaseReviewGate:
    def __init__(self, *, min_confidence: float = AUTO_RELEASE_CONFIDENCE): ...
    def evaluate(self, *, invoice: InvoiceData, company_id: int | None,
                 category: CategoryDecision,
                 project_match: ProjectMatch | None) -> ReviewDecision: ...
```

`project_match` is needed only to read `.tier` for the `source="project"`
rule above. Both operator scripts already hold one.

`reasons` is what makes a held purchase self-explanatory in the log, the
Telegram message, and the batch script's column — the operator sees *which*
condition held it, not just that it was held. Having the gate own `tags` keeps
the assembly in one place; `_build_create_payload` takes the finished list
rather than re-deriving policy.

**Which categories are trusted (D1).** `category_id is not None` alone is not
enough — the route that produced it matters:

| `CategoryDecision.source` | trusted for auto-release? |
|---|---|
| `"supplier"` | **yes** — the supplier was matched by name off the receipt itself, and the `Aufwandkonto` is deliberate operator config. |
| `"project"` | **only** when the project matched at the `exact` or `substring` tier. |
| `"default"` (the 4000 fallback) | **yes** — but see the note below; this is unreachable for card receipts. |
| `"already_paid"` | never — `category_id` is None by construction. |

The project restriction exists because `MocoProjectResolver`'s loosest tier is
`token-overlap`, where *any single shared token* counts as a match — loose
enough that `SmartmeProjectMatcher` was written specifically to avoid it. The
`Aufwandkonto` config is equally deliberate in both cases; it's the *match*
selecting the project that's weaker. `ProjectMatch.tier` already reports
which tier fired, so the gate reads it directly.

An override that was **set but missed the catalog** yields `category_id is
None` with `source="project"`/`"supplier"` — correctly held, since an explicit
account that didn't map is exactly the case a human should see.

**The gate needs no payment-method branch.** `MocoCategoryResolver` returns
`source="already_paid"` (and `category_id=None`) for card receipts *before*
reaching the 4000 default, so a card receipt can never present as
`source="default"`. Card-receipt strictness falls out of the existing resolver
chain rather than a special case in the gate — which is why the resolver needs
no change at all.

### 3. Payment registration

Immediately after a successful `POST /purchases`, in the existing
post-create block of `process()`:

```
if new_purchase_id:
    self._post_summary_comments(...)
    assign_warnings = self._assign_resolved_project(...)
    payment_result = self._register_payment_if_already_paid(created, invoice)
    self._delete_draft_after_create(...)
```

After `assign_to_project` (which mutates line items) and before the draft
delete — the payment is a leaf write that shouldn't sit between item
mutations.

**Gate** — register only when all hold:

1. `invoice.already_paid_by_card` — the single "already settled" signal.
   Deliberately not widened to "no IBAN" or "due date passed": a bill
   without an IBAN is a MANUAL-transfer bill, not a paid one, and
   mis-settling an open bill is worse than leaving a settled one open (you
   see the latter; the former silently vanishes from "was ist offen").
2. `not invoice.is_credit_note` — a card refund is conceivable but the
   payment sign convention is unverified and there is no live example.
3. A positive gross amount is resolvable.

Applies to **every** already-paid receipt, auto-released or not — the chore
is the same either way. See D5 for the cost when a reviewed receipt's amount
is later corrected.

**Field mapping:**

| Moco payment field | value |
|---|---|
| `purchase_id` | `created["id"]` |
| `date` | `created["date"]` (the purchase date Moco stored) |
| `total` | `created["gross_total"]`, falling back to `invoice.total_amount` |

`total` prefers the **create response's** `gross_total` — Moco's own
server-side figure, recomputed from the line item + VAT code. The open
balance is measured against it, so an OCR-derived number risks a
rounding-cent residual. `date` is the purchase date, not today: for a card
receipt the document date *is* the payment date, and using today would
mis-date the payment into a later period on a late import.

### 4. Failure posture — best-effort

The created purchase is authoritative; a failed payment must not fail the
sync. Mirrors `_assign_resolved_project`:

- `HTTPError` → tidy `logger.warning` with status + truncated body (per
  `feedback_soft_failure_logging`, no traceback), collect a warning string.
- any other `Exception` → `logger.exception`, collect a warning string.
- the warning is appended to the existing Telegram outcome message as a line
  (`⚠️ Zahlung nicht registriert: HTTP 422 …`) — one message per draft stays
  the rule — and `process()` still returns ok.

### 5. Telegram wording

Auto-release makes the Telegram alert the **only** human touchpoint for
those purchases, so the message must stop saying "bitte prüfen":

```
✅ OCR erfolgreich (94%) — Digitec CHF 249.00
Automatisch freigegeben (Firma + Konto erkannt) → Bexio: <link>
💳 Zahlung erfasst (bereits per Karte bezahlt)
```

vs. today's wording, unchanged, when `Review pending` was applied. The
credit-note and low-confidence branches are untouched.

### 6. Result payload

`process()`'s return dict gains:

```python
"review_pending": bool      # was the tag applied
"review_reasons": list[str] # why it was held (empty when auto-released)
"payment_registered": bool  # did POST /purchases/payments succeed
```

### 8. Batch-script preview column (same PR)

`scripts/batch_ocr_drafts.py` gains a `REVIEW` column showing what the gate
*would* decide for each historical draft, plus the reasons when held. The
script already resolves the supplier match and calls
`category_resolver.resolve()` itself (line ~512), so this is one
`PurchaseReviewGate.evaluate()` call on data it already has — and because it
calls the same collaborator the service does, the preview cannot drift from
the real rule (D12).

```
DRAFT   SUPPLIER      KATEGORIE   KONF   REVIEW
3143995 CKW AG        6500(supp)  0.94   AUTO
3143993 Digitec       4000(dflt)  0.91   AUTO
3144001 Brack.ch      -           0.88   HOLD (kein Konto, Konfidenz)
3144007 Unknown GmbH  4000(dflt)  0.72   HOLD (keine Firma, Konfidenz)
```

This is the rollout gate: run it over recent drafts and read the column
**before** enabling auto-release in production, so the historical hit rate is
known rather than discovered.

### 7. Idempotency

Unchanged from today: a webhook replay re-runs `POST /purchases`, hits
`receipt_identifier: ["ist bereits vergeben"]` (422), and returns
`skipped: "moco_rejected"` before reaching the payment step. Receipt-number-
less drafts would create a duplicate purchase *and* a duplicate payment, but
the duplicate purchase is the louder problem and already exists today. No
pre-flight `GET /purchases/payments?purchase_id=…` — it costs a round-trip
on every card receipt to defend a case that already implies a duplicate
purchase.

---

## Risk accepted by auto-release

Stated plainly, because this is the substantive change: a **bank-transfer**
bill that is auto-released reaches `BexioExpenseSyncService` with no human in
the loop, and that service does not stop at creating a bill — it books it
DRAFT→BOOKED and creates an **outgoing payment**. So an OCR error on the
amount or IBAN of an auto-released transfer bill can queue real money to
move.

The three conditions in §2 are the mitigation: a unique supplier match, an
explicitly configured expense account, and the model's own ≥85% confidence.
Company and category resolution say nothing about the amount — that is
exactly why the confidence condition is there (D2).

Already-paid card receipts carry none of this risk: the money is already
gone, and with no IBAN `_try_book_and_pay` returns early
(`bexio_expense_sync_service.py:273`), so their Bexio bill is created as
**DRAFT and never booked** — no outgoing payment, and you still book it in
Bexio by hand. Auto-releasing a card receipt therefore cannot move money; it
only saves the Moco review click. The confidence condition still applies to
them anyway (D2).

Rollout: run `scripts/batch_ocr_drafts.py` over recent drafts first and read
the new `REVIEW` column (§8) — it shows exactly which historical invoices
*would* have auto-released, before any of them can.

---

## Non-goals

- Payments for bank-transfer purchases (settled by the actual transfer,
  which the Bexio outgoing-payment flow already initiates).
- Credit notes / refunds — sign convention unverified.
- `POST /purchases/payments/bulk`; updating, reconciling, or deleting
  existing payments; partial payments.
- Payments on project expenses (smart-me / energy-credit-note flows) —
  outgoing revenue, different domain.
- A post-review payment path. A receipt that goes through review gets its
  payment at creation like any other (D5).

---

## Testing

Service-level, existing in-memory fakes in
`tests/test_supplier_invoice_ocr_service.py`
(`FakeMocoPurchases` gains a `create_payment` recorder):

*Payments*
1. `already_paid_by_card=True` → one `create_payment` with `purchase_id` /
   `date` / `total` from the create response's `gross_total`.
2. `already_paid_by_card=False` → no call.
3. `already_paid_by_card=True` + `is_credit_note=True` → no call.
4. create response missing `gross_total` → falls back to
   `invoice.total_amount`.
5. `create_payment` raises `HTTPError(422)` → sync still ok,
   `payment_registered` False, Telegram contains "Zahlung nicht registriert".

*Review tag (service level — the wiring)*
6. company + category + confidence 0.95 → tags are `["OCR", "Auto"]`,
   `review_pending` False, Telegram says "Automatisch freigegeben".
7. confidence 0.88 with both resolved → `Review pending` applied — the
   regression test for `AUTO_RELEASE_CONFIDENCE` being 0.90, *not* the 0.85
   alert threshold (D10).
8. already-paid receipt whose supplier has no `Aufwandkonto` → category
   None → `Review pending` applied (guards the 4000-default rule, D1).
9. already-paid receipt whose supplier *has* an `Aufwandkonto` → category
   resolved → auto-released **and** payment registered.

*Gate (unit level — the policy), `tests/test_purchase_review_gate.py`*
10. all four conditions met → `review_pending` False, `tags == ["OCR", "Auto"]`,
    `reasons` empty.
11. each condition failing on its own → held, with that condition named in
    `reasons` (four cases: no company, untrusted category, low confidence,
    credit note).
12. multiple failures → all named in `reasons`.
13. credit note with everything else resolved → held, `Gutschrift` tag still
    appended.
14. **category source matrix (D1)** — `source="supplier"` → released;
    `source="default"` → released; `source="project"` with
    `tier="exact"` → released; `source="project"` with
    `tier="substring"` → released; `source="project"` with
    `tier="token-overlap"` → **held**; `source="already_paid"`
    (`category_id=None`) → held.
15. override set but unmapped (`category_id=None`, `source="supplier"`) → held.

Wrapper-level (`tests/test_moco_purchase_client.py`): `create_payment` POSTs
to `/api/v1/purchases/payments` with the three-field JSON body.

Script-level: `scripts/batch_ocr_drafts.py` is exercised by hand over real
drafts as the rollout gate (§8), not unit-tested — consistent with the other
operator scripts.

---

## Decisions

- **D1 — a category is trusted by *source*, not just by being set.**
  `supplier` always; `project` only on an `exact`/`substring` project match
  (never `token-overlap`, the loosest matcher in the pipeline); `default`
  (4000) yes, which is unreachable for card receipts because the resolver
  short-circuits at `already_paid` first. Consequence, accepted knowingly: for
  a **bank-transfer** bill the 4000 fallback nearly always resolves, so the
  category condition rarely bites there and the effective gate is company +
  confidence. Card receipts stay strict — they auto-release only when their
  supplier (or a strongly-matched project) carries an explicit `Aufwandkonto`.
  The resolver itself needs no change.
- **D2 — confidence is part of the auto-release gate, uniformly.** Company and
  category resolution are orthogonal to whether the *amount* was read
  correctly; confidence is the only signal that covers it, and auto-release
  for transfer bills can move money. Considered and rejected: dropping the
  condition for already-paid receipts specifically, on the grounds that they
  cannot move money (see the risk section). Rejected in favour of one uniform
  rule — no payment-method branch in the gate, one number to reason about and
  to test. A card receipt at 0.88 with a supplier `Aufwandkonto` is held.
- **D9 — the gate is a pure collaborator, not a private method.** Both
  operator scripts need the same policy to preview it, and a duplicated copy
  in the batch script would drift from the real rule — defeating the point of
  the preview. Also keeps the 1457-line service from taking a fourth job.
- **D10 — `AUTO_RELEASE_CONFIDENCE` is separate from `CONFIDENCE_THRESHOLD`.**
  Different jobs (emoji vs. money), so they must be tunable independently.
  Starts at 0.90, stricter than the 0.85 alert bar, pending the batch preview.
- **D11 — auto-released purchases carry an `Auto` tag.** Otherwise they're
  indistinguishable in Moco's UI from purchases a human reviewed and cleared,
  and there'd be no way to audit what bypassed review.
- **D12 — the batch preview column ships in the same PR.** It's the rollout
  gate; shipping auto-release without it means enabling the behaviour before
  its historical hit rate is known.
- **D3 — payment registered at creation, in the OCR service.** No new
  endpoint, no new Moco webhook, no new env vars.
- **D4 — `payment_method == "credit_card"` (via `already_paid_by_card`) is
  the only settle signal.** Widening it would silently settle open bills.
- **D5 — accepted cost of registering at creation.** The purchase becomes
  undeletable (delete the payment first — two clicks), and if a *reviewed*
  receipt's amount is corrected the payment total goes stale and shows a
  residual. The alternative — registering only for auto-released purchases —
  was rejected as a confusing split behaviour that leaves the manual chore
  in place for exactly the receipts you're already handling by hand.
- **D6 — server `gross_total`, not the OCR figure.** The open balance is
  computed against it; anything else leaves a residual.
- **D7 — purchase date, not today.** The receipt's date is the payment date.
- **D8 — negative totals excluded.** No live example of a card refund.
