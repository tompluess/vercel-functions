"""MocoProjectResolver — match an OCR'd Kommission to a Moco project.

Supplier invoices often carry a project identifier ("Kommission" / "Objekt" /
"Auftragsnummer" / "Bauvorhaben") that the operator uses to assign the
resulting purchase to the right Moco project. This resolver builds an
in-memory index of Moco projects keyed by their custom-field value
(default: `"Kommission"`) and looks up an OCR'd string against it.

Match rules (see `reference/SPEC_kommission_project_resolution.md`):

1. **Exact normalized match** wins first.
2. Falls back to **substring** matching either direction (OCR ⊂ key or
   key ⊂ OCR), collecting distinct projects.
3. A single resolved project at either tier → `matched` (with `tier`
   reporting which one). Multiple distinct projects at the same tier →
   `ambiguous` (no project selected, candidate count reported so the
   batch script can render `✗ ambiguous (N)`).

Normalization strips **all non-alphanumeric characters** (whitespace,
punctuation, `#`, `_`, `-`, `/`) and case-folds. Umlauts (ü, ö, …) are
preserved. This is more aggressive than a plain trim/casefold because
Moco-side Kommissions and supplier-bill renderings routinely differ on
exactly those filler characters — e.g. project `#Haldenweg12_Jegensdorf`
vs OCR'd `PVA Haldenweg 12_Jegensdorf`. With aggressive normalization
both collapse to `haldenweg12jegensdorf` / `pvahaldenweg12jegensdorf`
and the substring fallback hits.

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
        is `"exact"` or `"substring"`.
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


class MocoProjectResolver:
    DEFAULT_CUSTOM_FIELD = "Kommission"

    def __init__(self, projects: list[dict], *,
                 custom_field_label: str = DEFAULT_CUSTOM_FIELD):
        self._custom_field_label = custom_field_label
        # normalized_key -> list[project]. A single key can map to multiple
        # projects when two Moco projects accidentally share the same
        # Kommission value — the resolver surfaces that as ambiguity rather
        # than silently picking the first.
        self._index: dict[str, list[dict]] = {}
        for p in projects:
            value = self._extract_index_key(p)
            if value is None:
                continue
            self._index.setdefault(value, []).append(p)

    def _extract_index_key(self, project: dict) -> str | None:
        """Return the normalized key under which the project is indexed.

        Preference order: the `Kommission` custom-property value if set,
        else fall back to `project.name`. Projects with neither are not
        indexed (returns None). The name fallback makes the resolver
        useful for projects where the operator hasn't filled in the
        Kommission field yet, at the cost of every-project-indexed-by-name
        being noisier on the substring tier.
        """
        props = project.get("custom_properties")
        if isinstance(props, dict):
            raw = props.get(self._custom_field_label)
            # Moco may return non-string types (e.g. integers) on numeric
            # custom fields. Coerce to str for normalization.
            if raw is not None and raw != "":
                return _normalize(str(raw))
        name = project.get("name")
        if name is None or name == "":
            return None
        return _normalize(str(name))

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

        candidates: list[dict] = []
        seen_ids: set = set()
        for key, key_projects in self._index.items():
            if key in norm or norm in key:
                for p in key_projects:
                    pid = p.get("id")
                    if pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    candidates.append(p)
        if not candidates:
            return ProjectMatch(None, "no_match", 0, None)
        if len(candidates) == 1:
            return ProjectMatch(candidates[0], "matched", 1, "substring")
        return ProjectMatch(None, "ambiguous", len(candidates), "substring")

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
