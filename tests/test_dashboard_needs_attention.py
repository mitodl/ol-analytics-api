"""Tests for the b2b_dashboard needs-attention aggregate endpoints.

Two layers, and the split is deliberate. The sqlite tests EXECUTE the
production query strings — the real ``needs_attention_aggregate`` output, not a
rewritten copy — against rows in an in-memory database, because what this
endpoint exists for is a value property: one learner counts once however many
of a contract's courses they have gone quiet in. Asserting on generated SQL
text can never show that. The ASGI tests then pin the envelope, the scoping,
the suppression and the auth gates, stubbing the pool.

The sqlite databases are ATTACHed under the schema name the query names, so
the production string runs verbatim rather than being edited to point at a bare
table. ``%s`` becomes ``?`` because that is the driver's placeholder dialect,
not part of what is under test.
"""

import base64
import datetime
import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ol_analytics_api.core.db.identifiers import validate_sql_identifier
from ol_analytics_api.core.db.refresh_metadata import _clear_cache
from ol_analytics_api.main import create_app
from ol_analytics_api.tenants.b2b_dashboard import learner_queries
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.models import ContractNeedsAttention

ORG_ID = "11111111-1111-1111-1111-111111111111"
CONTRACT_ID = 101
ORG_PATH = f"/api/v1/analytics/organizations/{ORG_ID}/needs-attention"
CONTRACT_PATH = f"/api/v1/analytics/organizations/{ORG_ID}/contracts/{CONTRACT_ID}/needs-attention"
_AS_OF = datetime.datetime(2026, 9, 15, 6, 0)  # noqa: DTZ001 - StarRocks returns naive UTC
# What the cluster answers NEEDS_ATTENTION_CUTOFF_QUERY with: 30 days before
# _AS_OF's date. Fixed, so the SQL these tests assert on is stable.
_CUTOFF = datetime.date(2026, 8, 16)
# Bound into the page query's HAVING by the tests whose subject is the
# needs-attention rule rather than the k-anonymity floor: their fixtures are a
# handful of learners on purpose, and the real floor would gate them all out.
# The two tests that ARE about the floor bind it explicitly instead.
_NO_FLOOR = 1

# The MV columns the aggregate's inner select reads. Same shape as the
# learner-progress sqlite fixtures, plus the scoping and grain columns this
# query groups and filters on.
_MV_COLUMNS = (
    "sso_organization_id TEXT",
    "contract_id INTEGER",
    "user_global_id TEXT",
    "enrollment_is_active INTEGER",
    "certificate_is_revoked INTEGER",
    "is_passing INTEGER",
    "grade_value REAL",
    "last_active_on TEXT",
)


def _enrollment(
    learner,
    *,
    contract_id=CONTRACT_ID,
    organization_id=ORG_ID,
    active=1,
    revoked=1,
    passing=0,
    grade=None,
    last_active_on=None,
):
    """One row of mv_b2b_learner_enrollment.

    ``revoked=1`` is the default because _COMPLETION_STATUS reads an
    *unrevoked* certificate as certified; 1 keeps a row out of that branch so
    the grade and activity columns decide its status, which is what these
    tests vary.
    """
    return (
        organization_id,
        contract_id,
        learner,
        active,
        revoked,
        passing,
        grade,
        last_active_on,
    )


def _db(rows):
    """An in-memory stand-in for the learner-records schema, ATTACHed under the
    name the production query actually uses, so that query needs no rewriting."""
    conn = sqlite3.connect(":memory:")
    schema = validate_sql_identifier(settings.learner_records_schema)
    conn.execute(f"ATTACH ':memory:' AS {schema}")
    conn.execute(
        f"CREATE TABLE {schema}.{learner_queries.ENROLLMENT_MV} ({', '.join(_MV_COLUMNS)})"
    )
    conn.executemany(
        f"INSERT INTO {schema}.{learner_queries.ENROLLMENT_MV} VALUES "  # noqa: S608
        f"({', '.join('?' * len(_MV_COLUMNS))})",
        rows,
    )
    return conn


def _run(conn, query, params):
    """Execute a production query string, translating only the placeholder
    dialect (``%s`` -> ``?``)."""
    return conn.execute(query.replace("%s", "?"), params)


@pytest.fixture
def _fail_open(monkeypatch):
    """Consent fail-open, which is what every deployed stack sets. The code
    default is fail-closed, under which every outcome -- needing attention
    included -- is withheld and the count is 0 everywhere; the consent test
    below pins both."""
    monkeypatch.setattr(settings, "consent_fail_open", True)


