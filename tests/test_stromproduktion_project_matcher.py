"""Unit tests for StromproduktionProjectMatcher — the customer-name filter
tier (incl. the two-different-company-records-same-name case), token-overlap
disambiguation among one EVU's own projects, the Kommission pin, and the
has_stromproduktion_tag helper."""

from api.stromproduktion_project_matcher import (
    StromproduktionProjectMatcher,
    has_stromproduktion_tag,
)


# Mirrors the real Stromproduktion project landscape (names + customer +
# tags as returned by GET /projects, pulled live from the account this
# feature was built against).
PROJECTS = [
    {"id": 1, "name": "Meierhofweg10_Emmen Contracting/Einspeisung",
     "tags": ["Contracting", "Stromproduktion"],
     "customer": {"id": 762378092, "name": "CKW AG"},
     "custom_properties": {"Kommission": None}},
    {"id": 2, "name": "Lindershalde_Rengg Contracting/Einspeisung",
     "tags": ["Contracting", "Stromproduktion"],
     "customer": {"id": 762378092, "name": "CKW AG"},
     "custom_properties": {"Kommission": None}},
    {"id": 3, "name": "Krugel1_Oberkirch - Contracting/Einspeisung",
     "tags": ["Contracting", "Stromproduktion", "ZEV"],
     "customer": {"id": 762378092, "name": "CKW AG"},
     "custom_properties": {"Kommission": None}},
    {"id": 4, "name": "Hauptstrasse33_Leimbach Contracting/Einspeisung",
     "tags": ["Contracting", "Stromproduktion"],
     "customer": {"id": 900000001, "name": "AEW Energie AG"},
     "custom_properties": {"Kommission": "#Hauptstrasse33_Leimbach"}},
    {"id": 5, "name": "Haulihof - EGBB Einspeisung",
     "tags": ["Contracting", "Stromproduktion"],
     "customer": {"id": 900000002,
                  "name": "EGBB Elektrizitäts Genossenschaft Boswil Bünzen"},
     "custom_properties": {"Kommission": None}},
    # Unlabeled decoy — must be invisible even though its customer + name
    # tokens would otherwise match.
    {"id": 99, "name": "Meierhofweg10_Emmen Sanierung",
     "tags": ["Sanierung"],
     "customer": {"id": 762378092, "name": "CKW AG"},
     "custom_properties": {}},
]

# The OCR'd supplier name comes from `MocoSupplierMatcher`, which links a
# *different* Moco company record than the project's customer — real
# example: "CKW AG (Lieferant)" (type=supplier, id 762378104) vs the
# project's customer "CKW AG" (type=customer, id 762378092). Both are
# tagged "Lokaler Energieversorger (EVU)"; only the NAME lines up.
CKW_SUPPLIER_NAME = "CKW AG (Lieferant)"


def test_indexes_only_stromproduktion_tagged_projects():
    m = StromproduktionProjectMatcher(PROJECTS)
    assert m.indexed_count() == 5


def test_supplier_name_matches_customer_despite_different_company_id():
    """The real CKW case: OCR'd/matched supplier name carries a
    "(Lieferant)" suffix and a different company id than the project's
    customer, but the names still line up via substring matching."""
    m = StromproduktionProjectMatcher(PROJECTS)
    match = m.match(supplier_name=CKW_SUPPLIER_NAME,
                    objekt="Produktion PVA HEIV Meierhofweg 10")
    assert match.status == "matched"
    assert match.project["id"] == 1
    assert match.tier == "tokens"


def test_objekt_tokens_disambiguate_among_same_customers_projects():
    """CKW has 3 Stromproduktion projects; the address tokens in the
    Objekt must pick the right one, not just "any CKW project"."""
    m = StromproduktionProjectMatcher(PROJECTS)
    match = m.match(supplier_name="CKW AG", objekt="Produktion Lindershalde")
    assert match.status == "matched"
    assert match.project["id"] == 2


def test_unrelated_supplier_does_not_fall_back_to_other_evus_projects():
    """An objekt string that would token-overlap a *different* EVU's
    project must not match when the supplier doesn't own that project —
    zero supplier-filtered candidates is a hard no_match, never a
    fallback across the full Stromproduktion set."""
    m = StromproduktionProjectMatcher(PROJECTS)
    match = m.match(supplier_name="Irgendein Unbekannter EVU AG",
                    objekt="Produktion PVA HEIV Meierhofweg 10")
    assert match.status == "no_match"
    assert match.project is None


