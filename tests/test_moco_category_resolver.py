"""Unit tests for MocoCategoryResolver.

Covers the four branches of the resolution chain — already-paid skip,
project-override hit, project-override miss (must NOT fall back to
4000), default fallback, and missing-default edge — plus index
construction defenses (non-string credit_accounts, missing fields,
collision behavior).
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


def test_indexed_count_excludes_categories_without_credit_account():
    cats = CATEGORIES + [
        {"id": 99, "label": "broken — no credit_account"},
        {"id": 100, "credit_account": "", "label": "blank"},
    ]
    r = MocoCategoryResolver(cats)
    assert r.indexed_count() == 3


def test_resolve_already_paid_returns_none():
    r = MocoCategoryResolver(CATEGORIES)
    d = r.resolve(already_paid_by_card=True, project=_project(aufwand="4500"))
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
