"""Fetch short-lived StarRocks credentials from Vault's database secrets engine.

Mirrors the pattern already used by this cluster's other StarRocks clients:
Dagster's StarRocksResource (ol-data-platform dg_projects/b2b_organization)
and `bin/starrocks-auth --mode vault` (ol-data-platform). The pod authenticates
to Vault via the Kubernetes auth method (its service account JWT), then reads
dynamic credentials from `{vault_starrocks_mount}/creds/{role}` — StarRocks
creates a scoped user for the lease and drops it on revocation.

Vault role/mount names are never hardcoded — see core/config.py.
"""

from __future__ import annotations

from pathlib import Path

import hvac

from ol_analytics_api.core.config import settings

_SERVICE_ACCOUNT_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")


def _read_service_account_token() -> str:
    return _SERVICE_ACCOUNT_TOKEN_PATH.read_text().strip()


def fetch_starrocks_credentials() -> tuple[str, str]:
    """Return (username, password) for the `app` StarRocks role via Vault."""
    client = hvac.Client(url=settings.vault_addr)
    client.auth.kubernetes.login(
        role=settings.vault_role,
        jwt=_read_service_account_token(),
        mount_point=settings.vault_k8s_mount,
    )
    response = client.read(
        f"{settings.vault_starrocks_mount}/creds/{settings.vault_starrocks_credential_role}"
    )
    if not isinstance(response, dict):
        msg = (
            f"Vault path not found: "
            f"{settings.vault_starrocks_mount}/creds/{settings.vault_starrocks_credential_role}"
        )
        raise RuntimeError(msg)  # noqa: TRY004 -- isinstance narrows a "not found" response
    data = response["data"]
    return data["username"], data["password"]
