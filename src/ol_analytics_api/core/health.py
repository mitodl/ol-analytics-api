"""Tiered health checks matching this org's K8s probe convention exactly —
see ol-infrastructure's shared OLApplicationK8s component
(src/ol_infrastructure/components/services/k8s.py), which hardcodes
GET /health/{startup,readiness,liveness}/ as the probe paths for every
service it deploys.

- liveness: is the process alive? No dependency checks — a slow/degraded
  StarRocks shouldn't cause Kubernetes to kill and restart health pods.
- readiness: can this pod serve traffic right now? Checks ONLY shared infra
  (the StarRocks pool every tenant reads through). Deliberately does NOT run
  any tenant-specific upstream check: this pod hosts multiple independent
  tenant sub-apps (see main.py), and K8s probes this single path to decide
  whether to keep the *whole* pod in rotation. Coupling one tenant's private
  upstream (e.g. b2b_dashboard's MITx Online round-trip) into it would let
  that tenant's outage pull the pod and take down every OTHER tenant with it
  — the opposite of the tenant-isolation guarantee. Tenant-specific
  dependency health is instead exposed, per tenant, at
  /health/readiness/{tenant}/ (see below) for monitoring and per-tenant
  routing decisions, where a degraded tenant can't deny service to the rest.
- startup: has initialization (the lifespan-started StarRocks pool)
  completed? Same shared-infra check as readiness — startup only exists as a
  separate probe so Kubernetes allows more time before the first
  readiness/liveness check without loosening their steady-state timing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, status

from ol_analytics_api.core.db.client import starrocks_pool

router = APIRouter(prefix="/health", tags=["infra"])

ReadinessCheck = Callable[[], Awaitable[None]]

# Tenant-scoped readiness checks, keyed by tenant name. A tenant registers a
# check for an upstream only it depends on; that check surfaces at the
# tenant's own /health/readiness/{tenant}/ sub-path and NEVER on the shared
# /health/readiness/ that K8s probes — so one tenant's upstream outage can't
# fail the pod for the others.
_tenant_readiness_checks: dict[str, list[ReadinessCheck]] = {}


def register_readiness_check(tenant: str, check: ReadinessCheck | None = None) -> None:
    """Register an async dependency check scoped to a single tenant. The
    check must succeed (raise nothing) for that tenant's
    /health/readiness/{tenant}/ sub-path to report ready. It has NO effect on
    the shared /health/readiness/ and /health/startup/ probes K8s uses to
    decide the whole pod's rotation — so a tenant's private upstream going
    down degrades only that tenant's sub-path, never the pod.

    `check` is optional so a tenant with no custom upstream dependencies can
    still register its existence — without this, /health/readiness/{tenant}/
    would 404 for a perfectly healthy tenant that only relies on shared
    infra, instead of reporting 200."""
    checks = _tenant_readiness_checks.setdefault(tenant, [])
    if check is not None:
        checks.append(check)


async def _check_shared_infra() -> None:
    """The dependency check gating the whole pod: only infra shared by every
    tenant. Today that's the StarRocks pool each tenant reads through."""
    await starrocks_pool.ping()


async def _shared_ready_response(status_label: str) -> dict[str, str]:
    try:
        await _check_shared_infra()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return {"status": status_label}


@router.get("/liveness/")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readiness/")
async def readiness() -> dict[str, str]:
    return await _shared_ready_response("ready")


@router.get("/startup/")
async def startup() -> dict[str, str]:
    return await _shared_ready_response("started")


@router.get("/readiness/{tenant}/")
async def tenant_readiness(tenant: str) -> dict[str, str]:
    """Per-tenant readiness: shared infra AND every check that tenant
    registered. Not a K8s probe target — this exists so monitoring (and a
    future per-tenant router that can route around a degraded tenant) can see
    a single tenant's upstream health without that health being able to pull
    the whole pod."""
    checks = _tenant_readiness_checks.get(tenant)
    if checks is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant {tenant!r} is not registered",
        )
    try:
        await _check_shared_infra()
        for check in checks:
            await check()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return {"status": "ready"}
