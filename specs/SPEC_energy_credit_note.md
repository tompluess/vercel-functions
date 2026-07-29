# SPEC: Energy credit notes (Stromproduktion Gutschriften)

Spec for turning an incoming EVU (local energy supplier) production credit
note into a Moco project expense + Moco invoice, instead of the generic
OCR→Purchase path.

Read `CLAUDE.md` first for project conventions and the existing
`SupplierInvoiceOcrService` / `SmartmeEnergyExpenseService` flows — this
feature reuses their architecture (matcher/resolver pattern, optional
injected collaborators, best-effort side effects, `status` vocabulary
`matched`/`ambiguous`/`no_match`/`empty`).

---

## Problem

Quarterly statements from local energy suppliers (EVUs — e.g. CKW) land in
the same `Purchase::Draft` inbox as regular supplier invoices. These
statements combine:

- a small **consumption invoice** ("Energiebezug" / "Rechnung" /
  "Eigenbedarf") — PVcontracting buying grid power, and
- a much larger **production credit** ("Rücklieferung" / "Gutschrift" /
  "Einspeisung") — the EVU paying PVcontracting for electricity fed back
  into the grid.

Today these get OCR'd like any other document and turned into a Moco
`Purchase` — wrong: the production-credit portion is PVcontracting's own
outgoing revenue, which the operator has been booking by hand every quarter
as a project expense + a Moco invoice back to the EVU (7 such invoices exist
per Stromproduktion project already, e.g. `R26036` / invoice id `7812757`
on the `Meierhofweg10_Emmen Contracting/Einspeisung` project — see
"Real example" below).

---

## Real example (verified against live Moco data)

**Draft `3143995`**: a CKW AG PDF titled `260731 CKW Meierhofweg10 Rechnung
600 949 594.pdf`. The PDF's 4 pages:

1. Summary: "Ihre Gutschrift" — CHF 3'785.65, netting page 3 against page 4.
2. Stromkennzeichnung (regulatory disclosure, irrelevant).
3. **Consumption** section: Objekt "Eigenbedarf PVA HEIV Meierhofweg 10",
   Abrechnungszeitraum 01.04.2026–30.06.2026, Rechnungsbetrag CHF 84.94
   (incl. MWST), Nettobetrag CHF 78.58.
4. **Production credit** section: Objekt "Produktion PVA HEIV Meierhofweg
   10", same Abrechnungszeitraum, Vergütung 91'904 kWh @ 0.03896,
   **Nettobetrag CHF 3'580.58**, MWST 8.1% CHF 290.03, Gutschriftsbetrag
   incl. MWST CHF 3'870.61.

Only page 4's figures matter for this feature. The top-level "Ihre
Gutschrift CHF 3'785.65" nets both sections together and must **not** be
used as the bookable amount.

**Precedent invoice** (`GET /invoices/7812757`, `Meierhofweg10_Emmen
Contracting/Einspeisung` project, quarter 2026/Q1):

```json
{
  "customer_id": 762378092,
  "project_id": 947264448,
  "title": "Stromproduktion 2026/Q1 – Meierhofweg10_Emmen Contracting/Einspeisung",
  "date": "2026-05-18", "due_date": "2026-06-17",
  "service_period": "01 – 03/2026",
  "service_period_from": "2026-01-01", "service_period_to": "2026-03-31",
  "recipient_address": "CKW AG\nTäschmattstrasse 4\n6015 Luzern\nSchweiz",
  "currency": "CHF", "net_total": 3301.0, "tax": 8.1,
  "vat": {"id": 107816, "tax": 8.1},
  "tags": ["Stromproduktion"],
  "items": [{
    "type": "item",
    "title": "Stromproduktion 2026/Q1 (01 – 03/2026)",
    "quantity": 1.0, "unit": "x", "unit_price": 3301.0, "net_total": 3301.0,
    "service_type": "expense", "expense_ids": [5187397]
  }]
}
```

And the linked project expense (`GET /projects/947264448/expenses/5187397`):

```json
{
  "date": "2026-05-18", "title": "Stromproduktion 2026/Q1",
  "quantity": 1.0, "unit": "x", "unit_price": 3301.0, "unit_cost": 0.0,
  "budget_relevant": true, "billable": true, "billed": true,
  "invoice_id": 7812757,
  "service_period": "01 – 03/2026",
  "service_period_from": "2026-01-01", "service_period_to": "2026-03-31",
  "company": {"id": 762378092, "name": "CKW AG"}
}
```

(`budget_relevant: true` here is an outlier — 5 of 6 historical
Stromproduktion expenses have `budget_relevant: false`; this feature uses
`false`, matching the majority and the original ask.)

