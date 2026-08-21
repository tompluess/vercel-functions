"""Unit tests for PurchaseReviewGate — the auto-release policy.

Pure decision logic, no I/O: every case builds an InvoiceData +
CategoryDecision (+ ProjectMatch where the tier matters) and asserts on the
resulting ReviewDecision.

See `specs/SPEC_purchase_payment_already_paid.md` for the rules being
enforced here, in particular D1 (a category is trusted by its *source*, not
merely by being set) and D2 (confidence applies uniformly, with no
payment-method carve-out), plus `specs/SPEC_vat_code_fallback.md` D3 (a
VAT rate the resolver guessed holds the purchase).
"""

import pytest

from api.anthropic_ocr_client import InvoiceData
from api.moco_category_resolver import CategoryDecision
from api.moco_project_resolver import ProjectMatch
from api.purchase_review_gate import (
    AUTO_RELEASE_CONFIDENCE,
    PurchaseReviewGate,
)
from api.vat_code_resolver import VatDecision


def make_invoice(**overrides) -> InvoiceData:
    base = dict(
        supplier_name="Digitec Galaxus AG",
        supplier_address=None,
        invoice_date="2026-08-01",
        due_date=None,
        invoice_number="R-1",
        total_amount=249.0,
        net_amount=None,
        vat_amount=None,
        vat_rate=0.081,
        currency="CHF",
        iban=None,
        qr_reference=None,
        creditor_reference=None,
        payment_purpose=None,
        description="Werkzeug",
        position_title=None,
        is_credit_note=False,
        commission=None,
        delivery_address=None,
        already_paid_by_card=False,
        confidence=0.95,
    )
    base.update(overrides)
    return InvoiceData(**base)


def supplier_category(category_id: int = 17) -> CategoryDecision:
    return CategoryDecision(category_id, "supplier override",
                            source="supplier", credit_account="6510")


def project_category(category_id: int = 18) -> CategoryDecision:
    return CategoryDecision(category_id, "project override",
                            source="project", credit_account="4500")


def default_category(category_id: int = 17) -> CategoryDecision:
    return CategoryDecision(category_id, "default", source="default",
                            credit_account="4000")


def already_paid_category() -> CategoryDecision:
    """What the resolver returns for a card receipt with no override."""
    return CategoryDecision(None, "skipped: already paid",
                            source="already_paid")


def match(tier: str) -> ProjectMatch:
    return ProjectMatch(project={"id": 7, "name": "P"}, status="matched",
                        candidate_count=1, tier=tier)


# --- the happy path ---------------------------------------------------------

def test_all_conditions_met_auto_releases():
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(), company_id=555,
        category=supplier_category(), project_match=None)

    assert decision.review_pending is False
    assert decision.tags == ["OCR", "Auto"]
    assert decision.reasons == []


# --- each condition failing on its own --------------------------------------

def test_no_company_holds():
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(), company_id=None,
        category=supplier_category(), project_match=None)

    assert decision.review_pending is True
    assert decision.tags == ["OCR", "Review pending"]
    assert decision.reasons == ["keine Firma zugeordnet"]


def test_no_category_holds():
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(), company_id=555,
        category=None, project_match=None)

    assert decision.review_pending is True
    assert decision.reasons == ["kein Aufwandkonto"]


def test_low_confidence_holds():
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(confidence=0.72), company_id=555,
        category=supplier_category(), project_match=None)

    assert decision.review_pending is True
    assert decision.reasons == ["Konfidenz 72% (< 90%)"]


def test_credit_note_holds_and_keeps_gutschrift_tag():
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(is_credit_note=True), company_id=555,
        category=supplier_category(), project_match=None)

    assert decision.review_pending is True
    assert decision.reasons == ["Gutschrift"]
    assert decision.tags == ["OCR", "Review pending", "Gutschrift"]


def test_multiple_failures_all_named():
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(confidence=0.40), company_id=None,
        category=None, project_match=None)

    assert decision.reasons == [
        "keine Firma zugeordnet",
        "kein Aufwandkonto",
        "Konfidenz 40% (< 90%)",
    ]
    assert decision.reason_text() == (
        "keine Firma zugeordnet, kein Aufwandkonto, Konfidenz 40% (< 90%)")


# --- D1: the category-source trust matrix -----------------------------------

