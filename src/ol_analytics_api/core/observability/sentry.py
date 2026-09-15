"""Sentry setup, modeled on mitxonline/learn-ai's main/sentry.py — same
before_send shutdown-error filter and init() shape, swapping the Django/Celery/
Redis integrations for Starlette/FastAPI equivalents.
"""

from __future__ import annotations

import logging

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.types import Event, Hint

log = logging.getLogger(__name__)

# These occur when a shutdown is happening (usually caused by a SIGTERM) —
# expected, not worth reporting to Sentry.
_SHUTDOWN_ERRORS = (SystemExit,)


def _before_send(event: Event, hint: Hint) -> Event | None:
    if "exc_info" in hint:
        _, exc_value, _ = hint["exc_info"]
        if isinstance(exc_value, _SHUTDOWN_ERRORS):
            return None
    return event


def init_sentry(  # noqa: PLR0913 -- matches the org's established init_sentry() shape
    *,
    dsn: str,
    environment: str,
    version: str,
    log_level: str,
    traces_sample_rate: float,
    profiles_sample_rate: float,
) -> None:
    if not 0 <= traces_sample_rate <= 1:
        log.error("SENTRY_TRACES_SAMPLE_RATE should be 0 <= x <= 1, defaulting to 0")
        traces_sample_rate = 0

    if not 0 <= profiles_sample_rate <= 1:
        log.error("SENTRY_PROFILES_SAMPLE_RATE should be 0 <= x <= 1, defaulting to 0")
        profiles_sample_rate = 0

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=version,
        before_send=_before_send,
        # This service serves aggregated-only analytics (no individual
        # learner PII) — default to not sending request/user PII to Sentry.
        send_default_pii=False,
        traces_sample_rate=traces_sample_rate,
        profiles_sample_rate=profiles_sample_rate,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            # LoggingIntegration has two independent thresholds: `level`
            # (breadcrumb capture, kept at the library default of INFO so
            # events have useful context leading up to them) and
            # `event_level` (creates a Sentry issue) — SENTRY_LOG_LEVEL
            # is meant to control the latter, not the former.
            LoggingIntegration(event_level=getattr(logging, log_level.upper(), logging.ERROR)),
        ],
    )
