"""End-to-end tests for the b2b_learner_records tenant.

Drives the mounted app over ASGITransport with an X-Userinfo header shaped like
a client-credentials token's claims, stubbing only the StarRocks pool. The SQL
itself isn't executed here; these tests pin what it filters on and what it binds.
"""

import ast
import base64
import datetime
import json
import pathlib

import pytest
from httpx import ASGITransport, AsyncClient

from ol_analytics_api.core.db.refresh_metadata import _clear_cache
from ol_analytics_api.main import create_app
from ol_analytics_api.tenants import b2b_learner_records
from ol_analytics_api.tenants.b2b_learner_records import queries
from ol_analytics_api.tenants.b2b_learner_records.auth import NO_GRANT_DETAIL
from ol_analytics_api.tenants.b2b_learner_records.models import Enrollment, Learner

BASE = "/api/v1/learner-records"
ORG_ID = "8f14e45f-ceea-467a-9c1b-2f4b9c0a3d21"
OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
LEARNER_ID = "3e1a9c74-5b2d-4f88-9a01-7c6de2b4f019"
OTHER_LEARNER_ID = "c04e8a17-3d62-4b95-a7e8-51fb2c8d9042"
_AS_OF = datetime.datetime(2026, 8, 13, 6, 15)  # noqa: DTZ001 - StarRocks returns naive UTC


def _header(claims: dict) -> str:
    return base64.b64encode(json.dumps(claims).encode()).decode()


def _partner_header(*organization_ids, scope="learner-records:read"):
    return _header(
        {
            "azp": "contoso-lms",
            "scope": f"profile email {scope}",
            "learner_records_organizations": list(organization_ids),
        }
    )


def _learner_row(**overrides):
    return {
        "learner_id": LEARNER_ID,
        "email": "rgarcia@contoso.example",
        "full_name": "R. Garcia",
        "organization_id": ORG_ID,
        "organization_name": "Contoso Manufacturing",
        "membership_source": "both",
        "is_organization_manager": 0,
        "first_enrolled_on": "2026-02-03T14:22:11.000000",
        "last_enrolled_on": "2026-05-19T09:04:52.000000",
        "courses_enrolled": 4,
        "outcomes_shared": 0,
        "outcomes_consent_on": None,
        "last_active_on": None,
        "courses_in_progress": None,
        "courses_passed": None,
        "courses_certified": None,
        "certificates_earned": None,
        **overrides,
    }


def _enrollment_row(**overrides):
    return {
        "learner_id": LEARNER_ID,
        "email": "rgarcia@contoso.example",
        "full_name": None,
        "organization_id": ORG_ID,
        "contract_id": 42,
        "contract_name": "Contoso 2026 Site Licence",
        "courserun_id": "course-v1:MITxT+14.310x+2T2026",
        "courserun_title": "Data Analysis for Social Scientists",
        "courserun_start_on": "2026-02-01T00:00:00",
        "courserun_end_on": None,
        "enrolled_on": "2026-02-03T14:22:11.000000",
        "enrollment_is_active": 1,
        "enrollment_mode": "verified",
        "enrollment_status": None,
        "outcomes_shared": 0,
        "completion_status": None,
        "is_passing": None,
        "grade": None,
        "letter_grade": None,
        "certificate_issued_on": None,
        "certificate_is_revoked": None,
        "last_active_on": None,
        "days_active": None,
        "videos_watched": None,
        "problems_attempted": None,
        "chatbot_interactions": None,
        **overrides,
    }


@pytest.mark.parametrize("name", ["Learner", "Enrollment"])
def test_every_record_field_is_required_in_the_generated_schema(name):
    # The contract lists pending fields as required and nullable. A defaulted
    # field would generate as optional, and a client would treat it as absent.
    schema = b2b_learner_records.app.create_app().openapi()["components"]["schemas"][name]
    assert set(schema["required"]) == set(schema["properties"])


