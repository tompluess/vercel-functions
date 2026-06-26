"""MocoCategoryResolver — pick a Moco purchase-category for an OCR'd bill.

Decides which `category_id` (Buchhaltungs-Konto / expense account) goes
on each line item of a newly created purchase. The chain is:

1. **Already-paid bills** (`invoice.already_paid_by_card`) → return None.
   The reviewer must pick the account by hand per receipt; setting any
   default would lull them into approving the wrong booking.
2. **Project-specified account**: when the resolver matched a Moco
   project AND that project carries an `Aufwandkonto` custom-property,
   look up the category whose `credit_account` equals that value.
   - On match: return its id.
   - On miss (project says `"4500"` but no such category): return None.
     We do NOT silently fall back to `"4000"` here — if the project
     explicitly overrode the default, picking a different account
     would mis-route the booking. Operator either fixes the project's
     custom-property or picks a category by hand during review.
3. **Account-wide fallback**: otherwise (no project, or project without
   `Aufwandkonto`), look up the category whose `credit_account` is the
   hardcoded default `"4000"` (Wareneinkauf, Swiss SKR convention).
4. **No category at all**: if even the fallback isn't in the catalog,
   return None. Moco creates the purchase with its own default; better
   than guessing a numeric id.

See `specs/SPEC_kommission_project_resolution.md` (Stage 3) for the
spec and reasoning behind each branch.

Kept as a separate collaborator (one-class-per-file) so the resolver
can be unit-tested in isolation and reused unchanged by both the
production webhook handler and the batch validation script.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CategoryDecision:
    """Outcome of `MocoCategoryResolver.resolve(...)`.

    `category_id` is None whenever the caller should OMIT the field from
    the create-purchase payload (already-paid bill, project override
    missed, or fallback missing). `reason` is an operator-facing string
    used in logs/Telegram so the rationale is visible without having to
    re-derive it.
    """
    category_id: int | None
    reason: str


class MocoCategoryResolver:
    DEFAULT_CREDIT_ACCOUNT = "4000"  # Wareneinkauf — Swiss SKR convention
    PROJECT_CUSTOM_FIELD = "Aufwandkonto"

    def __init__(self, categories: list[dict]):
        # credit_account (normalized) -> category dict. First wins on
        # collision — Moco shouldn't have two categories with the same
        # credit_account, but we don't want the resolver to crash if it
        # ever does.
        self._by_credit_account: dict[str, dict] = {}
        for c in categories:
            credit = c.get("credit_account")
            if credit is None:
                continue
            norm = str(credit).strip()
            if not norm:
                continue
            self._by_credit_account.setdefault(norm, c)

    def indexed_count(self) -> int:
        """Number of categories the resolver can map to (operator-facing
        diagnostic for the batch script's startup log)."""
        return len(self._by_credit_account)

    def resolve(self, *, already_paid_by_card: bool,
                project: dict | None) -> CategoryDecision:
        if already_paid_by_card:
            return CategoryDecision(None, "skipped: already paid")
        if project is not None:
            aufwand = self._extract_aufwandkonto(project)
            if aufwand is not None:
                cat = self._by_credit_account.get(aufwand)
                if cat is not None:
                    return CategoryDecision(
                        cat.get("id"),
                        f"project override: credit_account={aufwand}")
                return CategoryDecision(
                    None,
                    f"project override credit_account={aufwand!r} not "
                    "found in catalog — omit")
        cat = self._by_credit_account.get(self.DEFAULT_CREDIT_ACCOUNT)
        if cat is not None:
            return CategoryDecision(
                cat.get("id"),
                f"default: credit_account={self.DEFAULT_CREDIT_ACCOUNT}")
        return CategoryDecision(
            None,
            f"default credit_account={self.DEFAULT_CREDIT_ACCOUNT!r} not "
            "in catalog — omit")

    def _extract_aufwandkonto(self, project: dict) -> str | None:
        props = project.get("custom_properties")
        if not isinstance(props, dict):
            return None
        val = props.get(self.PROJECT_CUSTOM_FIELD)
        if val is None or val == "":
            return None
        norm = str(val).strip()
        return norm or None
