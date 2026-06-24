# SPEC: Kommission → Moco Project → Category resolution

Iterative spec for resolving the OCR'd `commission` ("Kommission" / "Objekt" /
"Auftragsnummer" / "Bauvorhaben") to a Moco project, and using that project to
pick the booking category (Buchhaltungs-Konto).

Read `CLAUDE.md` first for project conventions and the existing
`SupplierInvoiceOcrService` flow. This spec is implemented in two stages.

---

## End Goal

For each OCR'd draft purchase:

1. Lift `commission` from the invoice (already done by `AnthropicOcrClient`).
2. **Resolve** that free-text string to a Moco **project** (id + name).
3. **From the project**, derive the bookkeeping category for the purchase
   (`category_id` on `POST /purchases`, a.k.a. "Buchhaltungs-Konto").

Both 2 and 3 happen at draft-evaluation time. Today the OCR service emits
`commission` only as a comment line and leaves the project/category fields
empty; the operator fills them in manually during draft review.

---

## Stage 1 — Visibility in Batch Mode (this iteration)

Before any production wiring, expose Kommission in the batch validation
script so we can eyeball over real drafts how well a future matcher would do.

### Scope

`scripts/batch_ocr_drafts.py` only. **No** changes to
`SupplierInvoiceOcrService.process()` semantics, no Moco writes — purely
operator observability on top of existing OCR output.

### Changes

- **Live log line** (per draft, in addition to existing logs):
  `→ Kommission: "<raw>" → <project name or '—'>` printed at evaluation time,
  after VAT-tier resolution and before confidence/Gutschrift line.
- **Table column** in the summary table, inserted **after `Betrag`**:
  `Kommission` — the raw OCR'd value, suffixed with a ✓ when it resolved to
  exactly one Moco project (same convention as the Lieferant column).
- The `Row` dataclass gets `kommission_raw: str | None` and
  `kommission_project_matched: bool` fields. The new resolver runs in both
  dry-run and apply paths.

### Resolution algorithm (Stage 1, decided)

1. **Fetch** `GET /projects` from the source Moco account **once per script
   run** (no archived projects; default Moco listing).