def test_supplier_matched_but_no_token_overlap_is_no_match():
    m = StromproduktionProjectMatcher(PROJECTS)
    match = m.match(supplier_name="CKW AG", objekt="Produktion Solarpark Zermatt")
    assert match.status == "no_match"


def test_ambiguous_tie_among_same_customers_projects():
    projects = [
        {"id": 1, "name": "Blumenrain 1 Contracting/Einspeisung",
         "tags": ["Stromproduktion"],
         "customer": {"id": 1, "name": "CKW AG"}},
        {"id": 2, "name": "Blumenrain 3 Contracting/Einspeisung",
         "tags": ["Stromproduktion"],
         "customer": {"id": 1, "name": "CKW AG"}},
    ]
    m = StromproduktionProjectMatcher(projects)
    # "Blumenrain" alone overlaps both equally — no house number to break
    # the tie.
    match = m.match(supplier_name="CKW AG", objekt="Produktion Blumenrain")
    assert match.status == "ambiguous"
    assert match.project is None
    assert match.candidate_count == 2
    assert match.tier == "tokens"


def test_blank_objekt_is_empty():
    m = StromproduktionProjectMatcher(PROJECTS)
    assert m.match(supplier_name="CKW AG", objekt=None).status == "empty"
    assert m.match(supplier_name="CKW AG", objekt="").status == "empty"
    assert m.match(supplier_name="CKW AG", objekt="  / _ ").status == "empty"


def test_blank_supplier_name_is_no_match_not_empty():
    """A blank supplier name can never satisfy the customer-name filter —
    that's a genuine no_match (nothing to link the credit to any project),
    distinct from a blank Objekt (nothing to disambiguate on)."""
    m = StromproduktionProjectMatcher(PROJECTS)
    match = m.match(supplier_name=None, objekt="Produktion Meierhofweg 10")
    assert match.status == "no_match"


# --- Kommission pin tier -------------------------------------------------

def test_kommission_pin_matches_regardless_of_supplier():
    """An operator pin is an explicit override — it wins even when the
    supplied `supplier_name` wouldn't otherwise match that project's
    customer, mirroring SmartmeProjectMatcher's tier-0 precedence."""
    m = StromproduktionProjectMatcher(PROJECTS)
    match = m.match(supplier_name="Completely Unrelated AG",
                    objekt="#Hauptstrasse33_Leimbach")
    assert match.status == "matched"
    assert match.project["id"] == 4
    assert match.tier == "kommission"


def test_duplicate_kommission_pins_are_ambiguous():
    projects = [
        {"id": 1, "name": "Projekt Eins", "tags": ["Stromproduktion"],
         "customer": {"id": 1, "name": "CKW AG"},
         "custom_properties": {"Kommission": "Pin Eins"}},
        {"id": 2, "name": "Projekt Zwei", "tags": ["Stromproduktion"],
         "customer": {"id": 1, "name": "CKW AG"},
         "custom_properties": {"Kommission": "Pin Eins"}},
    ]
    m = StromproduktionProjectMatcher(projects)
    match = m.match(supplier_name="CKW AG", objekt="Pin Eins")
    assert match.status == "ambiguous"
    assert match.tier == "kommission"
    assert match.candidate_count == 2


def test_unlabeled_project_kommission_is_invisible():
    projects = [
        {"id": 1, "name": "Sanierung irgendwo", "tags": ["Sanierung"],
         "customer": {"id": 1, "name": "CKW AG"},
         "custom_properties": {"Kommission": "Pin Eins"}},
        {"id": 2, "name": "Echtes Stromproduktion Projekt Eins",
         "tags": ["Stromproduktion"],
         "customer": {"id": 1, "name": "CKW AG"},
         "custom_properties": {}},
    ]
    m = StromproduktionProjectMatcher(projects)
    # Falls through to the token tier since the pin is on an unlabeled
    # (invisible) project.
    match = m.match(supplier_name="CKW AG", objekt="Projekt Eins")
    assert match.status == "matched"
    assert match.project["id"] == 2
    assert match.tier == "tokens"


# --- has_stromproduktion_tag ----------------------------------------------

def test_has_stromproduktion_tag_case_insensitive():
    assert has_stromproduktion_tag({"tags": ["Stromproduktion"]}) is True
    assert has_stromproduktion_tag({"tags": ["STROMPRODUKTION"]}) is True
    assert has_stromproduktion_tag({"tags": ["ZEV"]}) is False
    assert has_stromproduktion_tag({"tags": None}) is False
    assert has_stromproduktion_tag({}) is False
