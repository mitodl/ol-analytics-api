"""End-to-end tests for the b2b_dashboard course-runs and courses endpoints.

Same harness as test_dashboard_learner_progress.py: drives the mounted app
over ASGITransport with an org manager's X-Userinfo header, stubbing only the
StarRocks pool and the MITx Online manager check. The order test at the end
executes ``learner_queries.course_runs()`` verbatim against an MV-shaped sqlite
table instead.
"""

import base64
import datetime as dt
import json
import re
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ol_analytics_api.core.db.refresh_metadata import _clear_cache
from ol_analytics_api.main import create_app
from ol_analytics_api.tenants.b2b_dashboard import learner_queries
from ol_analytics_api.tenants.b2b_dashboard.config import settings
from ol_analytics_api.tenants.b2b_dashboard.learner_models import (
    Course,
    CourseRun,
    CourseRunsResponse,
    CoursesResponse,
)
from tests.membership import answer_membership_probe, is_membership_probe

ORG_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
CONTRACT_ID = 101
PATH = f"/api/v1/analytics/organizations/{ORG_ID}/contracts/{CONTRACT_ID}/course-runs"
COURSES_PATH = PATH.removesuffix("course-runs") + "courses"
_AS_OF = dt.datetime(2026, 9, 15, 6, 0)  # noqa: DTZ001 - StarRocks returns naive UTC


def _manager_header(organization_id=ORG_ID):
    claims = {"sub": "kc-uuid-1", "organization": {"an-alias": {"id": organization_id}}}
    return base64.b64encode(json.dumps(claims).encode()).decode()


def _row(**overrides):
    return {
        "courserun_id": "course-v1:MITxT+14.310x+2T2026",
        "courserun_title": "Data Analysis for Social Scientists",
        "courserun_start_on": "2026-02-01T00:00:00",
        "courserun_end_on": None,
        **overrides,
    }


class _FakePool:
    """Answers the as_of probe, the contract gate, the count query and the page
    query, recording every call. Course runs carry no PII, so there's no
    consent or status-count bucketing to fake, unlike learner-progress's pool."""

    def __init__(self, rows=(), total_count=0, *, loaded=((ORG_ID, CONTRACT_ID),)):
        self.rows = list(rows)
        self.total_count = total_count
        self.loaded = loaded
        self.calls = []

    async def fetch_all(self, query, params=()):
        self.calls.append((query, params))
        if "information_schema" in query:
            return [{"as_of": _AS_OF}]
        if is_membership_probe(query):
            return answer_membership_probe(query, params, self.loaded)
        if "AS total_count" in query:
            return [{"total_count": self.total_count}]
        return self.rows

    def page_call(self):
        return next(call for call in self.calls if call[0].endswith("LIMIT %s OFFSET %s"))

    def count_call(self):
        return next(call for call in self.calls if "AS total_count" in call[0])


@pytest.fixture
def app():
    return create_app()


@pytest.fixture(autouse=True)
def _clear_as_of_cache():
    _clear_cache()
    yield
    _clear_cache()


