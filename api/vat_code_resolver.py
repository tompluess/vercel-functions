"""VatCodeResolver — pick the Moco `vat_code_id` for an OCR'd purchase.

Moco's `POST /purchases` requires a `vat_code_id` on every line item, and
the OCR flow has to produce one from whatever the document happens to
show. Four tiers, most-trustworthy first:

1. **ocr** — the OCR'd `vat_rate`, matched against `GET /vat_code_purchases`.
2. **supplier** — the matched supplier company's own default rate.
3. **account_default** — the code flagged as the account-wide default.
   (Neither live Moco account actually flags one, so this tier is
   currently dead weight kept for accounts that do.)
4. **fallback_zero / fallback_standard** — a floor, split on payment
   method: an already-paid card/POS slip books 0%, anything else books
   the 8.1% standard rate. See `specs/SPEC_vat_code_fallback.md` D1.

Only when even the floor finds no active code at the wanted rate is the
field omitted — Moco then 422s and
`SupplierInvoiceOcrService` turns that into a Telegram alert + a 200 ACK.

Pure, no I/O: the caller fetches `GET /vat_code_purchases` and hands the
list in, exactly like `MocoCategoryResolver` takes the category catalog.
That keeps it unit-testable and lets the two operator scripts
(`scripts/batch_ocr_drafts.py`, `scripts/test_ocr_create_purchase.py`)
preview the real rule instead of a hand-written mirror of it.
"""

import logging
from dataclasses import dataclass

from api.anthropic_ocr_client import InvoiceData

logger = logging.getLogger("vat_code_resolver")

# Switzerland's standard VAT rate since 2024, and the rate Moco itself
# treats as the default for a new purchase item. The floor for anything
# not settled at a terminal.
STANDARD_VAT_RATE = 8.1
ZERO_VAT_RATE = 0.0

# Tolerance when comparing an OCR'd rate against Moco's `tax` percentage.
# Sonnet sometimes returns 0.077 for the legal 7.7%, or rounds slightly.
RATE_EPSILON = 0.05

# `VatDecision.source` values that mean "nobody read this rate off the
# document" — `PurchaseReviewGate` holds those for human review.
GUESSED_SOURCES = ("fallback_zero", "fallback_standard")


@dataclass(frozen=True)
class VatDecision:
    """Outcome of `VatCodeResolver.resolve(...)`.

    `vat_code_id` is None only when the account has no usable code at all;
    `source` names the tier that produced it (None alongside a None id).
    `rate` is the chosen code's `tax` percentage — carried so the review
    gate and the Moco comment can name the number a fallback guessed.
    """
    vat_code_id: int | None
    source: str | None = None
    rate: float | None = None

    @property
    def guessed(self) -> bool:
        """True when the rate came from the payment-method floor."""
        return self.source in GUESSED_SOURCES


class VatCodeResolver:
    def __init__(self, vat_codes: list[dict]):
        self._vat_codes = vat_codes or []

    def resolve(self, invoice: InvoiceData,
                supplier_company: dict | None) -> VatDecision:
        """Run the four tiers and return the first hit.

        `supplier_company` is the full record from `get_company` — the
        list shape from `list_suppliers` carries neither the default vat
        code nor `custom_properties`.
        """
        if invoice.vat_rate is not None:
            match = find_vat_code_by_rate(self._vat_codes, invoice.vat_rate)
            if match is not None:
                logger.info("vat: matched OCR vat_rate=%s to vat_code_id=%s",
                            invoice.vat_rate, match.get("id"))
                return _decision(match, "ocr")
            logger.warning("vat: OCR vat_rate=%s did not match any active "
                           "Moco vat_code (tax values=%s); falling back to "
                           "supplier default", invoice.vat_rate,
                           [c.get("tax") for c in self._vat_codes
                            if c.get("active") is not False])

        if supplier_company:
            supplier_match = supplier_default_vat_code(
                supplier_company, self._vat_codes,
            )
            if supplier_match is not None:
                logger.info("vat: using supplier default vat_code_id=%s "
                            "(company_id=%s)", supplier_match.get("id"),
                            supplier_company.get("id"))
                return _decision(supplier_match, "supplier")
            logger.info("vat: supplier id=%s has no default vat_code, "
                        "falling back to account default",
                        supplier_company.get("id"))

        account_default = account_default_vat_code(self._vat_codes)
        if account_default is not None:
            logger.info("vat: using account-default vat_code_id=%s",
                        account_default.get("id"))
            return _decision(account_default, "account_default")

        return self._fallback(invoice)

    def _fallback(self, invoice: InvoiceData) -> VatDecision:
        """The payment-method floor (spec D1).

        A card/POS slip that prints no VAT line books 0% — claiming no
        input tax understates the deduction and never overstates it,
        which is the safe direction. Everything else is a supplier
        invoice paid by transfer, which in practice always carries the
        standard rate.
        """
        if invoice.already_paid_by_card:
            wanted, source = ZERO_VAT_RATE, "fallback_zero"
        else:
            wanted, source = STANDARD_VAT_RATE, "fallback_standard"

        match = find_vat_code_by_rate(self._vat_codes, wanted)
        if match is not None:
            logger.info("vat: no tier resolved a code — falling back to "
                        "%.1f%% (vat_code_id=%s, already_paid_by_card=%s)",
                        wanted, match.get("id"), invoice.already_paid_by_card)
            return _decision(match, source)

        logger.warning("vat: could not resolve vat_code_id from OCR, "
                       "supplier, account default, or the %.1f%% fallback "
                       "— POST /purchases will likely 422", wanted)
        return VatDecision(vat_code_id=None)


