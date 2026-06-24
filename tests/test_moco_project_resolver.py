"""Unit tests for MocoProjectResolver.

Covers index construction (custom-properties extraction, missing-field
projects skipped, non-string values coerced), the exact + substring
matching tiers, ambiguity handling at each tier, and the four `status`
outcomes (`matched` / `ambiguous` / `no_match` / `empty`).
"""

import pytest

from api.moco_project_resolver import MocoProjectResolver, ProjectMatch


def _proj(pid: int, name: str, kommission: str | int | None) -> dict:
    """Shape-faithful Moco project: top-level fields + custom_properties."""
    proj: dict = {"id": pid, "name": name}
    if kommission is not None:
        proj["custom_properties"] = {"Kommission": kommission}
    return proj


@pytest.fixture
def projects() -> list[dict]:
    return [
        _proj(1, "Sanierung Hauptstrasse", "2025-031"),
        _proj(2, "Neubau Müller", "Müller AG / Bv-12"),
        _proj(3, "Garage Bühler", " 2025-042 "),
        _proj(4, "no-kommission project", None),
        _proj(5, "empty kommission", ""),
        _proj(6, "numeric kommission", 12345),
    ]


def test_indexed_count_includes_name_fallbacks(projects):
    r = MocoProjectResolver(projects)
    # All six projects in the fixture have a name; the four with a
    # Kommission are indexed by that value, the remaining two (#4, #5)
    # fall back to their name. So all six get indexed.
    assert r.indexed_count() == 6


def test_resolve_empty_returns_empty_status():
    r = MocoProjectResolver([_proj(1, "x", "2025-031")])
    for raw in (None, "", "   "):
        m = r.resolve(raw)
        assert m.status == "empty"
        assert m.project is None
        assert m.candidate_count == 0
        assert m.tier is None


def test_resolve_exact_match_case_insensitive(projects):
    r = MocoProjectResolver(projects)
    m = r.resolve("2025-031")
    assert m.status == "matched"
    assert m.tier == "exact"
    assert m.project["id"] == 1


def test_resolve_matches_across_punctuation_and_descriptive_prefix():
    """Regression: project `#Haldenweg12_Jegensdorf` should match the
    OCR'd `PVA Haldenweg 12_Jegensdorf` (descriptive `PVA ` prefix,
    space after `Haldenweg`, leading `#` on the Moco side).

    Aggressive normalization strips all non-alphanumerics, so both
    collapse to `haldenweg12jegensdorf` / `pvahaldenweg12jegensdorf`
    and the substring fallback succeeds.
    """
    projects = [_proj(1, "Haldenweg", "#Haldenweg12_Jegensdorf")]
    r = MocoProjectResolver(projects)
    m = r.resolve("PVA Haldenweg 12_Jegensdorf")
    assert m.status == "matched"
    assert m.tier == "substring"
    assert m.project["id"] == 1


def test_resolve_noisy_ocr_result_substring_match():
    """Regression: Noisy OCR Kommission should match a lengthy project name by substring.
    """
    projects = [_proj(1, "P25031 PVA & Batteriespeicher, Stroppelstrasse19, Untersiggenthal", None)]
    r = MocoProjectResolver(projects)
    m = r.resolve("Gutschrift zur AB 2025-2013338, Stroppelstrasse19_Untersiggenthal")
    assert m.status == "matched"
    assert m.tier == "token-overlap"
    assert m.project["id"] == 1


def test_resolve_exact_match_normalizes_whitespace(projects):
    r = MocoProjectResolver(projects)
    # The Garage Bühler kommission is " 2025-042 " (stored with padding);
    # OCR may emit "2025  042" / "2025-042" / "  2025-042"
    m = r.resolve("  2025-042  ")
    assert m.status == "matched"
    assert m.project["id"] == 3


def test_resolve_numeric_custom_field_value(projects):
    r = MocoProjectResolver(projects)
    m = r.resolve("12345")
    assert m.status == "matched"
    assert m.project["id"] == 6


