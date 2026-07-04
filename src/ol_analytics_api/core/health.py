"""Tiered health checks matching this org's K8s probe convention exactly —
see ol-infrastructure's shared OLApplicationK8s component
(src/ol_infrastructure/components/services/k8s.py), which hardcodes
GET /health/{startup,readiness,liveness}/ as the probe paths for every
service it deploys.

- liveness: is the process alive? No dependency checks — a slow/degraded
  StarRocks shouldn't cause Kubernetes to kill and restart health pods.
- readiness: can this pod serve traffic right now? Checks the shared
  StarRocks pool, plus any tenant-contributed checks registered via
  register_readiness_check (e.g. a future tenant that depends on a
  different upstream service can add its own check without touching this
  module).
- startup: has initialization (the lifespan-started StarRocks pool)
  completed? Same checks as readiness — startup only exists as a separate
  probe so Kubernetes allows more time before the first readiness/liveness
  check without loosening their steady-state timing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, status

from ol_analytics_api.core.db.client import starrocks_pool

router = APIRouter(prefix="/health", tags=["infra"])

ReadinessCheck = Callable[[], Awaitable[None]]
_readiness_checks: list[ReadinessCheck] = []


def register_readiness_check(check: ReadinessCheck) -> None:
    """Register an additional async check that must succeed (raise nothing)
    for /health/readiness/ and /health/startup/ to report ready. A tenant
    calls this for a dependency only it needs (e.g. an upstream API) —
    other tenants and core infra are unaffected."""
    _readiness_checks.append(check)


async def _check_ready() -> None:
    await starrocks_pool.ping()
    for check in _readiness_checks:
        await check()


@router.get("/liveness/")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readiness/")
async def readiness() -> dict[str, str]:
    try:
        await _check_ready()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return {"status": "ready"}


@router.get("/startup/")
async def startup() -> dict[str, str]:
    try:
        await _check_ready()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return {"status": "started"}
