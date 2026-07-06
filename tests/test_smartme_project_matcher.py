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


# --- Kommission tier (tier 1) --------------------------------------------------

# The live ambiguity from draft 3071330: the Objekt's generic tokens
# (efh, 2, oberkirch) tie "Dogelzwil 2" with "EFH Krugel 2".
DOGELZWIL_OBJEKT = "EFH Dogelzwil 2 (vZEV Krugel1_Oberkirch)"

OBERKIRCH_PROJECTS = [
    {"id": 3, "name": "Krugel1_Oberkirch - Contracting/Einspeisung",
     "tags": ["Contracting", "Stromproduktion", "ZEV"],
     "custom_properties": {"Kommission": "#Krugel1_Oberkirch"}},
    {"id": 6, "name": "ZEV Strombezug, Dogelzwil 2, Oberkirch",
     "tags": ["ZEV"], "custom_properties": {"Kommission": None}},
    {"id": 7, "name": "ZEV Strombezug, EFH Krugel 2, Oberkirch",
     "tags": ["ZEV"], "custom_properties": {"Kommission": None}},
]


def test_unpinned_dogelzwil_objekt_stays_ambiguous_on_tokens():
    """Without a Kommission pin the generic-token tie persists — the
    baseline this tier exists to fix."""
    m = SmartmeProjectMatcher(OBERKIRCH_PROJECTS)
    match = m.match(DOGELZWIL_OBJEKT)
    assert match.status == "ambiguous"
    assert match.tier == "tokens"


def test_kommission_pin_resolves_the_token_tie():
    projects = [dict(p) for p in OBERKIRCH_PROJECTS]
    projects[1] = dict(projects[1],
                       custom_properties={"Kommission": "EFH Dogelzwil 2"})
    m = SmartmeProjectMatcher(projects)
    match = m.match(DOGELZWIL_OBJEKT)
    assert match.status == "matched"
    assert match.project["id"] == 6
    assert match.tier == "kommission"


def test_community_kommission_substring_does_not_hijack():
    """`#Krugel1_Oberkirch` normalizes to a *substring* of the Objekt's
    parenthesized community name ("vZEV Krugel1_Oberkirch"). Equality
    matching must NOT route the consumer bill to the community project —
    it falls through to the token tier instead."""
    m = SmartmeProjectMatcher(OBERKIRCH_PROJECTS)
    match = m.match(DOGELZWIL_OBJEKT)
    assert match.project is None or match.project["id"] != 3


def test_kommission_matches_parenthesized_site_address():
    """A pin can also equal the paren content (the site address) — the
    Gesamtverbrauch naming style, where the pre-paren part is generic."""
    projects = [
        {"id": 2, "name": "Irgendein Projektname ohne Adresse",
         "tags": ["Eigenverbrauch"],
         "custom_properties": {"Kommission": "Hauptstrasse 33 Leimbach"}},
        {"id": 9, "name": "Sanierung Hauptstrasse 33, Leimbach, Umbau",
         "tags": ["Eigenverbrauch"], "custom_properties": {}},
    ]
    m = SmartmeProjectMatcher(projects)
    match = m.match("Gesamtverbrauch (Hauptstrasse 33 Leimbach)")
    assert match.status == "matched"
    assert match.project["id"] == 2
    assert match.tier == "kommission"


def test_duplicate_kommission_pins_are_ambiguous():
    projects = [
        {"id": 1, "name": "Projekt Eins", "tags": ["ZEV"],
         "custom_properties": {"Kommission": "EFH Dogelzwil 2"}},
        {"id": 2, "name": "Projekt Zwei", "tags": ["ZEV"],
         "custom_properties": {"Kommission": "EFH Dogelzwil 2"}},
    ]
    m = SmartmeProjectMatcher(projects)
    match = m.match(DOGELZWIL_OBJEKT)
    assert match.status == "ambiguous"
    assert match.tier == "kommission"
    assert match.candidate_count == 2


def test_unlabeled_project_kommission_is_invisible():
    projects = [
        {"id": 1, "name": "Sanierung irgendwo", "tags": ["Sanierung"],
         "custom_properties": {"Kommission": "EFH Dogelzwil 2"}},
        {"id": 6, "name": "ZEV Strombezug, Dogelzwil 2, Oberkirch",
         "tags": ["ZEV"], "custom_properties": {}},
    ]
    m = SmartmeProjectMatcher(projects)
    match = m.match(DOGELZWIL_OBJEKT)
    # Falls to the token tier (unique — only one labeled candidate).
    assert match.status == "matched"
    assert match.project["id"] == 6
    assert match.tier == "tokens"


def test_token_matches_report_tokens_tier():
    m = SmartmeProjectMatcher(PROJECTS)
    match = m.match("Gesamtverbrauch (Hauptstrasse 33 Leimbach)")
    assert match.tier == "tokens"
