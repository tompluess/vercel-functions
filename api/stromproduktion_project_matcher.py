"""StromproduktionProjectMatcher — map an EVU credit note to one
"Stromproduktion" Moco project.

An EVU (local energy supplier, e.g. CKW) production credit note carries an
`Objekt` line (e.g. "Produktion PVA HEIV Meierhofweg 10") and an OCR'd
supplier name (e.g. "CKW AG"). The matching Moco project is tagged
`Stromproduktion` and its `customer` is that same EVU — but *not* the same
Moco company record the supplier matcher linked: Moco can hold two separate
company records for one real-world entity, one `type: "customer"` (used as
`project.customer`) and one `type: "supplier"` (used by
`MocoSupplierMatcher`/`list_suppliers`), sharing a name but not an id (e.g.
"CKW AG" id `762378092` vs "CKW AG (Lieferant)" id `762378104` — both
tagged `Lokaler Energieversorger (EVU)`, confirmed live). So project
matching goes through **name**, never company-id equality.

Three tiers, modeled on `SmartmeProjectMatcher`:

1. **Kommission custom-field equality** (operator pin/override). Checked
   against ALL `Stromproduktion`-tagged projects regardless of supplier —
   an explicit pin always wins. A unique hit → matched; several → ambiguous.
2. **Customer-name filter** (required, not optional): only projects whose
   `customer.name` plausibly matches the OCR'd `supplier_name` participate
   further (fold + alnum + legal-form-stripped token-set comparison,
   condensed from `MocoSupplierMatcher`'s three tiers into a single
   boolean — this is a *filter*, not a unique-hit selector, so ambiguity
   semantics don't apply here). Zero candidates → `no_match`: an unrelated
   EVU's credit must never fall through to someone else's
   `Stromproduktion` project.
3. **Best-token-overlap** between the OCR'd `Objekt` and each surviving
   candidate's project `name` (`MIN_TOKEN_LEN=1`, same rationale as
   `SmartmeProjectMatcher` — bare house numbers disambiguate neighbouring
   installations of the same EVU, e.g. "Meierhofweg" + "10"). Unique max →
   matched; a tie → ambiguous; no overlap → `no_match`.

   Tokenization here deliberately does NOT reuse `moco_project_resolver`'s
   plain `_tokens` (which only splits on non-alphanumerics): Moco's
   Contracting-project names are compact identifiers that fuse the street
   and house number with no separator (`"Meierhofweg10_Emmen"`), while
   OCR'd Objekt text from an EVU statement spaces them
   (`"Meierhofweg 10"`) — verified live on draft `3143995` against project
   `Meierhofweg10_Emmen Contracting/Einspeisung`. Plain non-alphanumeric
   splitting would tokenize these as `"meierhofweg10"` vs
   `"meierhofweg"`+`"10"` and find zero overlap. `_addr_tokens` below
   additionally splits at letter/digit boundaries so both sides agree.

A blank/unresolvable `Objekt` → `empty` (nothing to disambiguate on, same
semantics as `SmartmeProjectMatcher`).
"""

import re
from dataclasses import dataclass

from api.moco_project_resolver import _normalize

TAG_STROMPRODUKTION = "stromproduktion"
KOMMISSION_FIELD = "Kommission"
MIN_SUBSTRING_LEN = 3

# Same normalization forms as `moco_supplier_matcher.py` — duplicated
# locally (small, ~10 lines) rather than importing another module's
# private underscored names, matching how `SmartmeProjectMatcher` already
# duplicates its own helpers instead of reaching into `MocoProjectResolver`.
_STRIP_RE = re.compile(r"[\W_]+", flags=re.UNICODE)
_ACCENT_TRANS = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue",
    "é": "e", "è": "e", "ê": "e", "ë": "e",
    "à": "a", "â": "a",
    "î": "i", "ï": "i",
    "ô": "o", "û": "u", "ù": "u",
    "ç": "c",
})
_LEGAL_FORM_TOKENS = {
    "ag", "gmbh", "sa", "sarl", "sagl", "kg", "co", "cie",
    "inc", "ltd", "llc", "se", "ug",
    "und", "and", "et",
}


def _fold(value: str) -> str:
    return value.casefold().translate(_ACCENT_TRANS)


def _alnum(name: str | None) -> str:
    if not name:
        return ""
    return _STRIP_RE.sub("", _fold(name))


def _core_tokens(name: str | None) -> frozenset[str]:
    if not name:
        return frozenset()
    tokens = {t for t in _STRIP_RE.split(_fold(name)) if t}
    return frozenset(tokens - _LEGAL_FORM_TOKENS)


# Zero-width boundary between a letter run and a digit run (either
# direction) — see the module docstring for why this is needed (Moco's
# "Meierhofweg10" vs OCR's "Meierhofweg 10").
_ADDR_BOUNDARY_RE = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])",
                               flags=re.UNICODE)
_ADDR_SPLIT_RE = re.compile(r"[\W_]+", flags=re.UNICODE)


def _addr_tokens(value: str | None, *, min_len: int) -> set[str]:
    """Tokenize for the address-overlap tier, splitting at letter/digit
    boundaries in addition to non-alphanumerics. See module docstring."""
    if not value:
        return set()
    spaced = _ADDR_BOUNDARY_RE.sub(" ", value)
    return {t.casefold() for t in _ADDR_SPLIT_RE.split(spaced)
            if len(t) >= min_len}


