"""Unit tests for `VatCodeResolver` — the four-tier vat_code_id chain.

The catalogs below mirror the *live* `GET /vat_code_purchases` shape of
both Moco accounts (solar, skyr): an active 8.1 / 3.8 / 2.6, two active
`tax: 0.0` codes with the reverse-charge "(Ausland)" one listed **first**,
inactive pre-2024 rates, and — importantly — no code flagged as the
account-wide default anywhere.
"""

from api.anthropic_ocr_client import InvoiceData
from api.vat_code_resolver import (
    VatCodeResolver,
    account_default_vat_code,
    find_vat_code_by_rate,
    supplier_default_vat_code,
)


# Live skyr catalog, ids and order preserved.
LIVE_CODES = [
    {"id": 86400, "tax": 8.1, "reverse_charge": False, "intra_eu": False,
     "active": True},
    {"id": 33677, "tax": 7.7, "description": "(bis 2023)",
     "reverse_charge": False, "intra_eu": False, "active": False},
    {"id": 87523, "tax": 3.8, "reverse_charge": False, "intra_eu": False,
     "active": True},
    {"id": 88648, "tax": 2.6, "reverse_charge": False, "intra_eu": False,
     "active": True},
    {"id": 33681, "tax": 0.0, "description": "(Ausland)",
     "reverse_charge": True, "intra_eu": False, "active": True},
    {"id": 33679, "tax": 0.0, "reverse_charge": False, "intra_eu": False,
     "active": True},
]


def make_invoice(**overrides) -> InvoiceData:
    base = dict(
        supplier_name="FLYERALARM", supplier_address=None,
        invoice_date="2026-05-12", due_date=None, invoice_number="R-42",
        total_amount=1234.50, net_amount=None, vat_amount=None,
        vat_rate=None, currency="CHF", iban=None, qr_reference=None,
        creditor_reference=None, payment_purpose=None,
        description="Solarmodule", position_title=None,
        is_credit_note=False, commission=None,
        delivery_address=None, already_paid_by_card=False, confidence=0.92,
    )
    base.update(overrides)
    return InvoiceData(**base)


def resolve(*, codes=None, company=None, **invoice_overrides):
    return VatCodeResolver(LIVE_CODES if codes is None else codes).resolve(
        make_invoice(**invoice_overrides), company)


# --- tier 1: the OCR'd rate -------------------------------------------------

def test_ocr_rate_wins():
    d = resolve(vat_rate=0.081)
    assert (d.vat_code_id, d.source, d.rate) == (86400, "ocr", 8.1)
    assert d.guessed is False


def test_ocr_rate_accepts_a_percentage_too():
    """Prompt drift: Sonnet occasionally returns 8.1 instead of 0.081."""
    assert resolve(vat_rate=8.1).vat_code_id == 86400


def test_ocr_rate_ignores_inactive_historical_codes():
    """7.7% is the pre-2024 rate — Moco keeps it with `active: false`."""
    d = resolve(vat_rate=0.077)
    assert d.vat_code_id != 33677
    # No active 7.7 exists, so it falls through to the standard-rate floor.
    assert d.source == "fallback_standard"


def test_ocr_rate_of_zero_prefers_the_domestic_code():
    """Regression (spec D2): the reverse-charge "(Ausland)" 0.0 code sorts
    FIRST in both live accounts, so a naive first-match books a domestic
    zero-rated invoice to reverse charge."""
    d = resolve(vat_rate=0.0)
    assert d.vat_code_id == 33679
    assert d.source == "ocr"


def test_special_scheme_code_is_used_when_it_is_the_only_match():
    """Excluded as a *preference*, not a ban — an account that only has
    the reverse-charge code at a rate should still resolve."""
    codes = [{"id": 1, "tax": 0.0, "reverse_charge": True, "active": True}]
    assert resolve(codes=codes, vat_rate=0.0).vat_code_id == 1


# --- tier 2: the supplier's default -----------------------------------------

def test_supplier_default_rate_translates_to_a_code():
    company = {"id": 555, "supplier_vat": {"tax": 2.6}}
    d = resolve(company=company)
    assert (d.vat_code_id, d.source, d.rate) == (88648, "supplier", 2.6)


def test_supplier_direct_id_field_is_honoured():
    """Some accounts expose the id directly; that path can't report a rate."""
    d = resolve(company={"id": 555, "default_vat_code_purchase_id": 77})
    assert (d.vat_code_id, d.source, d.rate) == (77, "supplier", None)


def test_supplier_without_a_default_falls_through():
    d = resolve(company={"id": 555})
    assert d.source == "fallback_standard"


# --- tier 3: the account-wide default ---------------------------------------

def test_account_default_flag_beats_the_fallback():
    codes = LIVE_CODES + [{"id": 999, "tax": 3.8, "active": True,
                           "default": True}]
    d = resolve(codes=codes)
    assert (d.vat_code_id, d.source) == (999, "account_default")


def test_live_accounts_flag_no_default():
    """Documents why tier 3 never fires in production today."""
    assert account_default_vat_code(LIVE_CODES) is None


# --- tier 4: the payment-method floor (spec D1) -----------------------------

def test_bank_transfer_falls_back_to_the_standard_rate():
    d = resolve()
    assert (d.vat_code_id, d.source, d.rate) == (86400, "fallback_standard", 8.1)
    assert d.guessed is True


def test_already_paid_card_falls_back_to_zero():
    """skyr draft 3216692: a CHF 15 lunch slip with no VAT line printed."""
    d = resolve(already_paid_by_card=True)
    assert (d.vat_code_id, d.source, d.rate) == (33679, "fallback_zero", 0.0)
    assert d.guessed is True


def test_zero_fallback_never_picks_the_reverse_charge_code():
    assert resolve(already_paid_by_card=True).vat_code_id != 33681


def test_fallback_gives_up_when_the_rate_is_absent_from_the_account():
    codes = [{"id": 1, "tax": 2.6, "active": True}]
    d = resolve(codes=codes)
    assert (d.vat_code_id, d.source) == (None, None)
    assert d.guessed is False


def test_empty_catalog_resolves_to_nothing():
    """A flapping `/vat_code_purchases` hands in `[]` — the field comes off
    the payload and Moco's 422 lands on the alert-and-ACK path."""
    assert VatCodeResolver([]).resolve(make_invoice(), None).vat_code_id is None


# --- helper-level edges -----------------------------------------------------

def test_find_by_rate_tolerates_none_and_junk_tax_values():
    assert find_vat_code_by_rate(LIVE_CODES, None) is None
    assert find_vat_code_by_rate([{"id": 1, "tax": "n/a", "active": True},
                                  {"id": 2, "active": True}], 0.081) is None


def test_supplier_default_ignores_an_unparseable_rate():
    company = {"id": 555, "supplier_vat": {"tax": "acht"}}
    assert supplier_default_vat_code(company, LIVE_CODES) is None