class _FakePool:
    """Answers the as_of probe, the count query and the page query, and records
    every call so a test can inspect the one it cares about."""

    def __init__(self, rows=(), total_count=0, withheld=0, as_of_by_mv=None):
        self.rows = list(rows)
        self.counts = {"total_count": total_count, "outcomes_withheld_count": withheld}
        self.as_of_by_mv = as_of_by_mv or {}
        self.calls = []

    async def fetch_all(self, query, params=()):
        self.calls.append((query, params))
        if "information_schema" in query:
            return [{"as_of": self.as_of_by_mv.get(params[1], _AS_OF)}]
        if "COUNT(*)" in query:
            return [self.counts]
        return self.rows

    def page_call(self):
        return next(call for call in self.calls if call[0].endswith("LIMIT %s OFFSET %s"))

    def count_call(self):
        return next(call for call in self.calls if "COUNT(*)" in call[0])


@pytest.fixture
def app():
    return create_app()


@pytest.fixture(autouse=True)
def _clear_as_of_cache():
    _clear_cache()
    yield
    _clear_cache()


async def _get(app, path, header=None, pool=None, monkeypatch=None):
    pool = pool or _FakePool()
    monkeypatch.setattr("ol_analytics_api.core.db.client.starrocks_pool.fetch_all", pool.fetch_all)
    headers = {"X-Userinfo": header} if header else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(f"{BASE}{path}", headers=headers)


async def test_requires_forwarded_claims(app, monkeypatch):
    response = await _get(app, f"/organizations/{ORG_ID}/learners", monkeypatch=monkeypatch)
    assert response.status_code == 401


async def test_user_token_without_the_grant_claim_is_refused(app, monkeypatch):
    # A logged-in org manager's token carries no learner_records_organizations
    # claim, so the b2b_dashboard audience can't reach individual records.
    pool = _FakePool()
    header = _header(
        {
            "sub": "kc-user",
            "scope": "openid learner-records:read",
            "organization": {"a": {"id": ORG_ID}},
        }
    )
    response = await _get(
        app, f"/organizations/{ORG_ID}/learners", header, pool, monkeypatch=monkeypatch
    )
    assert response.status_code == 403
    assert response.json() == {"detail": NO_GRANT_DETAIL}
    assert pool.calls == []


async def test_ungranted_and_nonexistent_organizations_are_indistinguishable(app, monkeypatch):
    header = _partner_header(ORG_ID)
    ungranted = await _get(
        app, f"/organizations/{OTHER_ORG_ID}/enrollments", header, monkeypatch=monkeypatch
    )
    missing = await _get(
        app,
        "/organizations/99999999-9999-9999-9999-999999999999/enrollments",
        header,
        monkeypatch=monkeypatch,
    )
    assert ungranted.status_code == missing.status_code == 403
    assert ungranted.json() == missing.json() == {"detail": NO_GRANT_DETAIL}


async def test_missing_scope_is_refused(app, monkeypatch):
    response = await _get(
        app,
        f"/organizations/{ORG_ID}/learners",
        _partner_header(ORG_ID, scope="learner-records:write"),
        monkeypatch=monkeypatch,
    )
    assert response.status_code == 403


@pytest.mark.parametrize("claim", [ORG_ID, f"{ORG_ID},{OTHER_ORG_ID}", {"id": ORG_ID}, None, [42]])
async def test_a_grant_claim_that_is_not_a_json_array_of_uuids_grants_nothing(
    app, monkeypatch, claim
):
    header = _header({"scope": "learner-records:read", "learner_records_organizations": claim})
    response = await _get(app, f"/organizations/{ORG_ID}/learners", header, monkeypatch=monkeypatch)
    assert response.status_code == 403


async def test_grant_matches_the_uuid_not_its_spelling(app, monkeypatch):
    response = await _get(
        app,
        f"/organizations/{ORG_ID}/learners",
        _partner_header(ORG_ID.upper()),
        monkeypatch=monkeypatch,
    )
    assert response.status_code == 200


