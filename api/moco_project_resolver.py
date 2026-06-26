"""MocoProjectResolver — match an OCR'd Kommission to a Moco project.

Supplier invoices often carry a project identifier ("Kommission" / "Objekt" /
"Auftragsnummer" / "Bauvorhaben") that the operator uses to assign the
resulting purchase to the right Moco project. This resolver builds an
in-memory index of Moco projects keyed by their custom-field value
(default: `"Kommission"`) and looks up an OCR'd string against it.

Match rules (see `reference/SPEC_kommission_project_resolution.md`):

1. **Exact normalized match** wins first.
2. Falls back to **substring** matching either direction (OCR ⊂ key or
   key ⊂ OCR, alnum-collapsed) — catches cases where one full string
   sits inside the other (e.g. `#Haldenweg12_Jegensdorf` ⊂
   `PVAHaldenweg12_Jegensdorf`).
3. Then **token-overlap**: both sides tokenized on non-alphanumeric
   boundaries, tokens of length ≥ `MIN_TOKEN_LEN` kept; any shared
   token counts as a match. Catches noisy cases where neither full
   string sits inside the other but they share a distinctive fragment
   like an address (`Stroppelstrasse19`). Short tokens (`abc`, `123`,
   `ag`) are filtered out to avoid pathological false positives. Only
   fires when the substring tier found nothing — substring's stricter
   contiguity signal beats incidental token overlap.
4. A single resolved project at any tier → `matched` (with `tier`
   reporting which one — `"exact"` / `"substring"` / `"token-overlap"`).
   Multiple distinct projects at the same tier → `ambiguous` (no project
   selected, candidate count reported so the batch script can render
   `✗ ambiguous (N)`).

Normalization strips **all non-alphanumeric characters** (whitespace,
punctuation, `#`, `_`, `-`, `/`) and case-folds. Umlauts (ü, ö, …) are
preserved. This is more aggressive than a plain trim/casefold because
Moco-side Kommissions and supplier-bill renderings routinely differ on
exactly those filler characters — e.g. project `#Haldenweg12_Jegensdorf`
vs OCR'd `PVA Haldenweg 12_Jegensdorf`. With aggressive normalization
both collapse to `haldenweg12jegensdorf` / `pvahaldenweg12jegensdorf`
and the substring tier hits.

Kept as a separate collaborator (one-class-per-file) so the production
`SupplierInvoiceOcrService` can reuse it unchanged in Stage 2 when the
resolved project drives `project_id` + `category_id` on the created
purchase.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectMatch:
    """Outcome of `MocoProjectResolver.resolve(raw)`.

    `status` discriminates the four outcomes the caller cares about:
      - `"matched"`: a single project resolved; `project` is set, `tier`
        is `"exact"`, `"substring"`, or `"token-overlap"`.
      - `"ambiguous"`: multiple distinct projects matched at the same
        tier; `project` is None, `candidate_count` reports how many.
      - `"no_match"`: the index has no project matching the OCR'd value.
      - `"empty"`: the OCR'd value was None / blank — there was nothing
        to resolve. Lets callers render `"-"` vs `"no match"` distinctly.
    """
    project: dict | None
    status: str
    candidate_count: int
    tier: str | None


# Strip everything that's not a Unicode word character (\w = letters,
# digits, underscore + Unicode equivalents like ü, ö, ß), then strip
# underscores too. Result: only letters + digits remain. Casefold runs
# after so the German ß → ss normalization applies before we test for
# substring containment.
_STRIP_RE = re.compile(r"[\W_]+", flags=re.UNICODE)


def _normalize(value: str | None) -> str:
    """Strip all non-alphanumerics + casefold. Empty string on None."""
    if not value:
        return ""
    return _STRIP_RE.sub("", value).casefold()


def _tokens(value: str | None, *, min_len: int) -> set[str]:
    """Split on non-alphanumerics, casefold, drop tokens below `min_len`.

    Returns a set so token-overlap is a plain set intersection. Empty
    input → empty set. Min-length filtering is what keeps generic short
    fragments (`ag`, `abc`, `123`) from creating false positives when
    projects happen to share a tiny token.
    """
    if not value:
        return set()
    return {t.casefold() for t in _STRIP_RE.split(value) if len(t) >= min_len}


class MocoProjectResolver:
    DEFAULT_CUSTOM_FIELD = "Kommission"
    # Minimum token length (in characters) for the token-overlap tier.
    # Six is a heuristic — long enough to skip generic noise like `ag`,
    # `bv`, `abc`, `123`, short enough to keep meaningful German
    # surnames and Moco-style project codes (`p25031`).
    MIN_TOKEN_LEN = 6

    def __init__(self, projects: list[dict], *,
                 custom_field_label: str = DEFAULT_CUSTOM_FIELD):
        self._custom_field_label = custom_field_label
        # normalized_key -> list[project]. A single key can map to multiple
        # projects when two Moco projects accidentally share the same
        # Kommission value — the resolver surfaces that as ambiguity rather
        # than silently picking the first.
        self._index: dict[str, list[dict]] = {}
        # token -> list[project] for the token-overlap tier. Built from
        # the same source string the full-normalized index uses
        # (Kommission custom-property or name fallback).
        self._token_index: dict[str, list[dict]] = {}
        for p in projects:
            source = self._extract_source(p)
            if source is None:
                continue
            norm = _normalize(source)
            if not norm:
                continue
            self._index.setdefault(norm, []).append(p)
            for tok in _tokens(source, min_len=self.MIN_TOKEN_LEN):
                self._token_index.setdefault(tok, []).append(p)

    def _extract_source(self, project: dict) -> str | None:
        """Return the *raw* source string under which the project is
        indexed (Kommission custom-property if set, else `project.name`).

        Preference order: the `Kommission` custom-property value if set,
        else fall back to `project.name`. Projects with neither are not
        indexed (returns None). Returns the raw string (not normalized)
        so the caller can both normalize it for the full-string index and
        tokenize it for the token-overlap index.
        """
        props = project.get("custom_properties")
        if isinstance(props, dict):
            raw = props.get(self._custom_field_label)
            # Moco may return non-string types (e.g. integers) on numeric
            # custom fields. Coerce to str for downstream processing.
            if raw is not None and raw != "":
                return str(raw)
        name = project.get("name")
        if name is None or name == "":
            return None
        return str(name)

    def indexed_count(self) -> int:
        """Number of distinct index keys (operator-facing diagnostic for
        the batch script's startup log line). Counts both Kommission-keyed
        and name-fallback-keyed projects."""
        return len(self._index)

    def resolve(self, raw: str | None) -> ProjectMatch:
        norm = _normalize(raw)
        if not norm:
            return ProjectMatch(None, "empty", 0, None)

        exact = self._index.get(norm)
        if exact:
            projects = self._dedupe_by_id(exact)
            if len(projects) == 1:
                return ProjectMatch(projects[0], "matched", 1, "exact")
            return ProjectMatch(None, "ambiguous", len(projects), "exact")

        substring_candidates: list[dict] = []
        seen_ids: set = set()
        for key, key_projects in self._index.items():
            if key in norm or norm in key:
                for p in key_projects:
                    pid = p.get("id")
                    if pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    substring_candidates.append(p)
        if len(substring_candidates) == 1:
            return ProjectMatch(substring_candidates[0], "matched", 1,
                                "substring")
        if len(substring_candidates) > 1:
            return ProjectMatch(None, "ambiguous",
                                len(substring_candidates), "substring")

        # Token-overlap is the last-resort tier. We extract tokens from
        # the *raw* OCR string so word boundaries are preserved — that's
        # what makes "Stroppelstrasse19" pop out of
        # "...AB 2025-2013338, Stroppelstrasse19_Untersiggenthal".
        token_candidates: list[dict] = []
        token_seen: set = set()
        for tok in _tokens(raw, min_len=self.MIN_TOKEN_LEN):
            for p in self._token_index.get(tok, []):
                pid = p.get("id")
                if pid in token_seen:
                    continue
                token_seen.add(pid)
                token_candidates.append(p)
        if not token_candidates:
            return ProjectMatch(None, "no_match", 0, None)
        if len(token_candidates) == 1:
            return ProjectMatch(token_candidates[0], "matched", 1,
                                "token-overlap")
        return ProjectMatch(None, "ambiguous", len(token_candidates),
                            "token-overlap")

    @staticmethod
    def _dedupe_by_id(projects: list[dict]) -> list[dict]:
        seen: set = set()
        out: list[dict] = []
        for p in projects:
            pid = p.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            out.append(p)
        return out
