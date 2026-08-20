"""Enforce a k-anonymity floor across every disclosive column of an aggregate row.

A generic k-anonymity-style floor applied at the response layer (not in the
source views, so those retain full data for internal/admin use and the
suppression logic lives in one reviewable place). The floor value itself is a
governance decision each tenant sets in its own config (e.g. the b2b_dashboard
tenant's 5-learner floor, see Verification & QA epic, spec review 2026-07-02),
not a cross-tenant default.

A materialized-view row is not a single cohort: alongside its headline cohort
size it carries many *other* distinct-entity counts (learners who engaged,
earned a certificate, used the chatbot, ...) and values derived from them
(rates, averages, activity sums). Enforcing the floor on only one field lets a
surviving row still disclose the small counts — a row with 50 enrolled but
``certificates_earned == 1`` names that one learner's outcome, and an average
computed over a single engaged learner *is* that learner's exact value. So the
floor is enforced per-column, driven by a `CohortPolicy` the row model declares:

- the ``primary`` cohort gates the whole row (below floor -> row withheld),
- each ``secondary`` count is independently nulled when it is sub-floor,
- each ``secondary`` count is *also* nulled when its COMPLEMENT within a cohort
  containing it is sub-floor, and
- each ``derived`` value is nulled whenever a cohort it is computed over is
  suppressed (else the hidden count is trivially back-computed from the rate,
  or read off directly as an average over k<floor entities).

The complement rule closes the mirror image of the floor. A published count
that nearly fills the cohort containing it identifies the few members who did
*not* do the thing just as precisely as a small count identifies the few who
did: 42 active learners of whom 40 used the chatbot names exactly 2 abstainers.
Suppressing only the small side would leave that half open.

A second disclosure channel runs *across* rows rather than within one. This
service publishes the same learners at two grains — organization and contract —
and a coarse row's exactly-additive columns are the sum of the finer rows
beneath it, so a finer row withheld by the floor is recoverable as
``coarse_total - sum(the visible finer rows)``. Rows alone cannot see that;
``suppress_cross_grain_additives`` takes the finer grain's withheld keys as
input and blanks the coarse columns that would reconstruct them.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class CohortPolicy:
    """Which columns of an aggregate row are subject to the k-anonymity floor.

    ``primary``
        The row's headline cohort size. A row whose primary count is below the
        floor (or NULL/missing) is dropped from the response entirely.
    ``secondary``
        Other distinct-entity counts in the same row. Each is independently
        nulled when it is a nonzero sub-floor count (fewer than ``floor``
        identifiable entities). A count of exactly 0 is kept — it discloses no
        individual, only that nobody did the thing.
    ``derived``
        Maps a computed column (a rate, an average, or an activity total that
        is attributable to a cohort) to the cohort count(s) it is derived from.
        The derived value is nulled whenever any of those cohorts is suppressed.
    ``contained_in``
        Maps a secondary count to the cohort it is a strict subset of — the
        primary, or another secondary. This is what powers the complement rule:
        a cohort's members who are *not* in the subset are ``container - subset``
        many, and when that is nonzero and below the floor the subset is nulled.
        Declare the tightest container that holds; ancestors are walked
        transitively, so ``video_watchers -> engaged_learners ->
        total_enrolled_learners`` checks the complement against both.
    ``uncontained``
        Secondary counts deliberately subject to no complement rule, each of
        which must be justified in the declaring model's docstring. Two things
        land here: counts that are not subsets of anything in the row (a
        monthly ``enrolling_learners``, where enrolling does not itself make a
        learner active), and columns that count *events* rather than entities
        (``certificates_earned`` as ``sum(certificate_count)``), where
        ``container - count`` can go negative and means nothing either way.

    Every ``secondary`` count must appear in exactly one of ``contained_in``
    and ``uncontained``. Leaving one out is a leak, not an oversight, so it is
    rejected at policy-definition time.
    """

    primary: str
    secondary: tuple[str, ...] = ()
    derived: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    contained_in: Mapping[str, str] = field(default_factory=dict)
    uncontained: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # A `derived` entry naming a cohort that's neither `primary` nor in
        # `secondary` would silently skip suppression for that derived value
        # — a typo or omission here is a k-anonymity leak, not a cosmetic
        # bug, so it's caught at policy-definition time rather than at
        # response time.
        allowed = {self.primary, *self.secondary}
        for column, cohorts in self.derived.items():
            for cohort in cohorts:
                if cohort not in allowed:
                    msg = (
                        f"Derived column {column!r} references unknown cohort {cohort!r}. "
                        f"It must be either the primary cohort {self.primary!r} or in "
                        "secondary cohorts."
                    )
                    raise ValueError(msg)
        self._validate_containment(allowed)
        # `frozen=True` stops attribute *reassignment*, not mutation of a
        # mutable object already stored in one — a plain dict handed in (or
        # reused across CohortPolicy instances by a caller) could still be
        # mutated in place afterwards. Copy into a read-only view so it
        # can't be.
        object.__setattr__(self, "derived", MappingProxyType(dict(self.derived)))
        object.__setattr__(self, "contained_in", MappingProxyType(dict(self.contained_in)))

    def _validate_containment(self, allowed: set[str]) -> None:
        """Reject a containment declaration that can't be reasoned about.

        Same rationale as the `derived` check above: every failure mode here
        ends in a complement going unchecked, so none of them may pass
        silently to request time.
        """
        for subset, container in self.contained_in.items():
            if subset not in self.secondary:
                msg = (
                    f"contained_in names {subset!r}, which is not a secondary cohort. "
                    "Only secondary counts can be suppressed by the complement rule."
                )
                raise ValueError(msg)
            if container not in allowed:
                msg = (
                    f"Cohort {subset!r} is declared inside unknown cohort {container!r}. "
                    f"It must be either the primary cohort {self.primary!r} or in "
                    "secondary cohorts."
                )
                raise ValueError(msg)
        for column in self.uncontained:
            if column not in self.secondary:
                msg = f"uncontained names {column!r}, which is not a secondary cohort."
                raise ValueError(msg)
        classified = set(self.contained_in) | set(self.uncontained)
        if overlap := set(self.contained_in) & set(self.uncontained):
            msg = (
                f"Cohorts {sorted(overlap)} are in both contained_in and uncontained. "
                "Each secondary count is one or the other."
            )
            raise ValueError(msg)
        if unclassified := set(self.secondary) - classified:
            msg = (
                f"Secondary cohorts {sorted(unclassified)} are classified neither by "
                "contained_in nor by uncontained. An undeclared containment silently "
                "skips the complement rule, so declare the cohort each is a subset of, "
                "or list it in uncontained with the reason in the model's docstring."
            )
            raise ValueError(msg)
        # A cycle would make the ancestor walk below run forever. It also can't
        # describe anything real: strict containment is a partial order.
        for subset in self.contained_in:
            list(self._ancestors(subset))

    def _ancestors(self, subset: str) -> Iterator[str]:
        """Walk ``subset`` outwards through every cohort that contains it."""
        seen = {subset}
        container = self.contained_in.get(subset)
        while container is not None:
            if container in seen:
                msg = f"Containment cycle through cohort {container!r}."
                raise ValueError(msg)
            seen.add(container)
            yield container
            container = self.contained_in.get(container)

    def complement_pairs(self) -> tuple[tuple[str, str], ...]:
        """Every ``(subset, container)`` pair whose complement must be checked,
        including transitive ones. Computed once per response rather than per
        row — a policy is fixed ClassVar state on the row model."""
        return tuple(
            (subset, container)
            for subset in self.contained_in
            for container in self._ancestors(subset)
        )


@dataclass(frozen=True)
class CrossGrainAdditives:
    """Coarse-grained columns that are exact sums over a finer grain's rows.

    ``key_column``
        The column shared by both grains that lines a coarse row up with the
        finer rows summing into it (e.g. the activity month).
    ``columns``
        The coarse columns that are *exactly* additive across the finer rows.
        Only these are recoverable by subtraction, so only these are blanked.
        Distinct-entity counts generally are not additive — a learner active
        under two contracts is counted in both finer rows — so subtracting
        those yields a bound, not a value, and they are left alone rather than
        over-suppressed.
    """

    key_column: str
    columns: tuple[str, ...]


def _is_disclosive(value: int | None, floor: int) -> bool:
    # NULL (unknown — e.g. a LEFT JOIN miss in the source MV) is treated as
    # disclosive: we cannot prove the cohort was large enough, so suppress it.
    # Exactly 0 is safe (it names no individual); 1..floor-1 identifies too few.
    if value is None:
        return True
    return 0 < value < floor


def _complement_is_disclosive(subset: int, container: int, floor: int) -> bool:
    """Does publishing ``subset`` alongside ``container`` name too few of the
    entities that are in the container but not the subset?

    Both values are known non-NULL by the time this runs: a NULL secondary is
    disclosive on its own and is already suppressed (which skips the pair), and
    a NULL primary drops the row. Containers are constrained to the primary or
    a secondary at policy-definition time, so there is no third case.

    A complement of exactly 0 is safe for the same reason a count of 0 is:
    "everyone did it" singles nobody out. A *negative* complement is not a
    small cohort but a false declaration — the subset is provably not inside
    the container for this row — and since the whole point of the declaration
    is to bound what can be back-computed, an unreasonable one fails closed.
    """
    complement = container - subset
    return complement != 0 and complement < floor


def _suppressed_cohorts(row: Mapping[str, Any], policy: CohortPolicy, floor: int) -> set[str]:
    """The secondary cohorts of one row that must not be published."""
    suppressed = {column for column in policy.secondary if _is_disclosive(row.get(column), floor)}
    pairs = policy.complement_pairs()
    # Suppressing a cohort can hide the container of another pair, which
    # removes that pair from play rather than adding one, so this settles in
    # at most one pass per level of nesting. Cycles are rejected at
    # policy-definition time, so the loop terminates.
    changed = True
    while changed:
        changed = False
        for subset, container in pairs:
            if subset in suppressed or container in suppressed:
                # A complement needs both sides visible to be computed; if the
                # container is already withheld, the subset discloses nothing
                # beyond itself.
                continue
            # Indexing, not `.get`: a column missing from the row reads as a
            # NULL cohort, which is suppressed above and skipped just before
            # this line, so reaching here without both keys is a bug worth a
            # KeyError rather than a silently unchecked complement.
            if _complement_is_disclosive(row[subset], row[container], floor):
                suppressed.add(subset)
                changed = True
    return suppressed


def suppress_small_cohorts(
    rows: list[dict[str, Any]], policy: CohortPolicy, floor: int
) -> list[dict[str, Any]]:
    """Apply ``policy`` to every row, returning new dicts with sub-floor cohort
    counts, counts whose complement is sub-floor, and their derived values
    nulled, and rows below the primary floor dropped. Input rows are not
    mutated."""
    kept: list[dict[str, Any]] = []
    for row in rows:
        # `.get(field) or 0` folds both a missing key and a NULL primary to 0,
        # so a too-small *or* unknown headline cohort withholds the whole row.
        if (row.get(policy.primary) or 0) < floor:
            continue
        redacted = dict(row)
        suppressed = _suppressed_cohorts(redacted, policy, floor)
        for column in suppressed:
            redacted[column] = None
        for column, cohorts in policy.derived.items():
            if any(cohort in suppressed for cohort in cohorts):
                redacted[column] = None
        kept.append(redacted)
    return kept


def suppress_cross_grain_additives(
    rows: list[dict[str, Any]],
    additives: CrossGrainAdditives,
    hidden_keys: Collection[Any],
) -> list[dict[str, Any]]:
    """Blank the coarse columns that would reconstruct a withheld finer row.

    ``hidden_keys`` is the set of ``additives.key_column`` values for which the
    finer grain withheld at least one row. For those keys the caller holds a
    coarse total and every finer row but one (or a few), so the difference is
    the withheld row's value — a quantity attributable to a cohort the floor
    already judged too small to publish. Withholding the coarse total is what
    breaks the subtraction; the finer rows themselves stay as they are.

    One withheld finer row is enough to trigger this. Several are not safer:
    the difference is then their sum, which can still be a handful of entities.

    Input rows are not mutated.
    """
    if not hidden_keys:
        return rows
    hidden = set(hidden_keys)
    return [
        {column: (None if column in additives.columns else value) for column, value in row.items()}
        if row.get(additives.key_column) in hidden
        else row
        for row in rows
    ]
