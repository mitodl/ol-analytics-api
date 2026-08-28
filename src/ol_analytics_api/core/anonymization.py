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
beneath it, so anything the floor withholds at the finer grain is recoverable
as ``coarse_total - sum(what the finer rows do publish)``. Note "withholds",
not "drops": a finer row can clear its own row gate and still publish NULL for
one additive column, because the cohort that column is attributable to is
sub-floor. Both cases leave the same hole in the sum.

The coarse grain's distinct-entity cohort counts are not exactly additive the
same way — a learner active under two contracts is one coarse learner but two
finer rows — so subtracting a hidden contract's siblings from the coarse total
is usually only a bound. It stops being only a bound when the finer rows
happen to share no learners: two disjoint contracts sum exactly, and a hidden
one comes back the same way a hidden event sum would. Nothing available here
can tell overlapping contracts from disjoint ones, so cohort columns are
guarded as if every finer grain were disjoint.

Rows alone cannot see any of that. ``hidden_additive_columns`` runs the finer
grain through the suppression above — the same function, not a cheaper
approximation of it — and reports which additive columns come back NULL per
key; ``suppress_cross_grain_additives`` blanks exactly those at the coarse
grain, plus every cohort column for any key with something hidden.
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
        including transitive ones."""
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
        Blanked one at a time: only the specific column the finer grain hides
        for a key is recoverable by subtraction, so only that one is blanked.
    ``guarded_cohorts``
        The coarse grain's distinct-entity cohort columns (its ``primary`` and
        ``secondary`` policy fields) — blanked wholesale, all of them, for any
        key the finer grain hides *anything* for.

        These are not exactly additive across the finer rows in general — a
        learner active under two contracts is counted once at the coarse grain
        but appears in two finer rows — so ``coarse - sum(visible finer)`` is
        usually a bound on a hidden cohort, not its value. But nothing here can
        tell overlapping contracts from disjoint ones, and when the finer rows
        happen to partition the coarse cohort with no overlap, that bound is
        exact: a hidden contract-month's learner counts come back the same way
        a hidden event sum would. So every cohort column is blanked whenever
        anything is hidden for the key, at the cost of also blanking counts
        that overlap would have made safe to publish.
    """

    key_column: str
    columns: tuple[str, ...]
    guarded_cohorts: tuple[str, ...] = ()


def hidden_additive_columns(
    finer_rows: list[dict[str, Any]],
    policy: CohortPolicy,
    floor: int,
    *,
    key_column: str,
    additive_columns: tuple[str, ...],
) -> dict[Any, frozenset[str]]:
    """Per key, which additive columns the finer grain does not publish in full.

    This runs the finer rows through ``suppress_small_cohorts`` — the same
    function that suppresses them on their own endpoint — rather than asking
    the database a cheaper question about them. Two weaker tests were tried
    and are wrong:

    - "is any finer row dropped?" misses the larger case by far. Every additive
      column is a ``derived`` column at the finer grain, so a finer row can
      clear the row gate and still publish NULL for one of them because the
      cohort *that* column is attributable to is sub-floor. A contract month
      with 30 active learners of whom 2 used the chatbot publishes its row and
      withholds its chatbot total, and the coarse total minus its siblings
      hands that withheld number straight back.
    - re-deriving the rule in SQL keeps two copies of a governance decision in
      step by hand. The complement rule alone is a fixpoint over transitive
      containment pairs; a second implementation of it would drift, and the
      direction it drifts in is silently publishing.

    A column counts as hidden when it is NULL after suppression, whether the
    floor nulled it or the view never had a value. The caller cannot tell those
    apart either, so both leave a hole in the sum that the coarse row would
    fill in.
    """
    hidden: dict[Any, set[str]] = {}
    for row in finer_rows:
        # One row at a time: suppress_small_cohorts returns only survivors, with
        # no back-pointer to the row each came from, and several finer rows share
        # a key (that is what makes the coarse row a sum). It is per-row anyway,
        # so splitting the call changes nothing but keeps the correspondence.
        kept = suppress_small_cohorts([row], policy, floor)
        columns = (
            set(additive_columns)  # dropped whole: every additive column is gone
            if not kept
            else {column for column in additive_columns if kept[0].get(column) is None}
        )
        if columns:
            hidden.setdefault(row.get(key_column), set()).update(columns)
    return {key: frozenset(columns) for key, columns in hidden.items()}


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


def _suppressed_cohorts(
    row: Mapping[str, Any],
    policy: CohortPolicy,
    floor: int,
    pairs: tuple[tuple[str, str], ...],
) -> set[str]:
    """The secondary cohorts of one row that must not be published."""
    suppressed = {column for column in policy.secondary if _is_disclosive(row.get(column), floor)}
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
    # Walked once for the whole response, not once per row: a policy is fixed
    # ClassVar state on the row model, so the pairs it yields are the same for
    # every row in it.
    pairs = policy.complement_pairs()
    for row in rows:
        # `.get(field) or 0` folds both a missing key and a NULL primary to 0,
        # so a too-small *or* unknown headline cohort withholds the whole row.
        if (row.get(policy.primary) or 0) < floor:
            continue
        redacted = dict(row)
        suppressed = _suppressed_cohorts(redacted, policy, floor, pairs)
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
    hidden_by_key: Mapping[Any, Collection[str]],
) -> list[dict[str, Any]]:
    """Blank the coarse columns that would reconstruct what the finer grain hides.

    ``hidden_by_key`` maps a ``key_column`` value to the additive columns the
    finer grain does not publish in full for it — what
    ``hidden_additive_columns`` returns. For those the caller holds a coarse
    total and every finer contribution but one (or a few), so the difference is
    the hidden quantity, attributable to a cohort the floor already judged too
    small to publish. Withholding the coarse total is what breaks the
    subtraction; the finer rows themselves stay as they are.

    ``additives.columns`` is blanked per column, not per key: a month where
    only the chatbot total is withheld downstream keeps its video and problem
    totals, which nothing can be subtracted out of. One hidden finer
    contribution is enough to blank the column it belongs to — several are not
    safer, since the difference is then their sum, which can still be a
    handful of entities.

    ``additives.guarded_cohorts`` is blanked per key instead: any hidden entry
    for a key blanks every cohort column for that key, regardless of which
    specific additive column triggered it. These columns aren't attributable
    to one hidden contribution the way an additive column is, so there is no
    narrower blanking that stays safe under the disjoint-contract case
    ``CrossGrainAdditives`` documents.

    Input rows are not mutated.
    """
    if not hidden_by_key:
        return rows
    guarded = set(additives.guarded_cohorts)
    return [
        {
            column: (None if column in blanked or column in guarded else value)
            for column, value in row.items()
        }
        if (blanked := hidden_by_key.get(row.get(additives.key_column)))
        else row
        for row in rows
    ]
