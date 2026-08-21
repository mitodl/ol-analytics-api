import pytest

from ol_analytics_api.core.anonymization import (
    CohortPolicy,
    CrossGrainAdditives,
    hidden_additive_columns,
    suppress_cross_grain_additives,
    suppress_small_cohorts,
)


def test_row_below_primary_floor_is_dropped():
    rows = [
        {"org": "a", "seats_consumed": 10},
        {"org": "b", "seats_consumed": 4},
        {"org": "c", "seats_consumed": 5},
    ]
    policy = CohortPolicy(primary="seats_consumed")
    result = suppress_small_cohorts(rows, policy, floor=5)
    assert {row["org"] for row in result} == {"a", "c"}


def test_missing_primary_field_drops_row():
    policy = CohortPolicy(primary="seats_consumed")
    assert suppress_small_cohorts([{"org": "a"}], policy, floor=5) == []


def test_null_primary_value_does_not_crash_and_drops_row():
    # A NULL column from a LEFT JOIN in the source MV comes back as `None`,
    # not a missing key — `None >= floor` would raise TypeError if unhandled.
    policy = CohortPolicy(primary="seats_consumed")
    assert suppress_small_cohorts([{"org": "a", "seats_consumed": None}], policy, floor=5) == []


def test_secondary_count_below_floor_is_nulled_but_row_kept():
    # 50 enrolled passes the primary floor, but certificates_earned=1 and
    # chatbot_users=2 name too few learners and must be nulled out.
    rows = [
        {
            "total_enrolled_learners": 50,
            "engaged_learners": 40,
            "chatbot_users": 2,
            "certificates_earned": 1,
        }
    ]
    policy = CohortPolicy(
        primary="total_enrolled_learners",
        secondary=("engaged_learners", "chatbot_users", "certificates_earned"),
        contained_in={
            "engaged_learners": "total_enrolled_learners",
            "chatbot_users": "engaged_learners",
        },
        uncontained=("certificates_earned",),
    )
    (row,) = suppress_small_cohorts(rows, policy, floor=5)
    assert row["engaged_learners"] == 40  # above floor, retained
    assert row["chatbot_users"] is None
    assert row["certificates_earned"] is None


def test_secondary_count_of_zero_is_kept():
    # 0 discloses no individual — it stays visible as a real "nobody did this".
    rows = [{"total_enrolled_learners": 50, "certificates_earned": 0}]
    policy = CohortPolicy(
        primary="total_enrolled_learners",
        secondary=("certificates_earned",),
        uncontained=("certificates_earned",),
    )
    (row,) = suppress_small_cohorts(rows, policy, floor=5)
    assert row["certificates_earned"] == 0


def test_null_secondary_count_is_treated_as_disclosive():
    rows = [{"total_enrolled_learners": 50, "certificates_earned": None}]
    policy = CohortPolicy(
        primary="total_enrolled_learners",
        secondary=("certificates_earned",),
        uncontained=("certificates_earned",),
    )
    (row,) = suppress_small_cohorts(rows, policy, floor=5)
    assert row["certificates_earned"] is None


def test_derived_values_nulled_when_their_cohort_is_suppressed():
    # An average over a single engaged learner is that learner's exact value;
    # a rate plus its denominator back-computes the hidden count. Both must go.
    rows = [
        {
            "total_enrolled_learners": 50,
            "engaged_learners": 1,
            "engagement_rate_pct": 2.0,
            "total_videos_watched": 7,
            "avg_videos_per_engaged_learner": 7.0,
        }
    ]
    policy = CohortPolicy(
        primary="total_enrolled_learners",
        secondary=("engaged_learners",),
        derived={
            "engagement_rate_pct": ("engaged_learners",),
            "total_videos_watched": ("engaged_learners",),
            "avg_videos_per_engaged_learner": ("engaged_learners",),
        },
        contained_in={"engaged_learners": "total_enrolled_learners"},
    )
    (row,) = suppress_small_cohorts(rows, policy, floor=5)
    assert row["engaged_learners"] is None
    assert row["engagement_rate_pct"] is None
    assert row["total_videos_watched"] is None
    assert row["avg_videos_per_engaged_learner"] is None