def _decision(code: dict, source: str) -> VatDecision:
    return VatDecision(vat_code_id=code.get("id"), source=source,
                       rate=_tax_of(code))


def _tax_of(code: dict) -> float | None:
    try:
        return float(code["tax"])
    except (KeyError, TypeError, ValueError):
        return None


def find_vat_code_by_rate(vat_codes: list[dict],
                          rate: float | None) -> dict | None:
    """Find the active vat_code whose `tax` matches `rate`.

    Moco's `/vat_code_purchases` objects look like::
        {"id": 186, "tax": 7.7, "reverse_charge": false,
         "intra_eu": false, "active": true, ...}

    `tax` is a percentage (8.1, 7.7, 2.6). OCR returns the rate as a
    decimal (0.081 for 8.1% — per the system prompt), so we compare
    against `rate * 100`, but also try the raw value to stay tolerant of a
    prompt-drift run that returned the percentage directly. Callers
    passing a percentage already (the fallback tier, the supplier
    default) work because 8.1 also matches itself.

    Inactive codes are skipped — Moco keeps historical rates (pre-2024)
    around with `active: false`, and posting one would either 422 or book
    to a deprecated rate.

    **Special schemes are only picked as a last resort.** Both live
    accounts carry two active `tax: 0.0` codes and list the
    `reverse_charge` "(Ausland)" one *first*, so a plain first-match would
    book a domestic 0% invoice to reverse charge. Plain codes therefore
    win; a `reverse_charge` / `intra_eu` code is returned only when it is
    the sole match for the rate (spec D2).
    """
    if rate is None:
        return None
    # OCR rate in decimal (0.081) vs Moco's `tax` percentage (8.1). The
    # cross-format candidate covers a rate that arrives as a percentage.
    candidates = (rate * 100, rate)

    special: dict | None = None
    for code in vat_codes:
        if code.get("active") is False:
            continue
        value = _tax_of(code)
        if value is None:
            continue
        if not any(abs(value - target) < RATE_EPSILON
                   for target in candidates):
            continue
        if code.get("reverse_charge") is True or code.get("intra_eu") is True:
            if special is None:
                special = code
            continue
        return code
    return special


def account_default_vat_code(vat_codes: list[dict]) -> dict | None:
    """Return the active vat_code marked as the account-wide default.

    Moco's `/vat_code_purchases` response may carry a flag indicating
    which code is the configured default; the field name isn't documented
    in the example shape we have, so we try `default`, `is_default`, and
    the legacy `default_for_purchase` to be robust. Neither live account
    flags one — the payment-method fallback below is what actually
    catches these invoices.
    """
    for code in vat_codes:
        if code.get("active") is False:
            continue
        if (code.get("default") is True
                or code.get("is_default") is True
                or code.get("default_for_purchase") is True):
            return code
    return None


def supplier_default_vat_code(company: dict,
                              vat_codes: list[dict]) -> dict | None:
    """Resolve the supplier's default vat_code as a full code dict.

    Per Moco's company docs the relevant field is `supplier_vat`, a nested
    object with a `tax` percentage (e.g. `{"supplier_vat": {"tax": 8.1}}`).
    There's no direct `vat_code_id` on the company — we translate the rate
    through the same `/vat_code_purchases` list the OCR tier uses.

    Defensive fallback: a couple of older / alternate field names
    (`default_vat_code_purchase_id`, `vat_code_purchase_id`) are also
    tried in case some accounts return a direct id; that path can only
    report the id, so the returned dict carries no `tax`.
    """
    supplier_vat = company.get("supplier_vat")
    if isinstance(supplier_vat, dict) and supplier_vat.get("tax") is not None:
        try:
            rate = float(supplier_vat["tax"])
        except (TypeError, ValueError):
            rate = None
        if rate is not None:
            match = find_vat_code_by_rate(vat_codes, rate)
            if match is not None:
                return match
    for key in ("default_vat_code_purchase_id", "vat_code_purchase_id"):
        value = company.get(key)
        if isinstance(value, int):
            return {"id": value}
    return None
