"""PurchaseReviewGate — decide whether an OCR'd purchase needs human review.

Every purchase the OCR flow creates used to be stamped
`["OCR", "Review pending"]`, and `BexioExpenseSyncService` refuses to sync
until a human strips that tag. When OCR resolved everything — supplier
company matched, expense account determined, model confident — that review
is busywork, so this gate releases the purchase straight through with an
`Auto` tag instead.

A purchase is **auto-released** only when all five hold:

1. a supplier company was matched (`company_id is not None`),
2. the resolved category is *trusted* — see `_category_trusted` below,
3. the model's own confidence is at least `AUTO_RELEASE_CONFIDENCE`,
4. it isn't a credit note (those always need a human to check the sign),
5. the VAT rate came off the document (or the supplier / account default),
   not from `VatCodeResolver`'s payment-method floor,
6. the payment instruction is actually payable — no QR-IBAN left without
   its QR-reference.

Anything else is held, and `ReviewDecision.reasons` names which conditions
failed so the log line, the Telegram message and the batch script's column
all say *why* rather than just *that* it was held.

Deliberately a pure collaborator (no I/O), same shape as
`MocoCategoryResolver` / `MocoSupplierMatcher`: it is unit-testable in
isolation, and `scripts/batch_ocr_drafts.py` calls the very same class to
preview what the rule *would* decide for historical drafts. A private copy
of the policy inside the service would let that preview drift away from the
real gate, which is exactly what the preview exists to prevent.

See `specs/SPEC_purchase_payment_already_paid.md` and
`specs/SPEC_vat_code_fallback.md` for the decisions.
"""

import logging
from dataclasses import dataclass, field

from api.anthropic_ocr_client import InvoiceData, is_qr_iban
from api.moco_category_resolver import CategoryDecision
from api.moco_project_resolver import ProjectMatch
from api.vat_code_resolver import VatDecision

logger = logging.getLogger("purchase_review_gate")

# Deliberately separate from `supplier_invoice_ocr_service.CONFIDENCE_THRESHOLD`
# (0.85), which only picks a Telegram emoji. This one can let a purchase reach
# Bexio unreviewed, so tuning alert noise must not silently retune it. Starts
# stricter than the alert bar; lower it once the batch preview shows the real
# distribution.
AUTO_RELEASE_CONFIDENCE = 0.90

OCR_TAG = "OCR"
REVIEW_PENDING_TAG = "Review pending"
AUTO_TAG = "Auto"
CREDIT_NOTE_TAG = "Gutschrift"

# Project tiers we trust enough to skip review on. `MocoProjectResolver`'s
# loosest tier, "token-overlap", treats ANY single shared token as a match —
# loose enough that `SmartmeProjectMatcher` was written specifically to avoid
# it. The `Aufwandkonto` custom-field is equally deliberate operator config
# either way; it's the match *selecting* the project that's weaker there.
TRUSTED_PROJECT_TIERS = ("exact", "substring")


@dataclass(frozen=True)
class ReviewDecision:
    """Outcome of `PurchaseReviewGate.evaluate(...)`.

    `reasons` is operator-facing German, empty when auto-released. `tags` is
    the finished tag list for the create-purchase payload — assembling it
    here keeps tag policy in one place instead of splitting it between the
    gate and `_build_create_payload`.
    """
    review_pending: bool
    tags: list[str]
    reasons: list[str] = field(default_factory=list)

    def reason_text(self) -> str:
        """Comma-joined reasons for a log line or Telegram message."""
        return ", ".join(self.reasons)