2. **Index** each project by its `Kommission` custom-property value, taken
   from the project's `custom_properties` block. Normalization: strip
   **all non-alphanumeric characters** (whitespace, punctuation, `#`, `_`,
   `-`, `/`) + case-fold. Umlauts preserved. Projects without a
   `Kommission` custom-property value **fall back to `project.name`** as
   the index key (operators don't always fill in Kommission); projects
   with neither are skipped. This aggressive normalization is needed to
   bridge supplier-bill rendering (e.g. `PVA Haldenweg 12_Jegensdorf`)
   with the cleaner Moco-side key (`#Haldenweg12_Jegensdorf`) — both
   collapse to the same alnum core.
3. **Lookup** the OCR'd `commission` against the index:
   - normalize the OCR'd value the same way;
   - **exact match** → resolved (✓);
   - else **substring fallback**: collect distinct projects whose indexed
     `Kommission` is contained in the OCR'd value OR contains the OCR'd
     value; a single distinct project → matched (`tier="substring"`),
     multiple → ambiguous;
   - else **token-overlap**: tokenize both sides on non-alphanumeric
     boundaries, keep tokens of length ≥ 6, and union projects sharing
     any token with the OCR'd value. A single distinct project →
     matched (`tier="token-overlap"`), multiple → ambiguous. Catches
     noisy cases like a long project name plus a long OCR string that
     share only an address fragment (`Stroppelstrasse19`,
     `Untersiggenthal`);
   - **ambiguous** (more than one project matches at the same tier): no
     project is selected, the table cell shows `<raw> ✗ ambiguous (N)`;
   - **no match**: raw value only, no ✓.

### Out of scope for Stage 1

- Wiring the resolved project into the `POST /purchases` payload.
- Category (Buchhaltungs-Konto) selection.
- Anything in the production webhook handler.

---

## Stage 2 — Project Assignment (implemented)

`SupplierInvoiceOcrService` now takes an optional `MocoProjectResolver`.
After a successful `POST /purchases`, the service resolves
`invoice.commission` and — when the resolver returns a single
`matched` project — calls `POST /purchases/{id}/assign_to_project` for
every line item with the fixed param contract:

  - `notify_project_leader=false`
  - `billable=true`
  - `budget_relevant=true`
  - `surcharge=true`
  - `expense_id` omitted (Moco creates a fresh expense on the project)

Skipped silently when the resolver returns `empty` / `no_match` /
`ambiguous` (we prefer leaving the purchase project-less to mis-routing
it). Failures are soft-failed (log + Telegram warning appended to the
existing OCR-outcome alert) — the purchase exists, so the operator can
finish the assignment manually.

The webhook handler in `api/index.py` builds the resolver per-request
via `SourceMocoClient.list_projects()` (one extra GET per webhook). The
batch validation script passes its already-built resolver into the
service for `--apply` runs.

---

## Stage 3 — Category (Buchhaltungs-Konto) Resolution (in progress)

### Goal

Set the per-line-item `category_id` on `POST /purchases` so the new
purchase books against the right expense account. Today this field is
omitted and the operator picks it manually during review.

### Resolution chain (decided)

1. **Bills already paid via card / POS** (`invoice.already_paid_by_card`):
   **OMIT** the category entirely. These bills mix personal and project
   purchases and the operator must decide per receipt. Setting a default
   would lull the reviewer into approving the wrong account.

2. **Project-specified expense account**: if the resolver matched a Moco
   project AND that project carries an `Aufwandkonto` custom-property:
   - look up the category in `GET /purchases/categories` whose
     `credit_account` equals the property value (string equality after
     trim);
   - **on match**: use that category's `id`;
   - **on miss** (project says `"4500"` but no category has it): OMIT
     the field. We do NOT fall back to the default in this branch —
     the project explicitly said something other than the default, so
     silently using `4000` would mis-route the booking. Operator must
     either fix the project's `Aufwandkonto` or pick a category by hand.

3. **Account-wide fallback**: otherwise (no project resolved, or project
   has no `Aufwandkonto`), look up the category whose `credit_account`
   is the hardcoded default `"4000"` (Wareneinkauf — Swiss SKR
   convention).

4. **Missing-fallback edge case**: if even `"4000"` doesn't match any
   category in the catalog, OMIT the field rather than guessing — Moco
   will accept the purchase with its own default and the operator can
   set a category during review.

No reasoning lines are added to the OCR comment for this stage —
`category_id` itself is the audit trail (visible to the operator in
Moco's purchase UI).

### Data sources

- `GET /api/v1/purchases/categories` returns the catalog. The matching
  field is `credit_account` (a string like `"4000"`). Fetched once per
  webhook (and once per batch run), same pattern as
  `GET /vat_code_purchases` and `GET /projects`.
- `project.custom_properties["Aufwandkonto"]` carries the per-project
  override. Same shape as `Kommission` — string-valued custom field.

### Out of scope for Stage 3

- Per-line-item categories. The OCR pipeline always emits a single line
  item, so this is effectively a per-purchase decision.
- Editing the category after the fact (the operator does that in Moco's
  UI during review).

---

## Clarifying Questions (Stage 1)

Q1 — **What identifies a Moco project to a supplier?** Specifically: when a
supplier writes "Kommission 2025-031" on an invoice, does that correspond to
the Moco project's `identifier` field, the `name`, a custom field, or
something derivable from both?

Q2 — **Archived projects.** Should we match against archived projects too,
or only active ones? (Old construction sites stay archived but bills can
arrive months after completion.)

Q3 — **Match strictness.** Exact normalized match only, or do you want
substring / prefix matching when the OCR'd value is noisier than the project
identifier (e.g. supplier wrote "BV-Schmidt-Hauptstr." but the Moco project
is `Hauptstr. 12, Schmidt`)?

Q4 — **Caching.** A `GET /projects` over the source account can return
hundreds of projects. For the batch script, do you want one fetch per script
run (cached in memory), or one fetch per draft (slow but always fresh)?

Q5 — **Ambiguity handling.** If two projects match the same Kommission
string, what's the right behavior — pick none (no ✓), pick first, or flag
explicitly as `Kommission ✗ ambiguous` in the table?

Q6 — **Where in the table.** Confirm column order:
`Draft | Purchase | Lieferant | Betrag | Kommission | Result`. Anything I'm
missing about column width / truncation expectations?
