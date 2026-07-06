"""SmartmeProjectMatcher — map a smart-me "Objekt" to one energy project.

smart-me Energiekostenabrechnungen carry an `Objekt` line like
`"Gesamtverbrauch (Hauptstrasse 33 Leimbach)"`; the matching Moco project
is named after the site address, e.g. `"Hauptstrasse 33, Leimbach,
Solarstrom Eigenverbrauch"`. Only projects labeled `ZEV` or
`Eigenverbrauch` participate — those are the energy-billing projects the
resulting expense may land on.

Matching is a **best-token-overlap score**, not `MocoProjectResolver`'s
any-shared-token tier: several ZEV projects share their village token
("… Oberkirch"), so "any shared token" would report every one of them as
ambiguous. Scoring by the *number* of shared tokens lets the street /
house-number tokens break the tie:

    Objekt  "Gesamtverbrauch (Hauptstrasse 33 Leimbach)"
            → {gesamtverbrauch, hauptstrasse, 33, leimbach}
    Project "Hauptstrasse 33, Leimbach, Solarstrom Eigenverbrauch"
            → score 3 — unique max → matched.

`MIN_TOKEN_LEN = 1` (unlike the resolver's 6) because single-digit house
numbers ("1", "2", "33") are exactly what distinguishes neighbouring
installations; incidental short-token overlap can't produce a false
positive here since a tie at the top yields `ambiguous`, never a pick.

A unique highest positive score → `matched`. Tie at the top →
`ambiguous`. No token overlap at all → `no_match`. Blank Objekt →
`empty`. The caller alerts + keeps the draft for everything but
`matched` — we prefer a manual booking over a mis-routed expense.
"""

from dataclasses import dataclass

from api.moco_project_resolver import _tokens

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
    `"empty"`. `score` is the winning shared-token count (0 unless at
    least one project overlapped) — diagnostics for the batch table.
    """
    project: dict | None
    status: str
    candidate_count: int
    score: int


class SmartmeProjectMatcher:
    # Keep every token, including bare house numbers — see module docstring.
    MIN_TOKEN_LEN = 1

    def __init__(self, projects: list[dict]):
        # (project, name_tokens) for every ZEV/Eigenverbrauch project.
        # Projects without an energy label are invisible to the matcher.
        self._candidates: list[tuple[dict, set[str]]] = []
        for p in projects:
            if project_energy_label(p) is None:
                continue
            tokens = _tokens(p.get("name"), min_len=self.MIN_TOKEN_LEN)
            if not tokens:
                continue
            self._candidates.append((p, tokens))

    def indexed_count(self) -> int:
        """Number of labeled projects in the index (batch-script log line)."""
        return len(self._candidates)

    def match(self, objekt: str | None) -> SmartmeProjectMatch:
        objekt_tokens = _tokens(objekt, min_len=self.MIN_TOKEN_LEN)
        if not objekt_tokens:
            return SmartmeProjectMatch(None, "empty", 0, 0)

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
            return SmartmeProjectMatch(winners[0], "matched", 1, best_score)
        return SmartmeProjectMatch(None, "ambiguous", len(winners),
                                   best_score)