@pytest.mark.usefixtures("_fail_open")
def test_counts_each_learner_once_however_many_courses_they_are_quiet_in():
    # The defect this endpoint exists to fix. learner-progress's
    # needs_attention_count is a SUM over a learner x course-run grain, so one
    # learner stale in three of the contract's courses reads as 3 -- which
    # cannot sit beside active_learners, a distinct-learner count. Executed
    # against real rows, not asserted on SQL text, because the units are the
    # whole point.
    quiet = (_CUTOFF - datetime.timedelta(days=1)).isoformat()
    rows = [
        # One learner, quiet in three of the contract's course runs.
        *(_enrollment("learner-a", grade=0.4, last_active_on=quiet) for _ in range(3)),
        # A second learner, quiet in one.
        _enrollment("learner-b", grade=0.4, last_active_on=quiet),
        # A third, active yesterday in two runs: not quiet in either.
        *(
            _enrollment(
                "learner-c",
                grade=0.4,
                last_active_on=(
                    datetime.date(2026, 9, 15) - datetime.timedelta(days=1)
                ).isoformat(),
            )
            for _ in range(2)
        ),
    ]
    conn = _db(rows)
    query = learner_queries.needs_attention_aggregate(ORG_ID, CONTRACT_ID, _CUTOFF)
    [row] = _run(conn, query.page, (*query.params, _NO_FLOOR, 100, 0)).fetchall()

    # 6 enrollments, 3 learners, 2 of them needing attention.
    assert row == (CONTRACT_ID, 3, 2, 0)

    # And the same rows through learner-progress's enrollment-grain count,
    # built from the production expression rather than a copy of it, to show
    # the two really do disagree and by how much. 4 enrollments, 2 learners.
    needs_attention = learner_queries._needs_attention(_CUTOFF)  # noqa: SLF001
    enrollment_grain = _run(
        conn,
        "SELECT SUM(CASE WHEN TRUE AND"  # noqa: S608
        f" ({needs_attention}) THEN 1 ELSE 0 END) FROM (SELECT *,"
        f" {learner_queries._COMPLETION_STATUS} AS completion_status"  # noqa: SLF001
        f" FROM {settings.learner_records_schema}.{learner_queries.ENROLLMENT_MV})",
        (),
    ).fetchone()[0]
    conn.close()
    assert enrollment_grain == 4
    assert enrollment_grain != row[2]


@pytest.mark.usefixtures("_fail_open")
def test_projects_exactly_the_model_fields_in_order():
    # The hand-built analogue of test_column_contract's
    # test_query_projects_exactly_model_fields. This query is not built by
    # build_select, so nothing derives its projection from the model: a
    # mistyped alias would surface only as a TypeError constructing
    # ContractNeedsAttention on a live request. Executing it and reading the
    # cursor's column names catches that, and proves the SQL parses.
    conn = _db([_enrollment("learner-a")])
    query = learner_queries.needs_attention_aggregate(ORG_ID, CONTRACT_ID, _CUTOFF)
    cursor = _run(conn, query.page, (*query.params, _NO_FLOOR, 100, 0))
    columns = [description[0] for description in cursor.description]
    conn.close()

    assert columns == list(ContractNeedsAttention.model_fields)


def test_consent_gate_moves_learners_between_the_counts(monkeypatch):
    # learners_considered is not consent-gated and the other two are, mirroring
    # learner-progress: being enrolled is not an outcome, so it is disclosed,
    # while anything about progress is gated. So a withheld learner is counted
    # in the first and the third, never the second -- and the three never sum.
    quiet = (_CUTOFF - datetime.timedelta(days=1)).isoformat()
    conn = _db(
        [
            _enrollment("learner-a", grade=0.4, last_active_on=quiet),
            _enrollment("learner-b", grade=0.4, last_active_on=quiet),
        ]
    )

    def aggregate():
        query = learner_queries.needs_attention_aggregate(ORG_ID, CONTRACT_ID, _CUTOFF)
        return _run(conn, query.page, (*query.params, _NO_FLOOR, 100, 0)).fetchall()

    # The code default fails closed, so no learner's quietness is disclosed and
    # both are reported as withheld instead.
    assert type(settings)().consent_fail_open is False
    assert aggregate() == [(CONTRACT_ID, 2, 0, 2)]

    monkeypatch.setattr(settings, "consent_fail_open", True)
    assert aggregate() == [(CONTRACT_ID, 2, 2, 0)]
    conn.close()


