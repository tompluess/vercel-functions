"""Unit tests for MocoCategoryResolver.

Covers the branches of the resolution chain — project-override hit,
project-override miss (must NOT fall back), supplier-override hit/miss,
already-paid skip (only without an explicit override), default
fallback, and missing-default edge — plus index construction defenses
(non-string credit_accounts, missing fields, collision behavior).
"""

import pytest

from api.moco_category_resolver import CategoryDecision, MocoCategoryResolver


CATEGORIES = [
    {"id": 17, "credit_account": "4000", "label": "Wareneinkauf"},
    {"id": 18, "credit_account": "4500", "label": "Materialaufwand"},
    {"id": 19, "credit_account": "6500", "label": "Verwaltungsaufwand"},
]


def _project(*, aufwand: str | int | None = None) -> dict:
    p: dict = {"id": 1, "name": "Test"}
    if aufwand is not None:
        p["custom_properties"] = {"Aufwandkonto": aufwand}
    return p


def _supplier(*, aufwand: str | int | None = None) -> dict:
    s: dict = {"id": 7, "name": "Swisscom AG"}
    if aufwand is not None:
        s["custom_properties"] = {"Aufwandkonto": aufwand}
    return s


def test_indexed_count_excludes_categories_without_credit_account():
    cats = CATEGORIES + [
        {"id": 99, "label": "broken — no credit_account"},
        {"id": 100, "credit_account": "", "label": "blank"},
    ]
    r = MocoCategoryResolver(cats)
    assert r.indexed_count() == 3


def test_resolve_already_paid_returns_none():
    """Already-paid card receipt without any Aufwandkonto override:
    never a 4000 default — the reviewer picks the account by hand."""
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=True, project=None)
    assert d.category_id is None
    assert "already paid" in d.reason
    assert d.source == "already_paid"
    assert d.credit_account is None


def test_resolve_already_paid_project_override_still_applies():
    """An explicit project Aufwandkonto beats the already-paid guard —
    only the 4000 *default* is suppressed for card receipts."""
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=True, project=_project(aufwand="4500"))
    assert d.category_id == 18
    assert "project override" in d.reason


def test_resolve_already_paid_supplier_override_still_applies():
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=True, project=None,
                  supplier=_supplier(aufwand="6500"))
    assert d.category_id == 19
    assert "supplier override" in d.reason


def test_resolve_already_paid_supplier_without_field_returns_none():
    """A resolved supplier whose Aufwandkonto is unset behaves like no
    supplier on an already-paid receipt: no 4000 default."""
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=True, project=None,
                  supplier=_supplier(aufwand=None))
    assert d.category_id is None
    assert "already paid" in d.reason


def test_resolve_project_aufwand_hit_uses_project_account():
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=False,
                  project=_project(aufwand="4500"))
    assert d.category_id == 18
    assert "project override" in d.reason
    assert "4500" in d.reason


def test_resolve_project_aufwand_miss_returns_none_not_fallback():
    """Project says 4999 but no category has that credit_account.
    Must NOT fall back to 4000 — that'd silently mis-route the booking.
    The operator either fixes the project's Aufwandkonto or picks by hand.
    """
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=False,
                  project=_project(aufwand="4999"))
    assert d.category_id is None
    assert "not found" in d.reason


def test_resolve_supplier_aufwand_hit_no_project():
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=False, project=None,
                  supplier=_supplier(aufwand="6500"))
    assert d.category_id == 19
    assert "supplier override" in d.reason
    assert "6500" in d.reason
    # Machine-readable mirror of `reason` — the batch script's KATEGORIE
    # column renders from these instead of parsing the string.
    assert d.source == "supplier"
    assert d.credit_account == "6500"


def test_resolve_supplier_aufwand_hit_project_without_field():
    """A matched project without Aufwandkonto falls through to the
    supplier's field, not straight to 4000."""
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=False,
                  project=_project(aufwand=None),
                  supplier=_supplier(aufwand="6500"))
    assert d.category_id == 19


def test_resolve_project_field_beats_supplier_field():
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=False,
                  project=_project(aufwand="4500"),
                  supplier=_supplier(aufwand="6500"))
    assert d.category_id == 18
    assert "project override" in d.reason


def test_resolve_project_miss_wins_over_supplier_field():
    """A set-but-unmapped project override is a final answer — it must
    NOT fall through to the supplier tier (same no-reroute rationale as
    the existing miss-vs-4000 test)."""
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=False,
                  project=_project(aufwand="4999"),
                  supplier=_supplier(aufwand="6500"))
    assert d.category_id is None
    assert "project override" in d.reason
    assert "not found" in d.reason


def test_resolve_supplier_aufwand_miss_returns_none_not_fallback():
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=False, project=None,
                  supplier=_supplier(aufwand="9998"))
    assert d.category_id is None
    assert "supplier override" in d.reason
    assert "not found" in d.reason
    # credit_account stays set on a miss so the batch table can show
    # WHICH account failed to map (`✗ 9998 (supplier)`).
    assert d.source == "supplier"
    assert d.credit_account == "9998"


def test_resolve_supplier_with_blank_aufwand_uses_4000_fallback():
    r = MocoCategoryResolver(CATEGORIES)
    for value in ("", "   "):
        d = r.resolve(already_paid_by_card=False, project=None,
                      supplier=_supplier(aufwand=value))
        assert d.category_id == 17


def test_resolve_handles_numeric_supplier_aufwand():
    """Moco may store numeric custom-property values as ints."""
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=False, project=None,
                  supplier=_supplier(aufwand=6500))
    assert d.category_id == 19


def test_resolve_no_project_uses_4000_fallback():
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=False, project=None)
    assert d.category_id == 17  # the 4000-account category
    assert d.reason.startswith("default")


def test_resolve_project_without_aufwand_uses_4000_fallback():
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=False,
                  project=_project(aufwand=None))
    assert d.category_id == 17


def test_resolve_project_with_blank_aufwand_uses_4000_fallback():
    """An Aufwandkonto custom-property set to "" / "   " is treated as
    "not set", not as a miss — operator may have cleared the field."""
    r = MocoCategoryResolver(CATEGORIES)
    for value in ("", "   "):
        d = r.resolve(already_paid_by_card=False,
                      project=_project(aufwand=value))
        assert d.category_id == 17


def test_resolve_fallback_missing_returns_none():
    """No category in the catalog uses credit_account=4000 → omit."""
    cats = [{"id": 20, "credit_account": "9999", "label": "weird account"}]
    r = MocoCategoryResolver(cats)
    d = r.resolve(already_paid_by_card=False, project=None)
    assert d.category_id is None
    assert "not in catalog" in d.reason


def test_resolve_normalizes_whitespace_in_aufwand_and_credit_account():
    """Trim both sides so a typo like `'4500 '` still matches `'4500'`."""
    cats = [{"id": 18, "credit_account": " 4500 "}]
    r = MocoCategoryResolver(cats)
    d = r.resolve(already_paid_by_card=False,
                  project=_project(aufwand=" 4500\n"))
    assert d.category_id == 18


def test_resolve_handles_numeric_aufwand_custom_field():
    """Moco may store numeric custom-property values as ints."""
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=False,
                  project=_project(aufwand=4500))
    assert d.category_id == 18


def test_decision_dataclass_is_frozen():
    d = CategoryDecision(None, "x")
    with pytest.raises(Exception):
        d.reason = "mutated"  # type: ignore[misc]
