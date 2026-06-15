"""Bexio-side configuration for the sync services.

These are non-secret bookkeeping identifiers for the destination Bexio account.
They are intentionally hardcoded — they change rarely, are not credentials,
and are easier to review in a diff than to track across env vars. Actual
secrets (API token, webhook signing key) live in env vars; see README.

The only value kept in an env var is the per-Moco-user manual-bill bank
routing (`BEXIO_MANUAL_BANK_MAP`) — it references staff first names, which
shouldn't be committed to a public repo.
"""

import json
import os

# Owner of every record created via the sync (Bexio user id).
USER_ID = 2
OWNER_ID = 2
CONTACT_PARTNER_ID = 2

# Default bank account used for QR/IBAN payments and outgoing payments on the
# expense flow. Manual bills override this based on the source Moco user
# (see manual_bank_account_id below).
BANK_ACCOUNT_ID = 2

# Bexio contact types: 1 = Company, 2 = Person.
CONTACT_TYPE_ID_COMPANY = 1
# Bexio country ids: 1 = Switzerland.
COUNTRY_ID_CH = 1

# Defaults for the invoice flow.
LANGUAGE_ID_DE = 1
CURRENCY_ID_CHF = 1
PAYMENT_TYPE_ID = 4    # Bank transfer
MWST_TYPE = 0          # Net amounts, MWST shown per position
MWST_IS_NET = True
INVOICE_UNIT_ID = 1    # "pcs"

# Fallbacks when account lookup misses.
DEFAULT_BOOKING_ACCOUNT_NO = "4000"
DEFAULT_TAX_ID = 10

# Default revenue account for invoices when no project label matches.
DEFAULT_REVENUE_ACCOUNT_NO = "3210"

# Labels → revenue account mapping for invoices (replicates the n8n
# "Evaluate Profit Account" JS node). Iteration order matters: a later label
# wins, so order from least- to most-specific.
INVOICE_REVENUE_ACCOUNT_BY_LABEL: list[tuple[str, str]] = [
    ("Zins", "6961"),
    ("Stromproduktion", "3010"),
    ("Solarstrom", "3010"),
    ("ZEV", "3030"),
    ("Eigenverbrauch", "3020"),
    ("SDL", "3050"),
    ("Systemdienstleistung", "3050"),
    ("Auftrag", "3210"),
    ("AC-Elektro", "3220"),
    ("AC", "3220"),
    ("Elektro", "3220"),
    ("Dienstleistung", "3400"),
    ("Beratung", "3400"),
    ("Personalverleih", "3480"),
    ("Wartung", "3450"),
    ("Service", "3450"),
    ("Service und Wartung", "3450"),
    ("Energie-Management", "3410"),
    ("Material-Handel", "3200"),
]


def manual_bank_account_id(moco_user_firstname: str | None) -> int:
    """Resolve the Bexio bank_account_id used for MANUAL (non-IBAN) bills
    based on the originating Moco user's first name.

    Read from `BEXIO_MANUAL_BANK_MAP` env var (JSON object). Use a `default`
    key for the catch-all bank id:

        BEXIO_MANUAL_BANK_MAP='{"default": 3, "Michael": 5, "Romain": 4}'

    If the env var is missing or malformed, falls back to BANK_ACCOUNT_ID so
    bill creation still works (with the wrong account, which is recoverable
    in Bexio).
    """
    raw = os.environ.get("BEXIO_MANUAL_BANK_MAP", "")
    try:
        mapping = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return BANK_ACCOUNT_ID
    if moco_user_firstname and moco_user_firstname in mapping:
        return int(mapping[moco_user_firstname])
    return int(mapping.get("default", BANK_ACCOUNT_ID))


def outgoing_payment_sender() -> dict | None:
    """Sender (own-company) fields embedded in every Bexio outgoing payment.

    Bexio's POST /4.0/payment/outgoing-payments expects the paying party
    duplicated in every request (name, IBAN, bank, address). These values
    are static for our company but shouldn't be committed to a public repo,
    so they're env-driven via `BEXIO_OUTGOING_PAYMENT_SENDER` (JSON object).

    Expected keys (all strings unless noted): `name`, `iban`, `bank_name`,
    `bc_no`, `street`, `house_no`, `postcode`, `city`, `country_code`,
    and `bank_account_id` (int — defaults to BANK_ACCOUNT_ID when absent).

    Returns None when the env var is missing or malformed. The caller is
    expected to surface that as a Telegram alert and skip payment creation
    rather than crash the whole sync (the bill is the authoritative side
    effect; payment creation is an enrichment step).
    """
    raw = os.environ.get("BEXIO_OUTGOING_PAYMENT_SENDER", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def resolve_revenue_account_no(project_labels: list[str]) -> str:
    """Mirror of the n8n "Evaluate Profit Account" JS node — iterates the
    mapping in order, so later matches win over earlier ones (matches n8n's
    sequential `if` chain)."""
    chosen = DEFAULT_REVENUE_ACCOUNT_NO
    for label, account_no in INVOICE_REVENUE_ACCOUNT_BY_LABEL:
        if label in project_labels:
            chosen = account_no
    return chosen