@pytest.mark.usefixtures("_fail_open")
def test_boundary_is_the_same_day_the_learner_directory_uses():
    # The tile and the directory must agree about a learner whose 30th quiet
    # day is today, which is the reason this aggregate reuses
    # ._needs_attention against a per-request cutoff instead of restating the
    # rule. Day 30 counts ("at least 30 days ago" includes the 30th); day 29
    # does not.
    conn = _db(
        [
            _enrollment("day-30", grade=0.4, last_active_on=_CUTOFF.isoformat()),
            _enrollment(
                "day-29",
                grade=0.4,
                last_active_on=(_CUTOFF + datetime.timedelta(days=1)).isoformat(),
            ),
            # Never started: quiet by definition, with no timestamp involved.
            _enrollment("never-started"),
            # A grade but no tracked activity. The staleness comparison is NULL
            # for this row; ._needs_attention's COALESCE settles it as "no", so
            # it is counted in neither direction rather than vanishing.
            _enrollment("graded-no-activity", grade=0.4),
        ]
    )
    query = learner_queries.needs_attention_aggregate(ORG_ID, CONTRACT_ID, _CUTOFF)
    [row] = _run(conn, query.page, (*query.params, _NO_FLOOR, 100, 0)).fetchall()
    conn.close()

    assert row == (CONTRACT_ID, 4, 2, 0)


@pytest.mark.usefixtures("_fail_open")
def test_inactive_enrollments_are_left_out_like_the_directory_default():
    # learner_progress's include_inactive defaults to false, so a manager
    # clicking through from this tile sees only active enrollments. The tile
    # must count that same population or it sends them to a shorter list.
    quiet = (_CUTOFF - datetime.timedelta(days=1)).isoformat()
    conn = _db(
        [
            _enrollment("active", grade=0.4, last_active_on=quiet),
            _enrollment("unenrolled", active=0, grade=0.4, last_active_on=quiet),
        ]
    )
    query = learner_queries.needs_attention_aggregate(ORG_ID, CONTRACT_ID, _CUTOFF)
    [row] = _run(conn, query.page, (*query.params, _NO_FLOOR, 100, 0)).fetchall()
    conn.close()

    assert row == (CONTRACT_ID, 1, 1, 0)


@pytest.mark.usefixtures("_fail_open")
def test_org_scope_returns_one_row_per_contract_and_never_crosses_orgs():
    quiet = (_CUTOFF - datetime.timedelta(days=1)).isoformat()
    other_org = "22222222-2222-2222-2222-222222222222"
    conn = _db(
        [
            _enrollment("learner-a", contract_id=101, grade=0.4, last_active_on=quiet),
            _enrollment("learner-b", contract_id=102, grade=0.4, last_active_on=quiet),
            _enrollment("learner-b", contract_id=102),
            # Same contract id under a different org. Contract ids are globally
            # unique but not secret, so the org predicate is what keeps this
            # out -- not the absence of a collision.
            _enrollment("intruder", contract_id=101, organization_id=other_org),
        ]
    )
    query = learner_queries.needs_attention_aggregate(ORG_ID, None, _CUTOFF)
    rows = _run(conn, query.page, (*query.params, _NO_FLOOR, 100, 0)).fetchall()
    conn.close()

    assert rows == [(101, 1, 1, 0), (102, 1, 1, 0)]


@pytest.mark.usefixtures("_fail_open")
def test_page_and_count_describe_the_same_gated_set():
    """Regression: the page used to be ungated while total_count was gated.

    Two sub-floor contracts sorting ahead of a visible one made the first page
    come back empty beside a positive total_count, leaving the visible contract
    reachable only by guessing an offset -- and offset-probing then revealed how
    many suppressed contracts preceded it, which is the figure gating the count
    exists to withhold. Both now carry the same HAVING.
    """
    rows = [
        # 101 and 102 are sub-floor and sort first; 103 is visible.
        *(_enrollment(f"l101-{n}", contract_id=101) for n in range(2)),
        *(_enrollment(f"l102-{n}", contract_id=102) for n in range(2)),
        *(_enrollment(f"l103-{n}", contract_id=103) for n in range(6)),
    ]
    conn = _db(rows)
    query = learner_queries.needs_attention_aggregate(ORG_ID, None, _CUTOFF)
    floor = settings.anonymization_floor
    first_page = _run(conn, query.page, (*query.params, floor, 2, 0)).fetchall()
    [(total,)] = _run(conn, query.count, (*query.params, floor)).fetchall()
    conn.close()

    # The visible contract is on the first page, not behind two hidden ones.
    assert [row[0] for row in first_page] == [103]
    assert total == 1
    assert len(first_page) == total