def test_derived_values_retained_when_cohort_above_floor():
    rows = [
        {
            "total_enrolled_learners": 50,
            "engaged_learners": 30,
            "avg_videos_per_engaged_learner": 4.0,
        }
    ]
    policy = CohortPolicy(
        primary="total_enrolled_learners",
        secondary=("engaged_learners",),
        derived={"avg_videos_per_engaged_learner": ("engaged_learners",)},
        contained_in={"engaged_learners": "total_enrolled_learners"},
    )
    (row,) = suppress_small_cohorts(rows, policy, floor=5)
    assert row["engaged_learners"] == 30
    assert row["avg_videos_per_engaged_learner"] == 4.0


def test_input_rows_are_not_mutated():
    rows = [{"total_enrolled_learners": 50, "certificates_earned": 1}]
    policy = CohortPolicy(
        primary="total_enrolled_learners",
        secondary=("certificates_earned",),
        uncontained=("certificates_earned",),
    )
    suppress_small_cohorts(rows, policy, floor=5)
    assert rows[0]["certificates_earned"] == 1


def test_derived_referencing_unknown_cohort_raises_at_construction():
    # A typo/omission here would silently skip suppression for that derived
    # value — a k-anonymity leak, not a cosmetic bug — so it must fail at
    # policy-definition time rather than at response time.
    with pytest.raises(ValueError, match="unknown cohort"):
        CohortPolicy(
            primary="total_enrolled_learners",
            secondary=("engaged_learners",),
            derived={"avg_videos_per_engaged_learner": ("typo_learners",)},
            contained_in={"engaged_learners": "total_enrolled_learners"},
        )


def test_derived_referencing_primary_cohort_is_allowed():
    CohortPolicy(
        primary="total_enrolled_learners",
        derived={"avg_videos_per_learner": ("total_enrolled_learners",)},
    )


def test_derived_mapping_is_not_mutable_after_construction():
    # frozen=True only stops reassigning `derived`, not mutating a dict
    # already stored in it — __post_init__ must defend against that too,
    # since CohortPolicy instances are shared ClassVar state across
    # requests.
    source = {"avg_videos_per_learner": ("total_enrolled_learners",)}
    policy = CohortPolicy(primary="total_enrolled_learners", derived=source)

    source["sneaky"] = ("total_enrolled_learners",)
    assert "sneaky" not in policy.derived

    with pytest.raises(TypeError):
        policy.derived["sneaky"] = ("total_enrolled_learners",)


def _depth_policy(**overrides):
    """The nesting the content-engagement view actually declares: watchers
    inside the engaged learners, engaged inside everyone enrolled."""
    kwargs = {
        "primary": "total_enrolled_learners",
        "secondary": ("engaged_learners", "video_watchers"),
        "derived": {
            "engagement_rate_pct": ("engaged_learners",),
            "total_videos_watched": ("video_watchers",),
        },
        "contained_in": {
            "engaged_learners": "total_enrolled_learners",
            "video_watchers": "engaged_learners",
        },
    }
    return CohortPolicy(**(kwargs | overrides))


def test_near_total_secondary_is_nulled_because_its_complement_is_disclosive():
    # 42 engaged of whom 40 watched a video names the 2 who did not just as
    # precisely as a count of 2 watchers would name those 2.
    rows = [
        {
            "total_enrolled_learners": 50,
            "engaged_learners": 42,
            "video_watchers": 40,
            "total_videos_watched": 900,
        }
    ]
    (row,) = suppress_small_cohorts(rows, _depth_policy(), floor=5)
    assert row["video_watchers"] is None
    assert row["total_videos_watched"] is None
    assert row["engaged_learners"] == 42  # its own complement is 8, above floor


def test_complement_of_zero_is_kept():
    # Everybody engaged singles out nobody — the empty complement is not a
    # cohort of size < floor, it is no cohort at all.
    rows = [{"total_enrolled_learners": 50, "engaged_learners": 50, "video_watchers": 10}]
    (row,) = suppress_small_cohorts(rows, _depth_policy(), floor=5)
    assert row["engaged_learners"] == 50


def test_complement_at_the_floor_is_kept():
    rows = [{"total_enrolled_learners": 50, "engaged_learners": 45, "video_watchers": 10}]
    (row,) = suppress_small_cohorts(rows, _depth_policy(), floor=5)
    assert row["engaged_learners"] == 45


def test_negative_complement_fails_closed():
    # A subset larger than the cohort it is declared inside means the
    # declaration is wrong for this row. The complement rule's whole job is to
    # bound what can be back-computed, so an assumption it cannot trust
    # suppresses rather than waves through.
    rows = [{"total_enrolled_learners": 50, "engaged_learners": 30, "video_watchers": 31}]
    (row,) = suppress_small_cohorts(rows, _depth_policy(), floor=5)
    assert row["video_watchers"] is None