async def _get(app, pool, path=PATH, *, is_manager=True, params=None):
    with (
        patch("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", new=pool.fetch_all),
        patch(
            "ol_analytics_api.tenants.b2b_dashboard.auth.mitxonline_client.is_org_manager",
            new=AsyncMock(return_value=is_manager),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path, params=params, headers={"X-Userinfo": _manager_header()})


async def test_envelope_lists_course_runs(app):
    pool = _FakePool(rows=[_row()], total_count=1)
    response = await _get(app, pool)

    assert response.status_code == 200
    body = response.json()
    assert body["organization_id"] == ORG_ID
    assert body["as_of"] == "2026-09-15T06:00:00Z"
    assert body["total_count"] == 1
    [row] = body["data"]
    assert row["courserun_id"] == "course-v1:MITxT+14.310x+2T2026"
    assert row["courserun_end_on"] is None
    assert set(row) == set(CourseRun.model_fields)


async def test_scopes_to_org_and_contract_with_bound_values(app):
    pool = _FakePool()
    await _get(app, pool)
    page_query, page_params = pool.page_call()
    assert "sso_organization_id = %s AND contract_id = %s" in page_query
    assert "b2b_learner_records.mv_b2b_contract_courserun" in page_query
    assert page_params[:2] == (ORG_ID, CONTRACT_ID)
    count_query, count_params = pool.count_call()
    assert "sso_organization_id = %s AND contract_id = %s" in count_query
    assert count_params == page_params[:-2]


async def test_empty_contract_returns_no_rows(app):
    pool = _FakePool(rows=[], total_count=0)
    response = await _get(app, pool)
    body = response.json()
    assert body["total_count"] == 0
    assert body["data"] == []


async def test_contract_not_in_org_is_403(app):
    pool = _FakePool(loaded=((OTHER_ORG_ID, CONTRACT_ID),))
    response = await _get(app, pool)
    assert response.status_code == 403
    assert not any("mv_b2b_contract_courserun" in query for query, _ in pool.calls)


async def test_non_manager_is_refused_before_any_query(app):
    pool = _FakePool()
    response = await _get(app, pool, is_manager=False)
    assert response.status_code == 403
    assert pool.calls == []


@pytest.mark.parametrize("model", [CourseRun, CourseRunsResponse])
def test_every_field_has_a_manager_facing_description(model):
    # Same rationale as learner-progress's own version of this test: the
    # dashboard can show these as help text to a manager, who never sees field
    # names, so every field needs one and none may lean on another field's name.
    field_names = set(CourseRun.model_fields) | set(CourseRunsResponse.model_fields)
    for name, field in model.model_fields.items():
        assert field.description, f"{name} has no description"
        named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", field.description)) & field_names
        assert not named, f"{name}'s description names {named}"


# --- Executed against an MV-shaped sqlite table -------------------------------
#
# sqlite sorts NULL first ascending, so these rows also prove the IS NULL keys
# that move nulls last.

_COURSERUN_MV_COLUMNS = (
    "sso_organization_id",
    "contract_id",
    "courserun_readable_id",
    "courserun_title",
    "courserun_start_on",
    "courserun_end_on",
    "program_sort_order",
    "course_order_in_program",
)

# readable id: (title, start, program_sort_order, course_order_in_program), in
# the order the endpoint lists them. Start dates run against the program keys,
# and runs tied on the earlier keys run against readable id order, so
# dropping any key but the last changes the result.
_RUNS = {
    # Two runs of one course: start date orders them.
    "intro-b": ("Introduction to Universal AI", "2026-09-01", 1, 1),
    "intro-a": ("Introduction to Universal AI", "2027-01-01", 1, 1),
    "module-12": ("Module 12", "2026-01-15", 1, 2),
    "second-program": ("Module 1", "2026-01-10", 2, 1),
    # A program MITx Online never ordered.
    "unordered-program": ("Module 2", "2026-01-05", None, 1),
    # In none of the contract's programs. Title orders the same-day pair.
    "outside-b": ("A module", "2026-01-01", None, None),
    "outside-a": ("B module", "2026-01-01", None, None),
    "outside-self-paced": ("A module", None, None, None),
}


def test_runs_follow_program_order_with_null_sort_keys_last():
    schema = settings.learner_records_schema
    table = f"{schema}.{learner_queries.CONTRACT_COURSERUN_MV}"
    conn = sqlite3.connect(":memory:")
    conn.execute(f"ATTACH ':memory:' AS {schema}")
    conn.execute(f"CREATE TABLE {table} ({', '.join(_COURSERUN_MV_COLUMNS)})")
    for run_id, (title, start, program_order, course_order) in reversed(_RUNS.items()):
        conn.execute(
            f"INSERT INTO {table} VALUES ({', '.join('?' * len(_COURSERUN_MV_COLUMNS))})",  # noqa: S608
            (ORG_ID, CONTRACT_ID, run_id, title, start, None, program_order, course_order),
        )
    query = learner_queries.course_runs(ORG_ID, CONTRACT_ID)
    rows = conn.execute(query.page.replace("%s", "?"), (*query.params, 100, 0)).fetchall()
    conn.close()
    assert [row[0] for row in rows] == list(_RUNS)


# --- /courses -------------------------------------------------------------------


async def test_courses_envelope_lists_courses(app):
    row = {"course_readable_id": "course-v1:MITxT+14.310x", "course_title": "Data Analysis"}
    pool = _FakePool(rows=[row], total_count=1)
    response = await _get(app, pool, COURSES_PATH)
    assert response.status_code == 200
    body = response.json()
    assert (body["organization_id"], body["as_of"], body["total_count"]) == (
        ORG_ID,
        "2026-09-15T06:00:00Z",
        1,
    )
    assert body["data"] == [row]
    page_query, page_params = pool.page_call()
    assert "b2b_learner_records.mv_b2b_contract_courserun" in page_query
    assert page_params == (ORG_ID, CONTRACT_ID, settings.default_page_size, 0)
    assert pool.count_call()[1] == page_params[:-2]


async def test_courses_refuse_a_contract_outside_the_org(app):
    pool = _FakePool(loaded=((OTHER_ORG_ID, CONTRACT_ID),))
    assert (await _get(app, pool, COURSES_PATH)).status_code == 403
    assert not any("mv_b2b_contract_courserun" in query for query, _ in pool.calls)


@pytest.mark.parametrize("model", [Course, CoursesResponse])
def test_every_courses_field_has_a_manager_facing_description(model):
    field_names = set(Course.model_fields) | set(CoursesResponse.model_fields)
    for name, field in model.model_fields.items():
        assert field.description, f"{name} has no description"
        named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", field.description)) & field_names
        assert not named, f"{name}'s description names {named}"


# Each run's course, title, program_sort_order and course_order_in_program.
# The courses come out once each, in program order, with the ones the
# Overview doesn't list left out.
_COURSE_RUNS = {
    "intro-2025": ("intro", "Introduction to Universal AI", 1, 1),
    "intro-2026": ("intro", "Introduction to Universal AI", 1, 1),
    "python-2": ("python", "Python Coding, Part 2", 1, 2),
    "second": ("second", "Module 1", 2, 1),
    "unordered": ("unordered", "Module 2", None, 1),
    # In none of the contract's programs, and a run with no course.
    "outside": ("outside", "Module 14", None, None),
    "no-course": (None, None, 1, 3),
}


def test_courses_follow_program_order_once_each():
    schema = settings.learner_records_schema
    table = f"{schema}.{learner_queries.CONTRACT_COURSERUN_MV}"
    columns = (
        "sso_organization_id",
        "contract_id",
        "courserun_readable_id",
        "course_readable_id",
        "course_title",
        "program_sort_order",
        "course_order_in_program",
    )
    conn = sqlite3.connect(":memory:")
    conn.execute(f"ATTACH ':memory:' AS {schema}")
    conn.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
    rows = [(ORG_ID, CONTRACT_ID, run, *course) for run, course in reversed(_COURSE_RUNS.items())]
    # Another contract's course doesn't count.
    rows.append((ORG_ID, CONTRACT_ID + 1, "other", "other", "Other", 1, 1))
    conn.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(columns))})", rows)  # noqa: S608
    query = learner_queries.courses(ORG_ID, CONTRACT_ID)
    page = conn.execute(query.page.replace("%s", "?"), (*query.params, 100, 0)).fetchall()
    [(total,)] = conn.execute(query.count.replace("%s", "?"), query.params).fetchall()
    conn.close()
    assert page == [
        ("intro", "Introduction to Universal AI"),
        ("python", "Python Coding, Part 2"),
        ("second", "Module 1"),
        ("unordered", "Module 2"),
    ]
    assert total == len(page)