@pytest.mark.usefixtures("_fail_open")
def test_count_query_applies_the_primary_cohort_floor():
    # suppress_small_cohorts drops rows whose learners_considered is below the
    # floor after the query returns, so the count has to apply the same gate in
    # SQL. An ungated COUNT would exceed anything paging can reach, and the
    # difference would tell the caller how many sub-floor contracts their org
    # has -- the disclosure the floor exists to prevent.
    conn = _db(
        [
            *(_enrollment(f"big-{n}", contract_id=101) for n in range(5)),
            *(_enrollment(f"small-{n}", contract_id=102) for n in range(2)),
        ]
    )
    query = learner_queries.needs_attention_aggregate(ORG_ID, None, _CUTOFF)
    page = _run(conn, query.page, (*query.params, 5, 100, 0)).fetchall()
    [(total,)] = _run(conn, query.count, (*query.params, 5)).fetchall()
    conn.close()

    # The sub-floor contract is gated out of both, so neither can disclose how
    # many of them the org has.
    assert [row[0] for row in page] == [101]
    assert total == 1


# --- ASGI layer -----------------------------------------------------------


def _manager_header(organization_id=ORG_ID):
    claims = {"sub": "kc-uuid-1", "organization": {"an-alias": {"id": organization_id}}}
    return base64.b64encode(json.dumps(claims).encode()).decode()


class _FakePool:
    """Answers the cutoff probe, the as_of probe, the contract gate, the gated
    count and the aggregate itself, recording every call."""

    def __init__(self, rows=(), total_count=0, *, contract_exists=True):
        self.rows = list(rows)
        self.total_count = total_count
        self.contract_exists = contract_exists
        self.calls = []

    async def fetch_all(self, query, params=()):
        self.calls.append((query, params))
        if query == learner_queries.NEEDS_ATTENTION_CUTOFF_QUERY:
            return [{"cutoff": _CUTOFF}]
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if query.startswith("SELECT 1 "):
            return [{"1": 1}] if self.contract_exists else []
        if "COUNT(*)" in query:
            return [{"total_count": self.total_count}]
        return self.rows

    def page_call(self):
        return next(call for call in self.calls if call[0].endswith("LIMIT %s OFFSET %s"))

    def count_call(self):
        return next(call for call in self.calls if "COUNT(*)" in call[0])


def _row(**overrides):
    return {
        "contract_id": CONTRACT_ID,
        "learners_considered": 42,
        "learners_needing_attention": 11,
        "learners_outcomes_withheld": 0,
        **overrides,
    }


@pytest.fixture
def app():
    return create_app()


@pytest.fixture(autouse=True)
def _clear_as_of_cache():
    _clear_cache()
    yield
    _clear_cache()


async def _get(app, pool, path=CONTRACT_PATH, *, is_manager=True, params=None):
    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=pool.fetch_all),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=is_manager),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path, params=params, headers={"X-Userinfo": _manager_header()})


async def test_envelope_carries_the_learner_mvs_own_freshness(app):
    pool = _FakePool(rows=[_row()], total_count=1)
    response = await _get(app, pool)

    assert response.status_code == 200
    body = response.json()
    assert body["organization_id"] == ORG_ID
    assert body["as_of"] == "2026-09-15T06:00:00"
    assert body["total_count"] == 1
    assert body["data"] == [_row()]
    # as_of is read from the learner-records schema, not the aggregate one. A
    # client showing this tile beside contract-utilization's three must read
    # each endpoint's own freshness; that is what the per-section as_of is for.
    [as_of_params] = [params for query, params in pool.calls if "information_schema" in query]
    assert as_of_params == (settings.learner_records_schema, learner_queries.ENROLLMENT_MV)


