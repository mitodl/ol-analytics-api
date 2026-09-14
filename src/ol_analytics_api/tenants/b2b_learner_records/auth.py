"""Machine-to-machine access for the learner-records tenant.

There is no user in this flow. MIT issues one Keycloak client-credentials
client per contracted integration, and that client carries the organization
UUIDs its contract covers as a hardcoded claim
(docs/b2b-learner-records-provider-authorization.md). APISIX validates the
token and forwards its claims in X-Userinfo, so authorization here is a set
membership test: no call to MITx Online, no grant store.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Annotated, Any

import structlog
from fastapi import Depends, HTTPException, Security, status
from fastapi.openapi.models import OAuthFlowClientCredentials, OAuthFlows
from fastapi.security import OAuth2

from ol_analytics_api.core.auth.userinfo import get_userinfo
from ol_analytics_api.tenants.b2b_learner_records.config import settings

ORGANIZATIONS_CLAIM = "learner_records_organizations"
READ_SCOPE = "learner-records:read"

# Declares the contract's security scheme in this tenant's OpenAPI, so generated
# clients obtain and send a token. It enforces nothing: auto_error=False, and
# the checks below read the claims APISIX forwards after validating the token.
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

UserInfo = Annotated[dict[str, Any], Depends(get_userinfo)]


def _granted_organizations(userinfo: dict[str, Any]) -> set[uuid.UUID]:
    # Keycloak emits the claim as a JSON array only when the mapper's claim
    # type is JSON. Any other shape grants nothing rather than being guessed at.
    claim = userinfo.get(ORGANIZATIONS_CLAIM)
    if not isinstance(claim, list):
        return set()
    granted = set()
    for value in claim:
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            granted.add(uuid.UUID(value))
    return granted


def require_organization_grant(
    organization_id: uuid.UUID,
    userinfo: UserInfo,
    _token: Annotated[str | None, Security(oauth2_client_credentials, scopes=[READ_SCOPE])],
) -> None:
    scopes = userinfo.get("scope")
    if not isinstance(scopes, str) or READ_SCOPE not in scopes.split():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Token lacks the {READ_SCOPE} scope",
        )
    if organization_id not in _granted_organizations(userinfo):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NO_GRANT_DETAIL)
    # Every granted read discloses identifiable learner records, so record which
    # client read which organization. The access log has the path but not the client.
    log.info(
        "learner_records_access",
        client_id=userinfo.get("azp") or userinfo.get("client_id"),
        organization_id=str(organization_id),
    )
