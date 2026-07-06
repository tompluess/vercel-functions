"""SmartmeProjectMatcher — map a smart-me "Objekt" to one energy project.

smart-me Energiekostenabrechnungen carry an `Objekt` line like
`"Gesamtverbrauch (Hauptstrasse 33 Leimbach)"` or
`"EFH Dogelzwil 2 (vZEV Krugel1_Oberkirch)"`; the matching Moco project
is named after the site address, e.g. `"Hauptstrasse 33, Leimbach,
Solarstrom Eigenverbrauch"`. Only projects labeled `ZEV` or
`Eigenverbrauch` participate — those are the energy-billing projects the
resulting expense may land on.

Two tiers:

1. **Kommission custom-field equality.** The operator can pin the
   mapping by putting the smart-me object name into the project's
   `Kommission` custom property (the same field the purchase flow's
   `MocoProjectResolver` uses). A project matches when its normalized
   Kommission **equals** one of the Objekt's segments — the full string,
   the part before the parentheses ("EFH Dogelzwil 2"), or a
   parenthesized group's content ("Hauptstrasse 33 Leimbach"). Equality,
   not substring: the parenthesized part names the ZEV *community*
   (e.g. "vZEV Krugel1_Oberkirch"), and the community project's own
   Kommission (`#Krugel1_Oberkirch`) is a substring of it — containment
   would route every consumer bill in that ZEV to the community project.
   A unique tier-1 hit wins outright; multiple hits → ambiguous.

2. **Best-token-overlap score** over the project *names*, for projects
   the operator hasn't pinned. Not `MocoProjectResolver`'s
   any-shared-token tier: several ZEV projects share their village token
   ("… Oberkirch"), so "any shared token" would report every one of them
   as ambiguous. Scoring by the *number* of shared tokens lets the
   street / house-number tokens break the tie:

       Objekt  "Gesamtverbrauch (Hauptstrasse 33 Leimbach)"
               → {gesamtverbrauch, hauptstrasse, 33, leimbach}
       Project "Hauptstrasse 33, Leimbach, Solarstrom Eigenverbrauch"
               → score 3 — unique max → matched.

   `MIN_TOKEN_LEN = 1` (unlike the resolver's 6) because single-digit
   house numbers ("1", "2", "33") are exactly what distinguishes
   neighbouring installations. Generic tokens can still tie two
   projects (seen live: "EFH Dogelzwil 2 (vZEV Krugel1_Oberkirch)"
   scored 3 on both "Dogelzwil 2" and "EFH Krugel 2") — that's the
   case tier 1 exists for; the Telegram alert tells the operator which
   Kommission field to fill.

A unique winner → `matched` (with `tier` reporting `"kommission"` or
`"tokens"`). Tie at the top → `ambiguous`. No overlap at all →
`no_match`. Blank Objekt → `empty`. The caller alerts + keeps the draft
for everything but `matched` — we prefer a manual booking over a
mis-routed expense.
"""

import re
from dataclasses import dataclass

from api.moco_project_resolver import _normalize, _tokens

# Energy-billing project labels (casefolded). A project carrying either
# tag participates in matching; `project_energy_label` also drives the
# expense title (ZEV wins when both are present — ZEV billing includes
# Netzstrom, so the broader title is the safe one).
LABEL_ZEV = "zev"
LABEL_EIGENVERBRAUCH = "eigenverbrauch"


def project_energy_label(project: dict) -> str | None:
    """Return `"ZEV"` / `"Eigenverbrauch"` from the project's tags, or None.

    Case-insensitive; ZEV wins when both labels are present.
    """
    tags = project.get("tags")
    if not isinstance(tags, list):
        return None
    lowered = {str(t).casefold() for t in tags}
    if LABEL_ZEV in lowered:
        return "ZEV"
    if LABEL_EIGENVERBRAUCH in lowered:
        return "Eigenverbrauch"
    return None


@dataclass(frozen=True)
class SmartmeProjectMatch:
    """Outcome of `SmartmeProjectMatcher.match(objekt)`.

    `status` is one of `"matched"` / `"ambiguous"` / `"no_match"` /
    `"empty"`. `tier` reports which tier decided (`"kommission"` /
    `"tokens"`, None for no_match/empty). `score` is the winning
    shared-token count on the token tier (0 on the Kommission tier) —
    diagnostics for the batch table.
    """
    project: dict | None
    status: str
    candidate_count: int
    score: int
    tier: str | None = None


# Parenthesized groups in a smart-me Objekt — "(vZEV Krugel1_Oberkirch)"
# names the ZEV community, "(Hauptstrasse 33 Leimbach)" the site address.
_PAREN_GROUP_RE = re.compile(r"\(([^)]*)\)")

KOMMISSION_FIELD = "Kommission"


def _objekt_segments(objekt: str) -> set[str]:
    """Normalized candidate segments a Kommission may equal.

    The full string, the part before the first parenthesis (the smart-me
    object name proper), and each parenthesized group's content. Empty
    segments are dropped.
    """
    segments = {objekt, objekt.split("(", 1)[0]}
    segments.update(_PAREN_GROUP_RE.findall(objekt))
    return {norm for s in segments if (norm := _normalize(s))}


class SmartmeProjectMatcher:
    # Keep every token, including bare house numbers — see module docstring.
    MIN_TOKEN_LEN = 1

    def __init__(self, projects: list[dict]):
        # (project, name_tokens) for every ZEV/Eigenverbrauch project.
        # Projects without an energy label are invisible to the matcher.
        self._candidates: list[tuple[dict, set[str]]] = []
        # normalized Kommission -> list[project] for tier 1. Only labeled
        # projects participate — the purchase flow's resolver owns the
        # account-wide Kommission index.
        self._kommission_index: dict[str, list[dict]] = {}
        for p in projects:
            if project_energy_label(p) is None:
                continue
            tokens = _tokens(p.get("name"), min_len=self.MIN_TOKEN_LEN)
            if not tokens:
                continue
            self._candidates.append((p, tokens))
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
        """Number of labeled projects in the index (batch-script log line)."""
        return len(self._candidates)

    def match(self, objekt: str | None) -> SmartmeProjectMatch:
        objekt_tokens = _tokens(objekt, min_len=self.MIN_TOKEN_LEN)
        if not objekt_tokens:
            return SmartmeProjectMatch(None, "empty", 0, 0)

        # Tier 1 — operator-pinned Kommission equality. A unique hit wins
        # outright; multiple hits mean two projects claim the same object
        # name → surface as ambiguous rather than picking one.
        pinned: list[dict] = []
        seen_ids: set = set()
        for segment in _objekt_segments(objekt):
            for p in self._kommission_index.get(segment, []):
                pid = p.get("id")
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                pinned.append(p)
        if len(pinned) == 1:
            return SmartmeProjectMatch(pinned[0], "matched", 1, 0,
                                       "kommission")
        if len(pinned) > 1:
            return SmartmeProjectMatch(None, "ambiguous", len(pinned), 0,
                                       "kommission")

        # Tier 2 — best token overlap against project names.
        best_score = 0
        winners: list[dict] = []
        for project, name_tokens in self._candidates:
            score = len(objekt_tokens & name_tokens)
            if score == 0:
                continue
            if score > best_score:
                best_score = score
                winners = [project]
            elif score == best_score:
                winners.append(project)

        if not winners:
            return SmartmeProjectMatch(None, "no_match", 0, 0)
        if len(winners) == 1:
            return SmartmeProjectMatch(winners[0], "matched", 1, best_score,
                                       "tokens")
        return SmartmeProjectMatch(None, "ambiguous", len(winners),
                                   best_score, "tokens")
