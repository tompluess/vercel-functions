"""MocoCategoryResolver — pick a Moco purchase-category for an OCR'd bill.

Decides which `category_id` (Buchhaltungs-Konto / expense account) goes
on each line item of a newly created purchase. The chain is:

1. **Project-specified account**: when the resolver matched a Moco
   project AND that project carries an `Aufwandkonto` custom-property,
   look up the category whose `credit_account` equals that value.
   - On match: return its id.
   - On miss (project says `"4500"` but no such category): return None.
     We do NOT silently fall back here — if the project explicitly
     overrode the default, picking a different account would mis-route
     the booking. Operator either fixes the project's custom-property
     or picks a category by hand during review.
2. **Supplier-specified account**: same lookup against the matched
   supplier company's `Aufwandkonto` custom-property (e.g. a telecom
   supplier whose bills always book to 6510). Same no-fallthrough-on-
   miss semantics as the project override, for the same reason.
3. **Already-paid bills** (`invoice.already_paid_by_card`) → return
   None. Without an explicit override above, the reviewer must pick
   the account by hand per card receipt; setting any default would
   lull them into approving the wrong booking.
4. **Account-wide fallback**: otherwise, look up the category whose
   `credit_account` is the hardcoded default `"4000"` (Wareneinkauf,
   Swiss SKR convention).
5. **No category at all**: if even the fallback isn't in the catalog,
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
    the create-purchase payload (already-paid bill without an override,
    override missed, or fallback missing). `reason` is an operator-facing string
    used in logs/Telegram so the rationale is visible without having to
    re-derive it.

    `source` / `credit_account` are the machine-readable counterparts of
    `reason`, for the batch script's compact table cell: `source` is one
    of "project" / "supplier" / "default" / "already_paid", and
    `credit_account` is the account number the winning tier looked up —
    still set on a miss (override or missing-4000 edge) so the cell can
    show WHICH account failed to map. `already_paid` carries no
    credit_account.
    """
    category_id: int | None
    reason: str
    source: str | None = None
    credit_account: str | None = None


class MocoCategoryResolver:
    DEFAULT_CREDIT_ACCOUNT = "4000"  # Wareneinkauf — Swiss SKR convention
    CUSTOM_FIELD = "Aufwandkonto"  # on both projects and supplier companies

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
                project: dict | None,
                supplier: dict | None = None) -> CategoryDecision:
        override = (self._override_decision(project, "project")
                    or self._override_decision(supplier, "supplier"))
        if override is not None:
            return override
        if already_paid_by_card:
            return CategoryDecision(None, "skipped: already paid",
                                    source="already_paid")
        cat = self._by_credit_account.get(self.DEFAULT_CREDIT_ACCOUNT)
        if cat is not None:
            return CategoryDecision(
                cat.get("id"),
                f"default: credit_account={self.DEFAULT_CREDIT_ACCOUNT}",
                source="default",
                credit_account=self.DEFAULT_CREDIT_ACCOUNT)
        return CategoryDecision(
            None,
            f"default credit_account={self.DEFAULT_CREDIT_ACCOUNT!r} not "
            "in catalog — omit",
            source="default",
            credit_account=self.DEFAULT_CREDIT_ACCOUNT)

    def _override_decision(self, entity: dict | None,
                           label: str) -> CategoryDecision | None:
        """Explicit `Aufwandkonto` override from a project or supplier.

        Returns None when `entity` carries no override (caller falls
        through to the next tier). A set-but-unmapped override returns a
        CategoryDecision(None, …) — a final answer, NOT a fallthrough.
        """
        if entity is None:
            return None
        aufwand = self._extract_aufwandkonto(entity)
        if aufwand is None:
            return None
        cat = self._by_credit_account.get(aufwand)
        if cat is not None:
            return CategoryDecision(
                cat.get("id"),
                f"{label} override: credit_account={aufwand}",
                source=label, credit_account=aufwand)
        return CategoryDecision(
            None,
            f"{label} override credit_account={aufwand!r} not "
            "found in catalog — omit",
            source=label, credit_account=aufwand)

    def _extract_aufwandkonto(self, entity: dict) -> str | None:
        props = entity.get("custom_properties")
        if not isinstance(props, dict):
            return None
        val = props.get(self.CUSTOM_FIELD)
        if val is None or val == "":
            return None
        norm = str(val).strip()
        return norm or None