def test_complement_is_checked_transitively_when_the_inner_cohort_is_suppressed():
    # engaged_learners goes first (complement 2 within the enrolled). That
    # hides the container of the (video_watchers, engaged_learners) pair, so
    # the inner complement is no longer computable by a caller — but
    # video_watchers is still visible next to total_enrolled_learners, whose
    # complement is 3, and the transitive pair catches it.
    rows = [{"total_enrolled_learners": 42, "engaged_learners": 40, "video_watchers": 39}]
    (row,) = suppress_small_cohorts(rows, _depth_policy(), floor=5)
    assert row["engaged_learners"] is None
    assert row["video_watchers"] is None


def test_uncontained_cohort_is_exempt_from_the_complement_rule():
    # Enrolling does not make a learner active, so enrolling_learners is not a
    # subset of the primary and the difference is not a complement. Subtracting
    # anyway would suppress a perfectly publishable count on noise.
    policy = CohortPolicy(
        primary="monthly_active_learners",
        secondary=("enrolling_learners",),
        uncontained=("enrolling_learners",),
    )
    rows = [{"monthly_active_learners": 42, "enrolling_learners": 40}]
    (row,) = suppress_small_cohorts(rows, policy, floor=5)
    assert row["enrolling_learners"] == 40


def test_unclassified_secondary_raises_at_construction():
    # The leak this whole rule exists to close is silent by nature, so a
    # cohort nobody thought about must not default to unprotected.
    with pytest.raises(ValueError, match="classified neither"):
        CohortPolicy(primary="total_enrolled_learners", secondary=("engaged_learners",))


def test_cohort_in_both_contained_in_and_uncontained_raises():
    with pytest.raises(ValueError, match="both contained_in and uncontained"):
        CohortPolicy(
            primary="total_enrolled_learners",
            secondary=("engaged_learners",),
            contained_in={"engaged_learners": "total_enrolled_learners"},
            uncontained=("engaged_learners",),
        )


def test_containment_naming_an_unknown_container_raises():
    with pytest.raises(ValueError, match="unknown cohort"):
        CohortPolicy(
            primary="total_enrolled_learners",
            secondary=("engaged_learners",),
            contained_in={"engaged_learners": "typo_learners"},
        )


def test_containment_of_a_non_secondary_column_raises():
    with pytest.raises(ValueError, match="not a secondary cohort"):
        CohortPolicy(
            primary="total_enrolled_learners",
            secondary=("engaged_learners",),
            contained_in={
                "engaged_learners": "total_enrolled_learners",
                "some_rate_pct": "total_enrolled_learners",
            },
        )


def test_uncontained_naming_a_non_secondary_column_raises():
    with pytest.raises(ValueError, match="not a secondary cohort"):
        CohortPolicy(
            primary="total_enrolled_learners",
            secondary=("engaged_learners",),
            contained_in={"engaged_learners": "total_enrolled_learners"},
            uncontained=("some_rate_pct",),
        )


def test_containment_cycle_raises():
    # Left to run, a cycle would spin the ancestor walk forever; it also can't
    # describe anything real, since strict containment is a partial order.
    with pytest.raises(ValueError, match="cycle"):
        CohortPolicy(
            primary="total_enrolled_learners",
            secondary=("a_learners", "b_learners"),
            contained_in={"a_learners": "b_learners", "b_learners": "a_learners"},
        )


def test_containment_mapping_is_not_mutable_after_construction():
    source = {"engaged_learners": "total_enrolled_learners"}
    policy = CohortPolicy(
        primary="total_enrolled_learners",
        secondary=("engaged_learners",),
        contained_in=source,
    )

    source["sneaky"] = "total_enrolled_learners"
    assert "sneaky" not in policy.contained_in

    with pytest.raises(TypeError):
        policy.contained_in["sneaky"] = "total_enrolled_learners"


_ADDITIVES = CrossGrainAdditives(
    key_column="activity_year_and_month",
    columns=("new_enrollments", "total_videos_watched"),
)

# A finer grain shaped like the contract engagement trend: an event sum floored
# through the distinct-learner cohort it is attributable to.
_FINER_POLICY = CohortPolicy(
    primary="monthly_active_learners",
    secondary=("video_watchers",),
    derived={"total_videos_watched": ("video_watchers",)},
    contained_in={"video_watchers": "monthly_active_learners"},
)


