"""Unit tests for SmartmeProjectMatcher — label filtering, best-overlap
scoring (incl. the Oberkirch shared-village regression), tie → ambiguous,
and the project_energy_label helper."""

from api.smartme_project_matcher import (
    SmartmeProjectMatcher,
    project_energy_label,
)


# Mirrors the real ZEV/Eigenverbrauch project landscape (names + tags as
# returned by GET /projects). The four Oberkirch projects share a village
# token — the exact failure mode MocoProjectResolver's any-shared-token
# tier can't handle.
PROJECTS = [
    {"id": 1, "name": "Haulihof Solarstrom Eigenverbrauch",
     "tags": ["Eigenverbrauch", "Stromproduktion"]},
    {"id": 2, "name": "Hauptstrasse 33, Leimbach, Solarstrom Eigenverbrauch",
     "tags": ["Contracting", "Eigenverbrauch", "Stromproduktion"]},
    {"id": 3, "name": "Krugel1_Oberkirch - Contracting/Einspeisung",
     "tags": ["Contracting", "Stromproduktion", "ZEV"]},
    {"id": 4, "name": "ZEV Strombezug, Bauhaus / Landwirtschaft, Krugel 1, "
                      "Oberkirch",
     "tags": ["ZEV"]},
    {"id": 5, "name": "ZEV Strombezug, Blumenrain 1, Oberkirch",
     "tags": ["ZEV"]},
    {"id": 6, "name": "ZEV Strombezug, Dogelzwil 2, Oberkirch",
     "tags": ["ZEV"]},
    # Unlabeled decoy — must be invisible to the matcher even though its
    # name would score on address tokens.
    {"id": 99, "name": "Sanierung Hauptstrasse 33, Leimbach",
     "tags": ["Sanierung"]},
]


def test_indexes_only_energy_labeled_projects():
    m = SmartmeProjectMatcher(PROJECTS)
    assert m.indexed_count() == 6


def test_labels_match_case_insensitively():
    m = SmartmeProjectMatcher([
        {"id": 1, "name": "Testprojekt Eins", "tags": ["zev"]},
        {"id": 2, "name": "Testprojekt Zwei", "tags": ["EIGENVERBRAUCH"]},
    ])
    assert m.indexed_count() == 2


def test_real_sample_objekt_matches_unique_best_score():
    """The production sample: PDF Objekt with parenthesized address →
    the Leimbach project wins on {hauptstrasse, 33, leimbach}, and the
    unlabeled Sanierung decoy can't steal the match."""
    m = SmartmeProjectMatcher(PROJECTS)
    match = m.match("Gesamtverbrauch (Hauptstrasse 33 Leimbach)")
    assert match.status == "matched"
    assert match.project["id"] == 2
    assert match.score == 3


def test_shared_village_token_does_not_cause_ambiguity():
    """Regression: four projects share "Oberkirch"; the street token must
    break the tie instead of reporting all of them as candidates."""
    m = SmartmeProjectMatcher(PROJECTS)
    match = m.match("Blumenrain 1 (Oberkirch)")
    assert match.status == "matched"
    assert match.project["id"] == 5


def test_house_number_token_disambiguates():
    """Single-digit house numbers survive tokenization (MIN_TOKEN_LEN=1)
    and tip the score between neighbouring installations."""
    m = SmartmeProjectMatcher(PROJECTS)
    match = m.match("Bauhaus / Landwirtschaft, Krugel 1, Oberkirch")
    assert match.status == "matched"
    assert match.project["id"] == 4


def test_top_score_tie_is_ambiguous():
    m = SmartmeProjectMatcher([
        {"id": 1, "name": "ZEV Strombezug, Blumenrain 1, Oberkirch",
         "tags": ["ZEV"]},
        {"id": 2, "name": "ZEV Strombezug, Blumenrain 3, Oberkirch",
         "tags": ["ZEV"]},
    ])
    # "Blumenrain (Oberkirch)" overlaps both equally — no house number to
    # break the tie.
    match = m.match("Blumenrain (Oberkirch)")
    assert match.status == "ambiguous"
    assert match.project is None
    assert match.candidate_count == 2


def test_no_token_overlap_is_no_match():
    m = SmartmeProjectMatcher(PROJECTS)
    match = m.match("Solarpark Zermatt")
    assert match.status == "no_match"
    assert match.project is None
    assert match.score == 0


def test_blank_objekt_is_empty():
    m = SmartmeProjectMatcher(PROJECTS)
    assert m.match(None).status == "empty"
    assert m.match("").status == "empty"
    assert m.match("  / _ ").status == "empty"


def test_project_energy_label_variants():
    assert project_energy_label({"tags": ["ZEV"]}) == "ZEV"
    assert project_energy_label({"tags": ["eigenverbrauch"]}) == "Eigenverbrauch"
    # ZEV wins when both labels are present (broader billing scope).
    assert project_energy_label(
        {"tags": ["Eigenverbrauch", "ZEV"]}) == "ZEV"
    assert project_energy_label({"tags": ["Sanierung"]}) is None
    assert project_energy_label({"tags": None}) is None
    assert project_energy_label({}) is None