async def test_learners_envelope_withholds_outcomes_and_counts_them(app, monkeypatch):
    pool = _FakePool(rows=[_learner_row()], total_count=47, withheld=47)
    response = await _get(
        app, f"/organizations/{ORG_ID}/learners", _partner_header(ORG_ID), pool, monkeypatch
    )

    assert response.status_code == 200
    body = response.json()
    assert body["organization_id"] == ORG_ID
    assert body["as_of"] == "2026-08-13T06:15:00Z"
    assert body["total_count"] == 47
    assert body["outcomes_withheld_count"] == 47
    [learner] = body["data"]
    assert learner["email"] == "rgarcia@contoso.example"
    assert learner["first_enrolled_on"] == "2026-02-03T14:22:11Z"
    assert learner["is_organization_manager"] is False
    assert learner["outcomes_shared"] is False
    for field in ("courses_passed", "last_active_on", "courses_in_progress", "outcomes_consent_on"):
        assert learner[field] is None
    # Every field the contract requires is present, nulls included.
    assert set(learner) == set(Learner.model_fields)


async def test_outcome_columns_read_null_while_consent_is_absent(app, monkeypatch):
    pool = _FakePool()
    await _get(
        app, f"/organizations/{ORG_ID}/enrollments", _partner_header(ORG_ID), pool, monkeypatch
    )
    page_query, _ = pool.page_call()
    assert "FALSE AS outcomes_shared" in page_query
    assert "CASE WHEN FALSE THEN grade END AS grade" in page_query
    count_query, _ = pool.count_call()
    assert "SUM(CASE WHEN FALSE THEN 0 ELSE 1 END) AS outcomes_withheld_count" in count_query


def test_models_null_outcomes_a_row_carries_when_not_shared():
    learner = Learner(**_learner_row(courses_passed=3, courses_certified=3, certificates_earned=4))
    assert (learner.courses_passed, learner.courses_certified, learner.certificates_earned) == (
        None,
        None,
        None,
    )
    enrollment = Enrollment(
        **_enrollment_row(completion_status="certified", is_passing=1, grade=0.91, days_active=3)
    )
    assert (enrollment.completion_status, enrollment.is_passing, enrollment.grade) == (
        None,
        None,
        None,
    )
    assert enrollment.days_active is None


def test_models_keep_outcomes_when_shared():
    enrollment = Enrollment(
        **_enrollment_row(outcomes_shared=1, completion_status="passed", is_passing=1, grade=0.8)
    )
    assert (enrollment.completion_status, enrollment.grade) == ("passed", 0.8)


async def test_default_learners_read_the_precomputed_rollup(app, monkeypatch):
    pool = _FakePool()
    await _get(app, f"/organizations/{ORG_ID}/learners", _partner_header(ORG_ID), pool, monkeypatch)
    query, params = pool.page_call()
    assert f"FROM b2b_learner_records.{queries.LEARNER_MV} WHERE" in query
    assert queries.ENROLLMENT_MV not in query
    assert query.endswith("ORDER BY learner_id, user_pk LIMIT %s OFFSET %s")
    assert params == (ORG_ID, 100, 0)


async def test_contract_filter_recomputes_learners_from_that_contracts_enrollments(
    app, monkeypatch
):
    pool = _FakePool()
    await _get(
        app,
        f"/organizations/{ORG_ID}/learners?contract_id=42&limit=10&offset=20",
        _partner_header(ORG_ID),
        pool,
        monkeypatch,
    )
    query, params = pool.page_call()
    assert "RIGHT JOIN (SELECT user_pk, MAX(user_global_id)" in query
    assert "contract_id = %s AND enrollment_is_active = TRUE GROUP BY user_pk" in query
    # Both org predicates are bound, then the contract, then paging.
    assert params == (ORG_ID, ORG_ID, 42, 10, 20)


async def test_include_inactive_learners_keep_every_roster_member(app, monkeypatch):
    pool = _FakePool()
    await _get(
        app,
        f"/organizations/{ORG_ID}/learners?include_inactive=true",
        _partner_header(ORG_ID),
        pool,
        monkeypatch,
    )
    query, params = pool.page_call()
    assert "FULL OUTER JOIN" in query
    assert "enrollment_is_active" not in query
    assert params == (ORG_ID, ORG_ID, 100, 0)


async def test_recomputed_learners_report_the_staler_view(app, monkeypatch):
    older = datetime.datetime(2026, 8, 12, 6, 0)  # noqa: DTZ001
    pool = _FakePool(as_of_by_mv={queries.ENROLLMENT_MV: older})
    response = await _get(
        app,
        f"/organizations/{ORG_ID}/learners?contract_id=42",
        _partner_header(ORG_ID),
        pool,
        monkeypatch,
    )
    assert response.json()["as_of"] == "2026-08-12T06:00:00Z"


