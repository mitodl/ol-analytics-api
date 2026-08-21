"""The published OpenAPI contract.

`openapi/specs/<tenant>.yaml` is what the Concourse client pipeline generates
the TypeScript package from, so these assertions are about what consumers
receive, not about FastAPI's internals.
"""

from pathlib import Path

import pytest

from ol_analytics_api.main import TENANTS
from ol_analytics_api.openapi import render, tenant_specs

SPECS_DIR = Path(__file__).resolve().parent.parent / "openapi" / "specs"


@pytest.fixture(scope="module")
def specs():
    return tenant_specs()


def test_committed_spec_matches_the_code(specs):
    """The whole reason the spec is committed: drift is a failing test, not a
    consumer discovering a renamed column at runtime."""
    for tenant_name, spec in specs.items():
        path = SPECS_DIR / f"{tenant_name}.yaml"
        assert path.exists(), f"{path} is missing. Run `uv run bin/generate-openapi-spec`."
        assert path.read_text() == render(spec), (
            f"{path} is out of date. Run `uv run bin/generate-openapi-spec`."
        )


def test_every_mounted_tenant_publishes_a_spec(specs):
    assert set(specs) == {tenant.name for tenant in TENANTS}


def test_paths_carry_the_mount_prefix(specs):
    """A sub-app describes its routes relative to its own root, but a generated
    client is configured with the service host as its base URL. Publishing the
    unprefixed paths would produce a client that requests URLs the service does
    not serve."""
    for tenant in TENANTS:
        paths = specs[tenant.name]["paths"]
        assert paths, f"{tenant.name} published no paths at all"
        assert all(path.startswith(f"{tenant.mount_path}/") for path in paths)


def test_each_row_model_gets_its_own_response_schema(specs):
    """The org and contract endpoints are registered in a loop over a table of
    specs, parametrizing one generic envelope at runtime. If that collapsed to
    a single `OrgAnalyticsResponse` component, every panel would generate the
    same untyped row and the whole point of generating a client would be lost.
    """
    schemas = specs["b2b_dashboard"]["components"]["schemas"]
    envelopes = {name for name in schemas if name.startswith("OrgAnalyticsResponse")}
    row_models = {
        "ContractUtilization",
        "EnrollmentCompletionFunnel",
        "MonthlyEngagementTrend",
        "ProgramFunnel",
        "ContentEngagementDepth",
        "ContractMonthlyEngagementTrend",
        "ContractContentEngagementDepth",
    }
    assert envelopes == {f"OrgAnalyticsResponse_{model}_" for model in row_models}
    assert row_models <= set(schemas)


def test_operation_ids_are_unique_and_stable(specs):
    """openapi-generator names a client method after its operationId, so a
    collision silently drops a method and a path-derived default renames every
    method whenever a route moves. Both are named explicitly in the routers."""
    operation_ids = [
        operation["operationId"]
        for spec in specs.values()
        for path_item in spec["paths"].values()
        for operation in path_item.values()
    ]
    assert len(operation_ids) == len(set(operation_ids))
    # The org and contract routers expose identically-named panels; the tag
    # prefix is what keeps them apart.
    assert "organizations_contract_utilization_retrieve" in operation_ids
    assert "contracts_contract_utilization_retrieve" in operation_ids
