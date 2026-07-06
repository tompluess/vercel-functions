"""MocoSupplierMatcher — match an OCR'd supplier name to one Moco company.

The OCR'd supplier name rarely matches the Moco company record letter for
letter: invoices print the full legal name plus location ("Debrunner
Acifer AG, Bern") where Moco has the short form, or drop the legal form
("Brack.ch" vs "BRACK.CH AG"), or spell umlauts out ("Mueller" vs
"Müller"). A plain exact match therefore leaves too many purchases
company-less; a fuzzy match risks linking the *wrong* supplier, which
would silently skew supplier reporting. The compromise: three tiers of
increasing tolerance, each of which only links on a **unique** hit.

1. **Exact** — case-insensitive, whitespace-collapsed name equality.
2. **Substring** — containment in either direction after collapsing to
   alphanumerics only (umlauts folded): OCR ⊂ company name catches
   "Brack.ch" → "BRACK.CH AG"; company ⊂ OCR catches "Debrunner Acifer
   AG, 3014 Bern" → "Debrunner Acifer AG". Both sides must be at least
   `MIN_SUBSTRING_LEN` characters — a two-letter fragment ("AG") would
   sit inside half the supplier list.
3. **Normalized** — token-set equality after casefolding, folding
   umlauts/accents (ü→ue, é→e), splitting on non-alphanumerics, and
   dropping legal-form filler tokens (AG, GmbH, SA, Sàrl, & Co, …).
   Catches reordering + punctuation/legal-form noise that substring
   can't ("Mueller + Partner AG" vs "Partner Müller").

Multiple hits at a tier → `ambiguous` and the matcher STOPS (no
fallthrough to a looser tier) — same conservative semantics as
`MocoProjectResolver` and `SmartmeProjectMatcher`: we prefer a
company-less purchase the reviewer links by hand over a mis-linked one.
`candidates` carries the tied companies so diagnostic tooling (batch
table, single-draft script) can show the reviewer what tied.
"""

import re
from dataclasses import dataclass, field

# Same character-class as MocoProjectResolver._STRIP_RE: everything that
# is not a Unicode word character, plus underscore.
_STRIP_RE = re.compile(r"[\W_]+", flags=re.UNICODE)

# Swiss/German/French umlaut + accent folding applied AFTER casefold
# (casefold already turns ß into ss). Written spellings on invoices
# routinely differ from the Moco record on exactly these characters.
_ACCENT_TRANS = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue",
    "é": "e", "è": "e", "ê": "e", "ë": "e",
    "à": "a", "â": "a",
    "î": "i", "ï": "i",
    "ô": "o", "û": "u", "ù": "u",
    "ç": "c",
})

# Legal-form / connector tokens carrying no identity: "Müller Elektro"
# and "Müller Elektro GmbH" are the same supplier. Compared post-folding,
# so "Sàrl" arrives here as "sarl".
_LEGAL_FORM_TOKENS = {
    "ag", "gmbh", "sa", "sarl", "sagl", "kg", "co", "cie",
    "inc", "ltd", "llc", "se", "ug",
    "und", "and", "et",
}


def _fold(value: str) -> str:
    """Casefold + umlaut/accent folding — the base for tiers 2 and 3."""
    return value.casefold().translate(_ACCENT_TRANS)


def _exact_key(name: str | None) -> str:
    """Whitespace-collapsed casefold for the exact tier ("" on None)."""
    if not name:
        return ""
    return " ".join(name.split()).casefold()


def _alnum(name: str | None) -> str:
    """Only letters+digits remain, folded — the substring-tier form."""
    if not name:
        return ""
    return _STRIP_RE.sub("", _fold(name))


def _core_tokens(name: str | None) -> frozenset[str]:
    """Folded tokens minus legal-form filler — the normalized-tier form.

    May legitimately end up empty (a name consisting only of filler,
    like "AG & Co. KG"); callers must treat empty as unmatchable rather
    than letting two empty sets compare equal.
    """
    if not name:
        return frozenset()
    tokens = {t for t in _STRIP_RE.split(_fold(name)) if t}
    return frozenset(tokens - _LEGAL_FORM_TOKENS)


@dataclass(frozen=True)
class SupplierMatch:
    """Outcome of `MocoSupplierMatcher.match(name)`.

    `status` is one of `"matched"` / `"ambiguous"` / `"no_match"` /
    `"empty"`. `tier` reports which tier decided (`"exact"` /
    `"substring"` / `"normalized"`; None for no_match/empty).
    `candidates` lists the companies that tied on an ambiguous outcome
    (or the single winner on matched) for diagnostic output.
    """
    company: dict | None
    status: str
    candidate_count: int
    tier: str | None
    candidates: list[dict] = field(default_factory=list)


class MocoSupplierMatcher:
    # Both sides of a substring comparison must be at least this many
    # alphanumeric characters — below that, containment is noise.
    MIN_SUBSTRING_LEN = 3

    def __init__(self, companies: list[dict]):
        # Precompute all three comparison forms per company. Companies
        # without a usable name are invisible to the matcher.
        self._entries: list[tuple[dict, str, str, frozenset[str]]] = []
        for c in companies:
            name = c.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            self._entries.append(
                (c, _exact_key(name), _alnum(name), _core_tokens(name)))

    def indexed_count(self) -> int:
        """Number of matchable companies (batch-script startup log)."""
        return len(self._entries)

    def match(self, name: str | None) -> SupplierMatch:
        exact_key = _exact_key(name)
        if not exact_key:
            return SupplierMatch(None, "empty", 0, None)

        # Tier 1 — exact (case-insensitive, whitespace-collapsed).
        hits = [c for c, key, _, _ in self._entries if key == exact_key]
        outcome = self._decide(hits, "exact")
        if outcome is not None:
            return outcome

        # Tier 2 — substring containment either direction on the
        # alnum-collapsed forms.
        needle = _alnum(name)
        if len(needle) >= self.MIN_SUBSTRING_LEN:
            hits = [c for c, _, alnum, _ in self._entries
                    if len(alnum) >= self.MIN_SUBSTRING_LEN
                    and (needle in alnum or alnum in needle)]
            outcome = self._decide(hits, "substring")
            if outcome is not None:
                return outcome

        # Tier 3 — normalized token-set equality. Empty core (name was
        # all legal-form filler) matches nothing.
        core = _core_tokens(name)
        if core:
            hits = [c for c, _, _, tokens in self._entries
                    if tokens and tokens == core]
            outcome = self._decide(hits, "normalized")
            if outcome is not None:
                return outcome

        return SupplierMatch(None, "no_match", 0, None)

    @staticmethod
    def _decide(hits: list[dict], tier: str) -> SupplierMatch | None:
        """Unique hit → matched; several → ambiguous (stop); none → None
        so the caller falls through to the next tier."""
        if len(hits) == 1:
            return SupplierMatch(hits[0], "matched", 1, tier, hits)
        if len(hits) > 1:
            return SupplierMatch(None, "ambiguous", len(hits), tier, hits)
        return None
