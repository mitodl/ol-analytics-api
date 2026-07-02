from ol_analytics_api.anonymization import suppress_small_cohorts


def test_suppress_small_cohorts_filters_below_floor():
    rows = [
        {"org": "a", "seats_consumed": 10},
        {"org": "b", "seats_consumed": 4},
        {"org": "c", "seats_consumed": 5},
    ]
    result = suppress_small_cohorts(rows, "seats_consumed")
    assert {row["org"] for row in result} == {"a", "c"}


def test_suppress_small_cohorts_missing_field_defaults_to_suppressed():
    rows = [{"org": "a"}]
    assert suppress_small_cohorts(rows, "seats_consumed") == []