class PurchaseReviewGate:
    def __init__(self, *, min_confidence: float = AUTO_RELEASE_CONFIDENCE):
        self._min_confidence = min_confidence

    def evaluate(self, *, invoice: InvoiceData,
                 company_id: int | None,
                 category: CategoryDecision | None,
                 project_match: ProjectMatch | None = None,
                 vat: VatDecision | None = None) -> ReviewDecision:
        reasons: list[str] = []

        if company_id is None:
            reasons.append("keine Firma zugeordnet")

        category_reason = self._category_reason(category, project_match)
        if category_reason:
            reasons.append(category_reason)

        # A rate nobody read off the document must not reach Bexio
        # unreviewed — `VatCodeResolver`'s floor guesses by payment method
        # (spec D3). In practice this only bites bank-transfer bills:
        # already-paid card receipts are held anyway, because
        # `MocoCategoryResolver` short-circuits at `already_paid` and
        # leaves `category_id` None.
        if vat is not None and vat.guessed:
            # `:g` so 0.0 prints as "0%" and 8.1 stays "8.1%".
            rate = f"{vat.rate:g}%" if vat.rate is not None else "unbekannt"
            reasons.append(f"MWST-Satz nicht auf dem Beleg erkannt "
                           f"(Annahme {rate})")

        # A QR-IBAN is only payable WITH its QR-reference, so this pair is
        # a bill no bank will accept — releasing it to Bexio unreviewed
        # produces a Zahlungsausgang that cannot be executed. Unlike a
        # missing IBAN, which `_apply_supplier_iban_fallback` can recover
        # from the supplier record, a reference is per-invoice and exists
        # only on the document: there is nothing to fall back to, so a
        # human has to read it off the paper.
        #
        # Skipped for an already-paid card receipt: nothing is being paid,
        # so "not payable" would be a false statement in `reasons`. This
        # is not a strictness carve-out — those are held anyway by the
        # category chain (`MocoCategoryResolver` short-circuits at
        # `already_paid`) — it only keeps the stated reason honest.
        if (not invoice.already_paid_by_card
                and is_qr_iban(invoice.iban)
                and not invoice.qr_reference):
            reasons.append("QR-IBAN ohne QR-Referenz (nicht zahlbar)")

        if invoice.confidence < self._min_confidence:
            reasons.append(f"Konfidenz {invoice.confidence:.0%} "
                           f"(< {self._min_confidence:.0%})")

        if invoice.is_credit_note:
            reasons.append("Gutschrift")

        review_pending = bool(reasons)
        tags = [OCR_TAG, REVIEW_PENDING_TAG if review_pending else AUTO_TAG]
        if invoice.is_credit_note:
            # Second visual cue for the reviewer, on top of the negative
            # total and the comment warning. Kept independent of the
            # review/auto tag so a credit note is filterable either way.
            tags.append(CREDIT_NOTE_TAG)

        decision = ReviewDecision(review_pending=review_pending, tags=tags,
                                  reasons=reasons)
        logger.info("review gate: review_pending=%s tags=%s reasons=%s",
                    review_pending, tags, reasons or "-")
        return decision

    def _category_reason(self, category: CategoryDecision | None,
                         project_match: ProjectMatch | None) -> str | None:
        """None when the category is trusted, else the German reason.

        Trust is decided by *source*, not merely by the field being set:

        - `"supplier"` — always trusted. The supplier was matched by name
          off the receipt itself and the `Aufwandkonto` is deliberate
          operator config.
        - `"project"` — only on an `exact` / `substring` project match.
        - `"default"` (the hardcoded 4000 Wareneinkauf fallback) — trusted.
          Note this is *unreachable* for already-paid card receipts:
          `MocoCategoryResolver` short-circuits at `already_paid` before
          reaching the default, so a card receipt can never present as
          `source="default"`. Card-receipt strictness therefore falls out
          of the resolver's existing chain and this gate needs no
          payment-method branch of its own.
        - `"already_paid"` — never trusted; `category_id` is None by
          construction.

        A `category_id` of None also covers the "override was set but its
        credit_account isn't in the catalog" case, which is exactly the
        situation a human should look at.
        """
        if category is None or category.category_id is None:
            return "kein Aufwandkonto"
        if category.source == "project":
            tier = project_match.tier if project_match else None
            if tier not in TRUSTED_PROJECT_TIERS:
                return (f"Aufwandkonto nur über schwache Projekt-Zuordnung "
                        f"({tier or 'unbekannt'})")
        return None