async def test_enrollment_filters_are_bound_in_order(app, monkeypatch):
    pool = _FakePool(rows=[_enrollment_row()], total_count=1, withheld=1)
    response = await _get(
        app,
        f"/organizations/{ORG_ID}/enrollments"
        "?contract_id=42&courserun_id=course-v1:MITxT%2B14.310x%2B2T2026"
        f"&learner_id={LEARNER_ID}&learner_id={OTHER_LEARNER_ID}"
        "&completion_status=passed&completion_status=unknown"
        "&updated_since=2026-08-12T06:15:00.5%2B02:00",
        _partner_header(ORG_ID),
        pool,
        monkeypatch,
    )
    assert response.status_code == 200
    query, params = pool.page_call()
    assert "enrollment_is_active = TRUE" in query
    assert "learner_id IN (%s, %s)" in query
    assert "((FALSE AND completion_status IN (%s)) OR NOT FALSE)" in query
    assert params == (
        ORG_ID,
        42,
        "course-v1:MITxT+14.310x+2T2026",
        LEARNER_ID,
        OTHER_LEARNER_ID,
        # UTC, truncated to the second, so the boundary record is re-sent.
        "2026-08-12T04:15:00",
        "passed",
        100,
        0,
    )
    # The count binds the same filters without paging.
    assert pool.count_call()[1] == params[:-2]


@pytest.mark.parametrize(
    "query",
    [
        "learner_id=not-a-uuid",
        "limit=1001",
        "completion_status=done",
        "&".join(f"learner_id=00000000-0000-0000-0000-{n:012d}" for n in range(101)),
    ],
)
async def test_malformed_parameters_are_400_with_a_string_detail(app, monkeypatch, query):
    response = await _get(
        app,
        f"/organizations/{ORG_ID}/enrollments?{query}",
        _partner_header(ORG_ID),
        monkeypatch=monkeypatch,
    )
    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)


def test_cursor_value_is_a_prefix_of_stored_values_in_the_same_second():
    bound = queries._cursor_value(  # noqa: SLF001
        datetime.datetime(2026, 8, 12, 6, 15, 0, 999999, tzinfo=datetime.UTC)
    )
    assert bound == "2026-08-12T06:15:00"
    for stored in ("2026-08-12T06:15:00", "2026-08-12T06:15:00.000", "2026-08-12T06:15:00.000000"):
        assert stored >= bound


def test_tenant_never_imports_the_anonymization_module():
    """The two tenants' privacy postures should be legible from their imports."""
    package = pathlib.Path(b2b_learner_records.__file__).parent
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert "anonymization" not in (node.module or ""), path
                assert all("anonymization" not in alias.name for alias in node.names), path
            if isinstance(node, ast.Import):
                assert all("anonymization" not in alias.name for alias in node.names), path


def test_generated_schema_declares_the_client_credentials_scheme():
    # Without it a generated client has no reason to obtain or send a token.
    spec = b2b_learner_records.app.create_app().openapi()
    scheme = spec["components"]["securitySchemes"]["oauth2ClientCredentials"]
    assert "learner-records:read" in scheme["flows"]["clientCredentials"]["scopes"]
    for operations in spec["paths"].values():
        assert operations["get"]["security"] == [
            {"oauth2ClientCredentials": ["learner-records:read"]}
        ]


async def test_as_of_is_read_before_the_records(app, monkeypatch):
    # Read after, a refresh landing in between would label old rows with the new
    # refresh time, and a client syncing from that as_of would skip the new rows.
    pool = _FakePool()
    await _get(
        app, f"/organizations/{ORG_ID}/enrollments", _partner_header(ORG_ID), pool, monkeypatch
    )
    kinds = [
        "as_of" if "information_schema" in query else "count" if "COUNT(*)" in query else "page"
        for query, _ in pool.calls
    ]
    assert kinds.index("as_of") < kinds.index("page")
    assert kinds.index("as_of") < kinds.index("count")