@pytest.mark.parametrize("category,project_match,released", [
    # supplier override — always trusted.
    (supplier_category(), None, True),
    # the hardcoded 4000 fallback — trusted. Unreachable for card receipts,
    # since the resolver short-circuits at already_paid before it.
    (default_category(), None, True),
    # project override — only on a strong project match.
    (project_category(), match("exact"), True),
    (project_category(), match("substring"), True),
    (project_category(), match("token-overlap"), False),
    (project_category(), None, False),
    # card receipt with no override at all.
    (already_paid_category(), None, False),
])
def test_category_source_matrix(category, project_match, released):
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(), company_id=555,
        category=category, project_match=project_match)

    assert decision.review_pending is not released


def test_token_overlap_project_names_the_tier_in_the_reason():
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(), company_id=555,
        category=project_category(), project_match=match("token-overlap"))

    assert decision.reasons == [
        "Aufwandkonto nur über schwache Projekt-Zuordnung (token-overlap)"]


def test_override_set_but_unmapped_holds():
    """An Aufwandkonto whose credit_account isn't in the catalog resolves to
    category_id=None with source='supplier' — exactly the case a human
    should see, so it must not auto-release."""
    unmapped = CategoryDecision(None, "supplier override 9999 not found",
                                source="supplier", credit_account="9999")
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(), company_id=555,
        category=unmapped, project_match=None)

    assert decision.review_pending is True
    assert decision.reasons == ["kein Aufwandkonto"]


# --- D2: confidence applies uniformly ---------------------------------------

def test_already_paid_gets_no_confidence_carve_out():
    """A card receipt at 0.88 with a supplier Aufwandkonto is still held.

    Considered and rejected during spec review: relaxing the bar for
    already-paid receipts on the grounds that they cannot move money. One
    uniform rule won instead (D2), so this is the regression test for the
    absence of a payment-method branch in the gate.
    """
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(already_paid_by_card=True, confidence=0.88),
        company_id=555, category=supplier_category(), project_match=None)

    assert decision.review_pending is True
    assert decision.reasons == ["Konfidenz 88% (< 90%)"]


def test_boundary_confidence_is_inclusive():
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(confidence=AUTO_RELEASE_CONFIDENCE),
        company_id=555, category=supplier_category(), project_match=None)

    assert decision.review_pending is False


def test_min_confidence_is_injectable():
    decision = PurchaseReviewGate(min_confidence=0.5).evaluate(
        invoice=make_invoice(confidence=0.6), company_id=555,
        category=supplier_category(), project_match=None)

    assert decision.review_pending is False


# --- guessed VAT rate (SPEC_vat_code_fallback.md D3) ------------------------

def test_guessed_vat_rate_holds_the_purchase():
    """A rate `VatCodeResolver` assumed from the payment method must not
    reach Bexio unreviewed — everything else here is auto-release clean."""
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(), company_id=555,
        category=supplier_category(), project_match=None,
        vat=VatDecision(vat_code_id=86400, source="fallback_standard",
                        rate=8.1))

    assert decision.review_pending is True
    assert decision.reasons == [
        "MWST-Satz nicht auf dem Beleg erkannt (Annahme 8.1%)"]
    assert "Review pending" in decision.tags


def test_vat_read_off_the_document_does_not_hold():
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(), company_id=555,
        category=supplier_category(), project_match=None,
        vat=VatDecision(vat_code_id=86400, source="ocr", rate=8.1))

    assert decision.review_pending is False


@pytest.mark.parametrize("source", ["supplier", "account_default"])
def test_configured_defaults_are_not_guesses(source):
    """Tiers 2 and 3 are operator configuration, not an assumption."""
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(), company_id=555,
        category=supplier_category(), project_match=None,
        vat=VatDecision(vat_code_id=86400, source=source, rate=8.1))

    assert decision.review_pending is False


def test_unresolved_vat_alone_does_not_hold():
    """`vat_code_id=None` never reaches the gate in a way that matters —
    Moco 422s the create, so there's no purchase to tag. Asserting it
    doesn't add a bogus reason keeps the message honest."""
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(), company_id=555,
        category=supplier_category(), project_match=None,
        vat=VatDecision(vat_code_id=None))

    assert decision.review_pending is False


def test_omitted_vat_argument_keeps_the_old_behaviour():
    """Operator scripts and older call sites pass no `vat` at all."""
    decision = PurchaseReviewGate().evaluate(
        invoice=make_invoice(), company_id=555,
        category=supplier_category(), project_match=None)

    assert decision.review_pending is False
