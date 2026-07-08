import pytest

from ol_analytics_api.core.db.identifiers import validate_sql_identifier


@pytest.mark.parametrize("value", ["b2b_analytics", "_leading_underscore", "Mixed_Case123"])
def test_accepts_safe_identifiers(value):
    assert validate_sql_identifier(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "1_starts_with_digit",
        "has-a-dash",
        "has a space",
        "has.a.dot",
        "semi;colon",
        "quote'",
        "org_slug = 1 OR 1=1 --",
    ],
)
def test_rejects_unsafe_identifiers(value):
    # This is the only identifier-splicing guard in the service (StarRocks
    # can't parameterize identifiers) — every one of these must be rejected,
    # not just the obviously-malicious ones.
    with pytest.raises(ValueError, match="Not a safe SQL identifier"):
        validate_sql_identifier(value)
