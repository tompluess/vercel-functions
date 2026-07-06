"""Unit tests for MocoSupplierMatcher — the three-tier supplier-name match
(exact → substring → normalized token-set), unique-hit-per-tier semantics,
and the ambiguity stop."""

from api.moco_supplier_matcher import MocoSupplierMatcher


def matcher(*names_or_dicts) -> MocoSupplierMatcher:
    companies = [
        n if isinstance(n, dict) else {"id": i + 1, "name": n}
        for i, n in enumerate(names_or_dicts)
    ]
    return MocoSupplierMatcher(companies)


# --- tier 1: exact ------------------------------------------------------------

def test_exact_match_is_case_insensitive():
    m = matcher("FLYERALARM", "Other AG").match("flyeralarm")
    assert m.status == "matched"
    assert m.tier == "exact"
    assert m.company["name"] == "FLYERALARM"


def test_exact_match_collapses_whitespace():
    m = matcher("Debrunner  Acifer AG").match("  Debrunner Acifer   AG ")
    assert m.status == "matched"
    assert m.tier == "exact"


def test_exact_wins_over_a_looser_substring_candidate():
    """A unique exact hit links even when a second company would also hit
    at the substring tier — tiering means the strictest signal decides."""
    m = matcher("Flyeralarm", "Flyeralarm GmbH").match("Flyeralarm")
    assert m.status == "matched"
    assert m.tier == "exact"
    assert m.company["name"] == "Flyeralarm"


def test_duplicate_exact_names_are_ambiguous():
    """Two Moco companies with the same name (duplicate registration) →
    ambiguous, no auto-link, both surfaced as candidates."""
    m = matcher("FLYERALARM", "FLYERALARM").match("FLYERALARM")
    assert m.status == "ambiguous"
    assert m.tier == "exact"
    assert m.candidate_count == 2
    assert len(m.candidates) == 2
    assert m.company is None


# --- tier 2: substring --------------------------------------------------------

def test_substring_ocr_name_inside_company_name():
    """OCR drops the legal form: 'Brack.ch' ⊂ 'BRACK.CH AG' (compared on
    the alnum-collapsed forms, so the dot doesn't matter)."""
    m = matcher("BRACK.CH AG", "Other AG").match("Brack.ch")
    assert m.status == "matched"
    assert m.tier == "substring"
    assert m.company["name"] == "BRACK.CH AG"


def test_substring_company_name_inside_ocr_name():
    """Invoice letterhead appends the location: the shorter Moco record
    sits inside the OCR'd name."""
    m = matcher("Debrunner Acifer AG", "Other AG").match(
        "Debrunner Acifer AG, 3014 Bern")
    assert m.status == "matched"
    assert m.tier == "substring"


def test_substring_ambiguity_stops_without_trying_tier_3():
    """Two containment hits → ambiguous; the matcher must NOT fall
    through to the looser normalized tier (conservative semantics,
    same as MocoProjectResolver / SmartmeProjectMatcher)."""
    m = matcher("Müller Elektro AG Zürich", "Müller Elektro").match(
        "Müller Elektro AG")
    assert m.status == "ambiguous"
    assert m.tier == "substring"
    assert m.candidate_count == 2


def test_substring_requires_min_length_on_both_sides():
    """Two-letter fragments would sit inside half the supplier list —
    below MIN_SUBSTRING_LEN neither direction fires."""
    assert matcher("AG Immobilien Verwaltung").match("AG").status == "no_match"
    # And a two-letter *company* name never hits as containment target.
    assert matcher("AG").match("Agrola Brennstoffe").status == "no_match"


# --- tier 3: normalized token-set ----------------------------------------------

def test_normalized_matches_reordered_tokens_with_umlaut_and_legal_noise():
    """'Mueller + Partner AG' vs 'Partner Müller': substring can't hit
    (reordered), but the folded core token sets are equal."""
    m = matcher("Partner Müller", "Other AG").match("Mueller + Partner AG")
    assert m.status == "matched"
    assert m.tier == "normalized"
    assert m.company["name"] == "Partner Müller"


def test_normalized_drops_legal_form_tokens():
    """'Meier Sanitär GmbH' vs 'Sanitaer Meier & Co.' — legal-form and
    connector tokens carry no identity."""
    m = matcher("Sanitaer Meier & Co.").match("Meier Sanitär GmbH")
    assert m.status == "matched"
    assert m.tier == "normalized"


def test_normalized_does_not_match_on_empty_core():
    """A name that is all legal-form filler must not equal another
    all-filler name — empty sets comparing equal would be a false link."""
    assert matcher("AG & Co. KG").match("GmbH & Co. KG").status == "no_match"


def test_normalized_token_subset_is_not_enough():
    """Equality, not subset: 'Meier Holzbau' must not link 'Meier Dach
    und Holzbau AG' — the extra token means it may be a different firm.
    (Not a substring hit either: 'dach' breaks containment.)"""
    m = matcher("Meier Dach und Holzbau AG").match("Meier Holzbau")
    assert m.status == "no_match"


def test_normalized_ambiguity_reports_candidates():
    """Both candidates reorder the needle's tokens (so the substring tier
    can't see them) and tie on core-token equality → ambiguous."""
    m = matcher("Bau Meier AG", "Bau, Meier GmbH").match("Meier Bau")
    assert m.status == "ambiguous"
    assert m.tier == "normalized"
    assert {c["name"] for c in m.candidates} == {"Bau Meier AG",
                                                 "Bau, Meier GmbH"}


# --- edge cases ---------------------------------------------------------------

def test_no_match_at_any_tier():
    m = matcher("FLYERALARM", "BRACK.CH AG").match("Galaxus")
    assert m.status == "no_match"
    assert m.tier is None
    assert m.company is None


def test_blank_and_none_input_report_empty():
    assert matcher("X AG").match("").status == "empty"
    assert matcher("X AG").match("   ").status == "empty"
    assert matcher("X AG").match(None).status == "empty"


def test_companies_without_a_name_are_skipped():
    m = MocoSupplierMatcher([{"id": 1}, {"id": 2, "name": ""},
                             {"id": 3, "name": "FLYERALARM"}])
    assert m.indexed_count() == 1
    assert m.match("FLYERALARM").company["id"] == 3


def test_empty_company_list_reports_no_match():
    assert MocoSupplierMatcher([]).match("FLYERALARM").status == "no_match"
