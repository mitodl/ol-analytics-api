from ol_analytics_api.core.anonymization import suppress_small_cohorts


def test_suppress_small_cohorts_filters_below_floor():
    rows = [
        {"org": "a", "seats_consumed": 10},
        {"org": "b", "seats_consumed": 4},
        {"org": "c", "seats_consumed": 5},
    ]
    result = suppress_small_cohorts(rows, "seats_consumed", floor=5)
    assert {row["org"] for row in result} == {"a", "c"}


def test_suppress_small_cohorts_missing_field_defaults_to_suppressed():
    rows = [{"org": "a"}]
    assert suppress_small_cohorts(rows, "seats_consumed", floor=5) == []


def test_suppress_small_cohorts_null_value_does_not_crash():
    # A NULL column from a LEFT JOIN in the source MV comes back as `None`,
    # not a missing key — `.get(field, 0)`'s default wouldn't apply, and
    # `None >= floor` would raise TypeError if not handled explicitly.
    rows = [{"org": "a", "seats_consumed": None}]
    assert suppress_small_cohorts(rows, "seats_consumed", floor=5) == []
