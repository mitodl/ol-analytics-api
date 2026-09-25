"""Machine-to-machine access for the learner-records tenant.

There is no user in this flow. MIT issues one Keycloak client-credentials
client per contracted integration, and that client carries the organization
UUIDs its contract covers as a hardcoded claim
(docs/b2b-learner-records-provider-authorization.md). Authorization is
therefore a set membership test over the token's own claims: no call to MITx
Online, no grant store.

Unlike b2b_dashboard, this tenant does not read X-Userinfo. It verifies the
bearer token's signature itself (token.py), because a header rebuilt by
APISIX only proves anything about traffic that went through APISIX, and
these records name individual learners.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Annotated, Any

import structlog
from fastapi import Depends, Security, status
from fastapi.openapi.models import OAuthFlowClientCredentials, OAuthFlows
from fastapi.security import OAuth2

from ol_analytics_api.tenants.b2b_learner_records.config import settings
from ol_analytics_api.tenants.b2b_learner_records.errors import ApiError, ErrorCode
from ol_analytics_api.tenants.b2b_learner_records.token import (
    CONTRACT_END_DATE_CLAIM,
    verified_claims,
)

ORGANIZATIONS_CLAIM = "learner_records_organizations"
READ_SCOPE = "learner-records:read"

# Declares the contract's security scheme in this tenant's OpenAPI, so generated
# clients obtain and send a token. It enforces nothing on its own
# (auto_error=False); the token is verified by the TokenClaims dependency and
# the checks below run against the verified payload.
oauth2_client_credentials = OAuth2(
    flows=OAuthFlows(
        clientCredentials=OAuthFlowClientCredentials(
            tokenUrl=settings.token_url,
            scopes={READ_SCOPE: "Read records, identity fields included."},
        )
    ),
    scheme_name="oauth2ClientCredentials",
    auto_error=False,
)

# The same refusal whether the organization is ungranted or doesn't exist, so
# a client can't use this endpoint to enumerate organizations.
NO_GRANT_DETAIL = "No grant for the requested organization"

log = structlog.get_logger(__name__)

TokenClaims = Annotated[dict[str, Any], Depends(verified_claims)]


def _granted_organizations(claims: dict[str, Any]) -> set[uuid.UUID]:
    # Keycloak emits the claim as a JSON array only when the mapper's claim
    # type is JSON. Any other shape grants nothing rather than being guessed at.
    claim = claims.get(ORGANIZATIONS_CLAIM)
    if not isinstance(claim, list):
        return set()
    granted = set()
    for value in claim:
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            granted.add(uuid.UUID(value))
    return granted


def require_organization_grant(
    organization_id: uuid.UUID,
    claims: TokenClaims,
    _token: Annotated[str | None, Security(oauth2_client_credentials, scopes=[READ_SCOPE])],
) -> None:
    scopes = claims.get("scope")
    if not isinstance(scopes, str) or READ_SCOPE not in scopes.split():
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code=ErrorCode.MISSING_SCOPE,
            detail=f"Token lacks the {READ_SCOPE} scope",
        )
    if organization_id not in _granted_organizations(claims):
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code=ErrorCode.NO_ORGANIZATION_ACCESS,
            detail=NO_GRANT_DETAIL,
        )
    # Every granted read discloses identifiable learner records, so record which
    # client read which organization. The access log has the path but not the client.
    # The end date rides along so an alert can warn MIT ahead of a lapse; token.py
    # refuses the credential once it passes.
    log.info(
        "learner_records_access",
        client_id=claims.get("azp") or claims.get("client_id"),
        organization_id=str(organization_id),
        contract_end_date=claims.get(CONTRACT_END_DATE_CLAIM),
    )