**The two-company-records finding**: the project's `customer` is company
`762378092` "CKW AG" (`type: "customer"`), while the OCR'd sender matches
a *different* company `762378104` "CKW AG (Lieferant)" (`type: "supplier"`)
via the existing `MocoSupplierMatcher`/`list_suppliers()`. **Both** carry
the tag `Lokaler Energieversorger (EVU)`. Project lookup must therefore go
through **name matching**, never company-id equality.

**Candidate projects** (tag `Stromproduktion` + customer name "CKW AG"):
`Meierhofweg10_Emmen Contracting/Einspeisung`, `Lindershalde_Rengg
Contracting/Einspeisung`, `Krugel1_Oberkirch - Contracting/Einspeisung` —
disambiguated from the OCR'd Objekt by address token overlap (e.g.
"Meierhofweg" + "10").

---

## Design

### 1. OCR schema

New `EnergyCreditNoteData` dataclass + `extract_energy_credit_note()` on
`AnthropicOcrClient`, mirroring `EnergyBillData`/`extract_energy_bill`.
Fields: `objekt`, `net_amount`, `vat_rate`, `period_from`, `period_to`,
`invoice_date`, `invoice_number`, `confidence`. The prompt explicitly warns
about the two-section-per-PDF shape and instructs the model to extract only
the production/credit section (see "Real example" above).

### 2. Detection

No cheap pre-download signal exists here (unlike smart-me: the draft's
`title` is just a PDF filename, `email_from`/`email_body` are often null).
Detection instead reuses the **general** OCR pass and supplier lookup that
`SupplierInvoiceOcrService.process()` already runs for every draft:

```python
EVU_TAG = "Lokaler Energieversorger (EVU)"

def is_energy_credit_note(invoice: InvoiceData, company: dict | None) -> bool:
    if not invoice.is_credit_note or company is None:
        return False
    tags = {str(t).casefold() for t in (company.get("tags") or [])}
    return EVU_TAG.casefold() in tags
```

The tag check alone is not sufficient in practice — see **D4** below: a
second, independent signal (`EnergyCreditNoteService.has_matching_project`)
is OR'd in at both call sites so a missing/incomplete tag doesn't drop a
real credit note.

Hooked in right after the existing `company = self._fetch_company(company_id)`
step, before VAT/category resolution for the normal purchase path.

### 3. Project matching — `StromproduktionProjectMatcher`

Modeled on `SmartmeProjectMatcher`, with an extra required filter tier:

- Indexes only projects tagged `Stromproduktion`.
- **Tier 0** (pin): `Kommission` custom-property equality against the OCR'd
  `objekt` (reuses `_normalize`/`_tokens` from `moco_project_resolver.py`).
- **Tier 1** (required filter): `project.customer.name` normalized-matches
  the OCR'd `invoice.supplier_name` (fold/alnum/token-set style, mirroring
  `moco_supplier_matcher.py`'s normalization).
- **Tier 2** (disambiguation): token-overlap between the OCR'd `objekt` and
  the project `name` within the tier-1-filtered candidate set
  (`MIN_TOKEN_LEN=1`, house numbers must survive).
- Zero tier-1 candidates (an EVU with no Stromproduktion project yet) →
  `no_match`, never a fallback across all Stromproduktion projects — an
  unrelated EVU's credit must never land on someone else's project.

### 4. Moco invoice API (confirmed via the OpenAPI spec)

- `POST /invoices` — `status` defaults when omitted; explicit `"created"`
  is a real (non-draft) invoice, not yet sent. `items[].expense_ids` links
  the item to an existing project expense — Moco marks that expense
  `billed: true` and sets its `invoice_id` automatically.
- `POST /invoices/{id}/attachments` — separate call, base64 JSON body
  (unlike purchases/expenses, the attachment is not embedded in create).
- `PUT /invoices/{id}/update_status` exists but is **not used** — the
  invoice is deliberately left at `status: "created"`; sending is a manual
  step the operator performs later in the Moco UI. (Decision below.)

### 5. Field mapping

| Field | Source |
|---|---|
| Expense `title` | `"Stromproduktion {leistungszeitraum}"` (e.g. `"Stromproduktion 2026/Q2"`) |
| Expense `unit` | `"x"` (decision below) |
| Expense `quantity` | `1` |
| Expense `unit_price` | `credit.net_amount` |
| Expense `unit_cost` | `0` |
| Expense `billable` | `true` |
| Expense `budget_relevant` | `false` |
| Expense `service_period_from/to` | `credit.period_from` / `credit.period_to` |
| Expense attachment | source PDF |
| Invoice `customer_id` / `recipient_address` | `project.customer.id` / `project.billing_address` |
| Invoice `project_id` | matched project id |
| Invoice `title` | `"Stromproduktion {leistungszeitraum} – {project.name}"` |
| Invoice `date` | today |
| Invoice `due_date` | today + 30 days (matches precedent) |
| Invoice `currency` | `"CHF"` |
| Invoice `tags` | `["Stromproduktion"]` |
| Invoice `vat_code_id` | OCR `vat_rate` matched against `GET /vat_code_sales`, else the entry with `tax == 8.1` (account standard, matches every precedent), else the first active entry |
| Invoice item | `type="item"`, `title="Stromproduktion {leistungszeitraum} ({from.month:02d} – {to.month:02d}/{to.year})"`, `quantity=1`, `unit="x"`, `unit_price=credit.net_amount`, `expense_ids=[expense_id]` |
| Invoice attachment | same source PDF |
| `leistungszeitraum` | derived, not OCR'd: `f"{period_from.year}/Q{(period_from.month-1)//3+1}"` |