def test_resolve_substring_fallback_ocr_in_key():
    # OCR'd value is a substring of an indexed Kommission.
    projects = [_proj(1, "Müller", "Müller AG / Bv-12")]
    r = MocoProjectResolver(projects)
    m = r.resolve("Müller AG")
    assert m.status == "matched"
    assert m.tier == "substring"
    assert m.project["id"] == 1


def test_resolve_substring_fallback_key_in_ocr():
    # Indexed Kommission is a substring of the OCR'd value.
    projects = [_proj(1, "x", "ABC-123")]
    r = MocoProjectResolver(projects)
    m = r.resolve("Auftragsnummer ABC-123 Lieferschein 42")
    assert m.status == "matched"
    assert m.tier == "substring"
    assert m.project["id"] == 1


def test_resolve_exact_ambiguous_reports_count():
    projects = [
        _proj(1, "Project A", "shared"),
        _proj(2, "Project B", "shared"),
    ]
    r = MocoProjectResolver(projects)
    m = r.resolve("Shared")
    assert m.status == "ambiguous"
    assert m.tier == "exact"
    assert m.candidate_count == 2
    assert m.project is None


def test_resolve_substring_ambiguous_reports_distinct_count():
    projects = [
        _proj(1, "A", "ABC"),
        _proj(2, "B", "ABCDEF"),
    ]
    r = MocoProjectResolver(projects)
    # Both keys are substrings of the OCR string → ambiguous
    m = r.resolve("ABCDEF-extra")
    assert m.status == "ambiguous"
    assert m.tier == "substring"
    assert m.candidate_count == 2


def test_resolve_no_match():
    projects = [_proj(1, "A", "totally-different")]
    r = MocoProjectResolver(projects)
    m = r.resolve("nothing-matches-here")
    assert m.status == "no_match"
    assert m.project is None
    assert m.tier is None


def test_resolve_exact_wins_over_substring():
    # Project 1 is an exact match; project 2 is a substring fallback that
    # would otherwise match too. Exact tier must short-circuit.
    projects = [
        _proj(1, "exact", "Kommission-A"),
        _proj(2, "sub", "Kommission-A-extra"),
    ]
    r = MocoProjectResolver(projects)
    m = r.resolve("kommission-a")
    assert m.status == "matched"
    assert m.tier == "exact"
    assert m.project["id"] == 1


def test_resolver_falls_back_to_name_when_kommission_missing():
    # When the Kommission custom-field is missing/empty, the project name
    # serves as the index key — both at exact and substring tiers.
    r = MocoProjectResolver([{"id": 99, "name": "Sanierung Hauptstrasse"}])
    assert r.indexed_count() == 1
    m = r.resolve("Sanierung Hauptstrasse")
    assert m.status == "matched"
    assert m.tier == "exact"
    assert m.project["id"] == 99


def test_resolver_skips_projects_with_neither_kommission_nor_name():
    # Defensive: nothing to index → not indexed, no_match on lookup.
    r = MocoProjectResolver([{"id": 1}])
    assert r.indexed_count() == 0
    assert r.resolve("anything").status == "no_match"


def test_kommission_wins_over_name_when_both_present():
    # Project has both a Kommission and a name; the Kommission value is
    # what gets indexed (the name fallback only kicks in when Kommission
    # is missing/empty), so a lookup against the name doesn't match.
    projects = [{"id": 7, "name": "Bauvorhaben Müller",
                 "custom_properties": {"Kommission": "K-99"}}]
    r = MocoProjectResolver(projects)
    assert r.resolve("K-99").status == "matched"
    # The name is NOT indexed when Kommission is set — looking up the
    # name returns no_match (no other project has it).
    assert r.resolve("Bauvorhaben Müller").status == "no_match"


def test_custom_field_label_override():
    # The default field label is "Kommission", but a future call site might
    # need to point at a different custom field.
    projects = [{"id": 1, "name": "x",
                 "custom_properties": {"Objektnummer": "OB-99"}}]
    r = MocoProjectResolver(projects, custom_field_label="Objektnummer")
    m = r.resolve("OB-99")
    assert m.status == "matched"
    assert m.project["id"] == 1


def test_match_dataclass_is_frozen():
    m = ProjectMatch(None, "empty", 0, None)
    with pytest.raises(Exception):
        m.status = "matched"  # type: ignore[misc]