def _finer_row(contract, active, watchers, videos, month="2026-07"):
    return {
        "activity_year_and_month": month,
        "contract_id": contract,
        "monthly_active_learners": active,
        "video_watchers": watchers,
        "total_videos_watched": videos,
    }


def _hidden(rows):
    return hidden_additive_columns(
        rows,
        _FINER_POLICY,
        floor=5,
        key_column="activity_year_and_month",
        additive_columns=("total_videos_watched",),
    )


def test_a_finer_row_that_survives_but_nulls_its_total_still_hides_it():
    # The case a row-gate check misses entirely, and the reason this is not one.
    # C1 clears the row gate with 30 active learners, so nothing is dropped —
    # but only 2 of them watched a video, so its video total is withheld. The
    # coarse total minus C2's 500 hands that withheld number straight back.
    rows = [
        _finer_row("C1", active=30, watchers=2, videos=17),
        _finer_row("C2", active=25, watchers=20, videos=500),
    ]
    assert _hidden(rows) == {"2026-07": frozenset({"total_videos_watched"})}


def test_a_dropped_finer_row_hides_every_additive_column():
    # Below the row gate, so it contributes nothing the caller can see and
    # every column it fed into the coarse total is recoverable.
    rows = [
        _finer_row("C1", active=2, watchers=2, videos=17),
        _finer_row("C2", active=25, watchers=20, videos=500),
    ]
    assert _hidden(rows) == {"2026-07": frozenset({"total_videos_watched"})}


def test_a_finer_row_hidden_by_the_complement_rule_is_caught_too():
    # 30 active of whom 28 watched: the complement rule nulls video_watchers,
    # which nulls the total derived from it. Nothing here is sub-floor on its
    # own, so only running the real suppression finds this.
    rows = [
        _finer_row("C1", active=30, watchers=28, videos=900),
        _finer_row("C2", active=25, watchers=10, videos=500),
    ]
    assert _hidden(rows) == {"2026-07": frozenset({"total_videos_watched"})}


def test_fully_published_finer_rows_hide_nothing():
    rows = [
        _finer_row("C1", active=30, watchers=10, videos=900),
        _finer_row("C2", active=25, watchers=10, videos=500),
    ]
    assert _hidden(rows) == {}


def test_hidden_columns_are_tracked_per_key():
    rows = [
        _finer_row("C1", active=30, watchers=2, videos=17, month="2026-07"),
        _finer_row("C1", active=30, watchers=10, videos=900, month="2026-06"),
    ]
    assert _hidden(rows) == {"2026-07": frozenset({"total_videos_watched"})}


def test_cross_grain_additives_are_blanked_for_a_withheld_key():
    # The caller holds this org total and every contract row but one, so the
    # difference is the withheld contract's own activity.
    rows = [
        {
            "activity_year_and_month": "2026-07",
            "monthly_active_learners": 40,
            "new_enrollments": 12,
            "total_videos_watched": 500,
        }
    ]
    (row,) = suppress_cross_grain_additives(
        rows, _ADDITIVES, {"2026-07": frozenset({"new_enrollments", "total_videos_watched"})}
    )
    assert row["new_enrollments"] is None
    assert row["total_videos_watched"] is None
    # Learner counts are not additive across contracts (one learner active
    # under two is counted in both), so subtracting them bounds rather than
    # reveals, and they stay published.
    assert row["monthly_active_learners"] == 40


def test_cross_grain_leaves_other_keys_alone():
    rows = [
        {"activity_year_and_month": "2026-06", "new_enrollments": 9},
        {"activity_year_and_month": "2026-07", "new_enrollments": 12},
    ]
    june, july = suppress_cross_grain_additives(
        rows, _ADDITIVES, {"2026-07": frozenset({"new_enrollments"})}
    )
    assert june["new_enrollments"] == 9
    assert july["new_enrollments"] is None


def test_cross_grain_with_nothing_withheld_changes_nothing():
    rows = [{"activity_year_and_month": "2026-07", "new_enrollments": 12}]
    assert suppress_cross_grain_additives(rows, _ADDITIVES, {}) == rows


def test_cross_grain_does_not_mutate_input_rows():
    rows = [{"activity_year_and_month": "2026-07", "new_enrollments": 12}]
    suppress_cross_grain_additives(rows, _ADDITIVES, {"2026-07": frozenset({"new_enrollments"})})
    assert rows[0]["new_enrollments"] == 12