### 6. Failure paths

Mirrors `SmartmeEnergyExpenseService._keep_draft` exactly: project
`no_match`/`ambiguous`/`empty`, or missing `net_amount`/period → comment on
the draft (`commentable_type="PurchaseDraft"`) + Telegram alert, draft
**stays** in the inbox (not deleted), webhook ACKs `ok=true`. Expense/
invoice/attachment `HTTPError`s are **not** internally swallowed (same
posture as `SmartmeEnergyExpenseService.create_project_expense` — no known
routine 4xx here) and propagate to `index.py`'s existing 4xx→200-ok=false /
5xx→502 mapping.

On success: best-effort draft delete (404-idempotent) + Telegram summary
with a direct link to the new invoice, explicitly noting it still needs to
be reviewed and manually set to "sent" in the Moco UI.

---

## Decisions

**D1 — Expense `unit`: `"x"`, not `"Netto"`.** The original ask specified
`"Netto"` (the unit smart-me's *Eigenverbrauch* flow uses), but all 6
historical Stromproduktion expenses in the live Moco account use `"x"`.
Confirmed with the operator: use `"x"` to stay consistent with every past
quarter on these projects.

**D2 — Invoice stays at `status: "created"`, never auto-transitioned to
`"sent"`.** The original ask described marking the invoice "versendet"
(sent) without emailing it, which would have cascaded automatically into a
Bexio invoice via the existing `bexio-invoice-sync` webhook (`Invoice:update`
+ `status=sent`). Given this flow auto-creates a real invoice — a bigger
real-world effect than the existing OCR flows, which only create a
draft-review purchase — the operator chose to keep a manual review gate:
the invoice is created and left as `status: "created"`; the operator
reviews it and sends it by hand in the Moco UI. The existing
`bexio-invoice-sync` cascade fires later, automatically, whenever that
manual send happens — no new code needed for that part, and `update_status`
is not implemented on `MocoInvoiceClient` since it would be unused (see
CLAUDE.md conventions on not adding unused code).

**D3 — Auto-process regardless of OCR confidence, alert louder below the
threshold.** Mirrors `SmartmeEnergyExpenseService`'s existing posture: the
expense + invoice are always created on a successful project match (no
confidence gate blocks creation), but the Telegram success message uses a
`⚠️ … bitte prüfen` tone below `CONFIDENCE_THRESHOLD = 0.85`. This is lower
risk than it would otherwise be specifically because of D2 — nothing is
sent to the EVU or cascaded to Bexio without a human looking at the created
invoice first.

**D4 — Detection uses TWO independent signals, not just the EVU tag.**
Live-tested against 5 real drafts and found a second real EVU (EGBB) whose
*supplier*-side Moco company record — the one `MocoSupplierMatcher` actually
links — has `tags: []`, while only its *customer*-side record (a different
company id, same pattern as CKW) carries `Lokaler Energieversorger (EVU)`.
Relying on the tag alone silently missed this draft. Fix:
`StromproduktionProjectMatcher.has_candidate_for_supplier(supplier_name)`
(a cheap existence check reusing the matcher's own tier-1 customer-name
filter, exposed via `EnergyCreditNoteService.has_matching_project`) is now
checked as a second, independent signal — either the EVU tag OR an actual
matching `Stromproduktion` project is sufficient. This is strictly more
robust than the tag (it can only be true when there is a real project to
route to) and doesn't require clean tag data on every supplier record.

**D5 — `EnergyCreditNoteData.net_amount` is always normalized to a
positive magnitude.** Live-tested and found EGBB's statement format frames
its *entire* invoice as negative (e.g. `"Elektrizität Rücklieferung
-908.25"`, net `-840.20`) because the document reads from the payer's
perspective (`"Der Betrag wird Ihnen ... ausbezahlt"` — a payout), unlike
CKW's plain-positive convention. `net_amount` always represents the amount
owed TO PVcontracting, so `_to_energy_credit_note_data` applies `abs()`
unconditionally — a hard code-level guarantee, not just prompt wording
(the prompt is also updated to ask for a positive value, belt-and-suspenders,
same posture as the IBAN/QR-reference normalization in `_normalize_iban`/
`_normalize_qr_reference`).
