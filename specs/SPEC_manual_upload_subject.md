# SPEC — the operator subject on hand-uploaded drafts

Status: implemented on branch `ocr-manual-upload-subject` (stacked on
`ocr-vat-fallback`).

## Problem

Moco purchase drafts reach the invoice inbox two ways: **email import**
(Moco fills `email_from` / `email_body`, and `SupplierInvoiceOcrService`
already replays both into a 📧 comment on the created purchase) and
**manual upload**, where a staff member picks a PDF and types a subject.

That subject is thrown away today. It shouldn't be, for two reasons:

1. It is the only record of *who* filed the expense and *what they called
   it* — and the draft itself is deleted once the purchase is created, so
   after a successful run the information is gone for good.
2. On an expense receipt it states the **business purpose**, which no
   amount of reading the document can recover. "Mittagessen 20.8." on a
   restaurant slip is the bookkeeping-relevant fact; the document itself
   can only say "Küche / Restaurant-Beleg".

## Live shape of the data

Sampled across both accounts (`GET /purchases/drafts`):

- The discriminator is **`email_from`, not `user`**. Solar draft 3213194
  is an email Romain *forwarded*: it carries `email_from` **and**
  `user: Romain Kälin`. Keying on `user` would misread email Subject
  headers as operator input.
- Roughly **half of manual subjects are worthless file names**, and it
  splits by account: all 7 live skyr manual drafts carry a real subject
  ("Mittagessen 20.8.", "Geschäftsessen mit Beraterin E. Aschwanden"),
  while all 3 solar ones are browser file names
  ("trennscheibenblätter.pdf", "260731 CKW Meierhofweg10 Rechnung  600
  949 594.pdf").
- Moco appends "(ohne PDF-Beleg)" to the title of an attachment-less
  draft. Those are already handled upstream by `_is_notification_subject`.

## Decisions

**D1 — the model merges subject and document, not a hand-written rule.**
The subject is passed to Claude as context and the merge comes back as a
new field. Chosen over deterministic concatenation because the useful-vs-
filename call needs the document in hand, and because a fixed template
("<subject> — <description>") produces `260731 CKW Meierhofweg10 Rechnung
600 949 594.pdf — Stromrechnung` on every solar upload. Cost is ~20 extra
input tokens on a call that already happens; no second request.

Accepted trade-off: the title is no longer byte-predictable, so tests
assert the *plumbing* (subject reaches the model, field reaches the
payload, fallbacks hold) rather than exact strings. Live output is
recorded under "Verification" below.

**D2 — a separate `position_title` field, not an overloaded
`description`.** `InvoiceData.description` stays a pure reading of the
document; `position_title` is the merged booking title. Overloading
`description` would have changed the meaning of a field that
`EnergyCreditNoteService` and the Gutschrift path also read, for a
benefit confined to the purchase title. The prompt tells the model to
return the description verbatim when no subject was supplied, and
`_build_create_payload` falls through `position_title → description →
supplier_name → "OCR-importierte Rechnung"`, so a model that omits the
field lands exactly on the old behaviour.

The prompt caps `position_title` at 80 characters — the same limit
`BexioExpenseSyncService._truncate` applies to both the bill title and
the bill line title, so nothing gets chopped mid-word downstream.

**D3 — the subject rides in the user turn.** `SYSTEM_PROMPT` stays a
static, per-request-identical string. The subject is employee-typed free
text landing in a prompt, so it is fenced in guillemets and explicitly
labelled as data, not instruction.

**D4 — purchase title and line-item title stay the same string.** They
are one derived value today; the subject folds into both, so the business
purpose shows in Moco's purchase list and on the Bexio bill title as well
as the position line.

**D5 — one provenance comment, either 📧 or 📎.** The manual-upload
comment (`Betreff` + `Hochgeladen von`) is the mutually exclusive
counterpart of the existing email-source comment and occupies the same
slot. The email comment additionally gains a `Betreff:` line — for an
email-imported draft the title *is* the Subject header, and it was being
dropped. A title alone never renders as an email source: without an
`email_from` that would claim a manual upload as an email.

**D6 — the Bexio payment remark becomes the purchase title.** Once
`position_title` carries the business purpose, the Moco purchase `title`
is the best text available for `payment.note`, the remark shown against
the payment in Bexio. `BexioExpenseSyncService._payment_note` uses it for
BOTH payment types, falling back to composing supplier / Belegnummer /
Zahlungszweck (empty parts dropped) and finally to `"-"`.

Two defects fixed along the way, both inherited from
`reference/Sync_expenses_from_Moco_to_Bexio.json`:

- The IBAN/QR branch read only Moco's `info` — the QR-bill Zahlungszweck,
  empty on both live IBAN fixtures and on most invoices — so nearly every
  QR payment reached Bexio carrying a bare `"-"`.
- The MANUAL branch's join filtered on `is not None`, but its parts were
  `x or ""` and therefore never None, leaving dangling separators. An
  OCR'd card receipt with an unmatched supplier and no Zahlungszweck
  produced `" - 000047 - "` — the commonest shape this flow generates.

(The n8n original was worse still: `receipt_identifier||"" + " - "` binds
as `receipt_identifier || (" - ")`, so it concatenated with no separator
at all. The port had already fixed that and introduced the empty-part
artifact instead.)

Reviewed and deliberately left alone: `message` / `booking_text` /
`reference_no` keep carrying `receipt_identifier` and `reference` — those
are payment-instruction fields, not the remark. The bill payment block
sets both `message` and `reference_no` for QR while
`_build_outgoing_payment_payload` makes them mutually exclusive; that
asymmetry is in the n8n export too, so it is upstream intent rather than
a porting slip. The outgoing-payment payload gains no `note`: the
endpoint has no such field today and adding an unvalidated one risks a
400 on a step that is already soft-failed.

**D7 — a plain ASCII hyphen joins subject and description.** Bexio
rejects an em dash on its text fields, and the `position_title` separator
flows through to the bill title, the line-item title and the payment
remark. The prompt therefore asks for `" - "`. Because the model writes
German prose and can reach for a dash anywhere in it — not only at the
join — `BexioExpenseSyncService._bexio_text` also normalizes em, en,
figure, horizontal-bar and minus characters at the boundary, then
collapses whitespace, then truncates (in that order, so the cap counts
what Bexio actually receives). Moco keeps whatever the model wrote; this
is a translation for one downstream system, not a correction.

## Out of scope

Backfilling purchases already created from manual uploads — their drafts
are gone, so the subject is unrecoverable.

## Verification

Live dry-runs through `scripts/test_ocr_create_purchase.py`:

| draft | subject | `description` (document) | `position_title` (booked) |
|---|---|---|---|
| skyr 3216692 | `Mittagessen 20.8.` | Restaurantbeleg Küche/Bar, Barzahlung per Debit Mastercard | **Mittagessen 20.8. — Ligu Lehm, Bern** |
| solar 2916828 | `trennscheibenblätter.pdf` | Trennscheiben Alu 125mm, Inox 115x1, Proteinriegel Banane | Trennscheiben Alu 125mm, Inox 115x1, Proteinriegel Banane |

The file-name subject was dropped as intended (`position_title` ==
`description`), while still being recorded in the 📎 comment as
provenance.
