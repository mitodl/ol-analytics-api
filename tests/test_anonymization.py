import pytest

from ol_analytics_api.core.anonymization import CohortPolicy, suppress_small_cohorts


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
    )
    (row,) = suppress_small_cohorts(rows, policy, floor=5)
    assert row["engaged_learners"] == 40  # above floor, retained
    assert row["chatbot_users"] is None
    assert row["certificates_earned"] is None


def test_secondary_count_of_zero_is_kept():
    # 0 discloses no individual — it stays visible as a real "nobody did this".
    rows = [{"total_enrolled_learners": 50, "certificates_earned": 0}]
    policy = CohortPolicy(primary="total_enrolled_learners", secondary=("certificates_earned",))
    (row,) = suppress_small_cohorts(rows, policy, floor=5)
    assert row["certificates_earned"] == 0


def test_null_secondary_count_is_treated_as_disclosive():
    rows = [{"total_enrolled_learners": 50, "certificates_earned": None}]
    policy = CohortPolicy(primary="total_enrolled_learners", secondary=("certificates_earned",))
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
    )
    (row,) = suppress_small_cohorts(rows, policy, floor=5)
    assert row["engaged_learners"] == 30
    assert row["avg_videos_per_engaged_learner"] == 4.0


def test_input_rows_are_not_mutated():
    rows = [{"total_enrolled_learners": 50, "certificates_earned": 1}]
    policy = CohortPolicy(primary="total_enrolled_learners", secondary=("certificates_earned",))
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
        )


def test_derived_referencing_primary_cohort_is_allowed():
    CohortPolicy(
        primary="total_enrolled_learners",
        derived={"avg_videos_per_learner": ("total_enrolled_learners",)},
    )
