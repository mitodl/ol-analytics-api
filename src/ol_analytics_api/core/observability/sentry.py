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


# Postgres appends a DETAIL line to constraint violations that echoes the whole
# offending row verbatim.  psycopg puts it in str(exc), so it ships inside the
# exception value, where no SDK privacy setting reaches it: send_default_pii
# governs user/cookie/header capture and max_request_body_size governs request
# bodies, and neither touches exception text.
_PG_DETAIL_MARKER = "\nDETAIL:"
_PG_DETAIL_REPLACEMENT = "\nDETAIL:  [scrubbed]"


def _scrub_pg_detail(text: str) -> str:
    """Truncate a Postgres error string at its DETAIL line.

    Keeps the primary message, which is what identifies the failure, and drops
    the row echo plus any HINT/CONTEXT Postgres appends after it.
    """
    index = text.find(_PG_DETAIL_MARKER)
    if index == -1:
        return text
    return text[:index] + _PG_DETAIL_REPLACEMENT


def _scrub_pg_details(event: Event) -> Event:
    """Apply _scrub_pg_detail everywhere an error string lands on the event.

    Covers exception values, the logentry message/formatted pair, and the legacy
    top-level message, so the scrub holds whether the event arrived as an
    uncaught exception or via logger.exception.
    """
    for entry in (event.get("exception") or {}).get("values") or []:
        value = entry.get("value")
        if isinstance(value, str):
            entry["value"] = _scrub_pg_detail(value)
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for key in ("formatted", "message"):
            value = logentry.get(key)
            if isinstance(value, str):
                logentry[key] = _scrub_pg_detail(value)
    top_message = event.get("message")
    if isinstance(top_message, str):
        event["message"] = _scrub_pg_detail(top_message)
    return event


def _before_send(event: Event, hint: Hint) -> Event | None:
    if "exc_info" in hint:
        _, exc_value, _ = hint["exc_info"]
        if isinstance(exc_value, _SHUTDOWN_ERRORS):
            return None
    return _scrub_pg_details(event)


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
        # Request bodies are NOT gated on send_default_pii: the SDK sets
        # request.data unconditionally (sentry_sdk/integrations/_wsgi_common.py
        # :123) and this is the only control (:61).  Left unset it defaults to
        # "medium", i.e. 10,000-byte bodies.  Set explicitly so the choice is
        # findable here rather than in a dependency's defaults.
        max_request_body_size="small",
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