async def test_org_route_binds_the_org_and_the_contract_route_binds_both(app):
    pool = _FakePool()
    await _get(app, pool, path=ORG_PATH)
    assert pool.page_call()[1] == (ORG_ID, settings.anonymization_floor, 100, 0)
    assert pool.count_call()[1] == (ORG_ID, settings.anonymization_floor)
    assert "contract_id = %s" not in pool.page_call()[0]

    pool = _FakePool()
    await _get(app, pool)
    assert pool.page_call()[1] == (ORG_ID, CONTRACT_ID, settings.anonymization_floor, 100, 0)
    assert pool.count_call()[1] == (ORG_ID, CONTRACT_ID, settings.anonymization_floor)
    # The org predicate is never dropped when the contract one is added.
    assert "sso_organization_id = %s AND contract_id = %s" in pool.page_call()[0]


async def test_sub_floor_contract_is_withheld_whole(app):
    # learners_considered is the primary cohort, so a contract with too few
    # learners does not appear at all.
    pool = _FakePool(rows=[_row(learners_considered=4, learners_needing_attention=4)])
    response = await _get(app, pool, path=ORG_PATH)

    assert response.status_code == 200
    assert response.json()["data"] == []


async def test_sub_floor_needs_attention_count_is_nulled_not_the_row(app):
    # The secondary counts are nulled on their own terms, so the tile renders a
    # suppressed "--" beside a real learners_considered rather than the whole
    # card disappearing. A count of exactly 0 is kept: it names no individual.
    pool = _FakePool(
        rows=[
            _row(learners_needing_attention=2),
            _row(contract_id=102, learners_needing_attention=0),
        ]
    )
    response = await _get(app, pool, path=ORG_PATH)

    rows = response.json()["data"]
    assert rows[0]["learners_considered"] == 42
    assert rows[0]["learners_needing_attention"] is None
    assert rows[1]["learners_needing_attention"] == 0


async def test_cutoff_is_resolved_once_and_shared_by_the_page_and_the_count(app):
    # The page and the count are two round trips. If the cutoff were left as
    # CURRENT_DATE() in the SQL they could evaluate it either side of midnight
    # and report a count the rows contradict, so it is resolved once per
    # request and spliced into both.
    pool = _FakePool(rows=[_row()], total_count=1)
    await _get(app, pool)

    probes = [
        query for query, _ in pool.calls if query == learner_queries.NEEDS_ATTENTION_CUTOFF_QUERY
    ]
    assert len(probes) == 1
    needs_attention = learner_queries._needs_attention(_CUTOFF)  # noqa: SLF001
    assert needs_attention in pool.page_call()[0]
    # The count query gates on the primary cohort only, so it carries the
    # scope but not the predicate -- the two cannot disagree about the cutoff
    # because only one of them applies it.
    assert "COUNT(DISTINCT learner_id) >= %s" in pool.count_call()[0]


async def test_contract_not_in_org_is_403(app):
    pool = _FakePool(contract_exists=False)
    response = await _get(app, pool)
    assert response.status_code == 403


async def test_non_manager_is_403_on_both_routes(app):
    for path in (ORG_PATH, CONTRACT_PATH):
        response = await _get(app, _FakePool(), path=path, is_manager=False)
        assert response.status_code == 403


async def test_pagination_bounds_are_enforced(app):
    pool = _FakePool()
    response = await _get(app, pool, path=ORG_PATH, params={"limit": 10, "offset": 20})
    assert response.status_code == 200
    assert pool.page_call()[1] == (ORG_ID, settings.anonymization_floor, 10, 20)

    response = await _get(app, _FakePool(), path=ORG_PATH, params={"limit": 0})
    assert response.status_code == 422


# --- governance -----------------------------------------------------------
#
# ContractNeedsAttention is not in test_column_contract's _CASES: those cases
# are built from build_select, and this model's query is hand-written. The two
# checks below are the ones from that file which are about the model rather
# than the builder, so a suppressible model added outside build_select does not
# skip them.


def test_cohort_policy_columns_are_real_model_fields():
    # A typo'd cohort column would silently never suppress -- a governance bug,
    # not a cosmetic one.
    fields = set(ContractNeedsAttention.model_fields)
    policy = ContractNeedsAttention.cohort_policy
    assert policy.primary in fields
    assert set(policy.secondary) <= fields


def test_every_field_is_described_in_the_published_schema():
    properties = ContractNeedsAttention.model_json_schema()["properties"]
    for name, field in ContractNeedsAttention.model_fields.items():
        assert field.description, f"{name} has no description"
        assert properties[name].get("description") == field.description