def _names_match(a: str | None, b: str | None) -> bool:
    """True when two company names plausibly refer to the same entity.

    Condensed form of `MocoSupplierMatcher`'s exact/substring/normalized
    tiers into a single boolean — used here to *filter* candidate
    projects (is this project's customer the EVU that sent the credit
    note?), not to uniquely select a company, so ambiguity semantics
    don't apply.
    """
    alnum_a, alnum_b = _alnum(a), _alnum(b)
    if not alnum_a or not alnum_b:
        return False
    if alnum_a == alnum_b:
        return True
    if (len(alnum_a) >= MIN_SUBSTRING_LEN and len(alnum_b) >= MIN_SUBSTRING_LEN
            and (alnum_a in alnum_b or alnum_b in alnum_a)):
        return True
    core_a, core_b = _core_tokens(a), _core_tokens(b)
    return bool(core_a) and core_a == core_b


def has_stromproduktion_tag(project: dict) -> bool:
    """True when the project carries the `Stromproduktion` tag (casefold)."""
    tags = project.get("tags")
    if not isinstance(tags, list):
        return False
    return TAG_STROMPRODUKTION in {str(t).casefold() for t in tags}


@dataclass(frozen=True)
class StromproduktionProjectMatch:
    """Outcome of `StromproduktionProjectMatcher.match(...)`.

    `status` is one of `"matched"` / `"ambiguous"` / `"no_match"` /
    `"empty"`. `tier` reports which tier decided (`"kommission"` /
    `"tokens"`; None for no_match/empty).
    """
    project: dict | None
    status: str
    candidate_count: int
    tier: str | None = None


class StromproduktionProjectMatcher:
    # Keep every token, including bare house numbers — see module docstring.
    MIN_TOKEN_LEN = 1

    def __init__(self, projects: list[dict]):
        # (project, customer_name, name_tokens) for every Stromproduktion
        # project. Projects without the tag are invisible to the matcher.
        self._candidates: list[tuple[dict, str | None, set[str]]] = []
        # normalized Kommission -> list[project], for tier 0 (pin).
        self._kommission_index: dict[str, list[dict]] = {}
        for p in projects:
            if not has_stromproduktion_tag(p):
                continue
            customer = p.get("customer")
            customer_name = (customer.get("name")
                             if isinstance(customer, dict) else None)
            name_tokens = _addr_tokens(p.get("name"), min_len=self.MIN_TOKEN_LEN)
            self._candidates.append((p, customer_name, name_tokens))
            kommission = _normalize(self._kommission_of(p))
            if kommission:
                self._kommission_index.setdefault(kommission, []).append(p)

    @staticmethod
    def _kommission_of(project: dict) -> str | None:
        props = project.get("custom_properties")
        if not isinstance(props, dict):
            return None
        raw = props.get(KOMMISSION_FIELD)
        if raw is None or raw == "":
            return None
        return str(raw)

    def indexed_count(self) -> int:
        """Number of Stromproduktion-tagged projects in the index."""
        return len(self._candidates)

    def has_candidate_for_supplier(self, supplier_name: str | None) -> bool:
        """Cheap existence check: does ANY `Stromproduktion` project's
        customer plausibly match `supplier_name`?

        Used as a detection fallback (see `EnergyCreditNoteService.
        has_matching_project` / `is_energy_credit_note`'s call sites) for
        when a supplier's Moco company record isn't (yet) tagged
        `Lokaler Energieversorger (EVU)`. Confirmed live: Moco can hold
        the EVU tag on one of an entity's two company records but not the
        other (EGBB's `type: "customer"` record was tagged, its
        `type: "supplier"` "(Lieferant)" record — the one
        `MocoSupplierMatcher` actually links — had `tags: []`). Relying on
        "does a Stromproduktion project actually exist for this supplier"
        instead of a possibly-incomplete tag is both more robust and
        operationally exact: it can only be true when there's a real
        project to route to.
        """
        return any(_names_match(supplier_name, customer_name)
                   for _, customer_name, _ in self._candidates)

    def match(self, *, supplier_name: str | None,
             objekt: str | None) -> StromproduktionProjectMatch:
        # Tier 0 — operator-pinned Kommission equality against the full
        # Objekt string, checked across ALL Stromproduktion projects
        # regardless of supplier — an explicit pin always wins.
        objekt_norm = _normalize(objekt)
        if objekt_norm:
            pinned = self._dedupe(self._kommission_index.get(objekt_norm, []))
            if len(pinned) == 1:
                return StromproduktionProjectMatch(pinned[0], "matched", 1,
                                                    "kommission")
            if len(pinned) > 1:
                return StromproduktionProjectMatch(None, "ambiguous",
                                                    len(pinned), "kommission")

        objekt_tokens = _addr_tokens(objekt, min_len=self.MIN_TOKEN_LEN)
        if not objekt_tokens:
            return StromproduktionProjectMatch(None, "empty", 0, None)

        # Tier 1 — required filter: only this EVU's own Stromproduktion
        # projects participate in disambiguation.
        supplier_candidates = [
            (project, name_tokens)
            for project, customer_name, name_tokens in self._candidates
            if _names_match(supplier_name, customer_name)
        ]
        if not supplier_candidates:
            return StromproduktionProjectMatch(None, "no_match", 0, None)

        # Tier 2 — best token overlap against project names.
        best_score = 0
        winners: list[dict] = []
        for project, name_tokens in supplier_candidates:
            score = len(objekt_tokens & name_tokens)
            if score == 0:
                continue
            if score > best_score:
                best_score = score
                winners = [project]
            elif score == best_score:
                winners.append(project)

        if not winners:
            return StromproduktionProjectMatch(None, "no_match", 0, None)
        if len(winners) == 1:
            return StromproduktionProjectMatch(winners[0], "matched", 1,
                                                "tokens")
        return StromproduktionProjectMatch(None, "ambiguous", len(winners),
                                            "tokens")

    @staticmethod
    def _dedupe(projects: list[dict]) -> list[dict]:
        seen: set = set()
        out: list[dict] = []
        for p in projects:
            pid = p.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            out.append(p)
        return out
