"""Sentry setup, modeled on mitxonline/learn-ai's main/sentry.py — same
before_send shutdown-error filter and init() shape, swapping the Django/Celery/
Redis integrations for Starlette/FastAPI equivalents.
"""

from __future__ import annotations

import logging
from typing import Any, cast

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
    """Truncate Postgres DETAIL lines everywhere in a Sentry event.

    The row echo reaches Sentry through more fields than the exception value:
    LoggingIntegration puts the log message in a breadcrumb
    (integrations/logging.py:311), logger.error("...: %s", exc) puts it in
    logentry.params (:274), and captured stack-frame locals carry it in
    frame vars because include_local_variables defaults to True
    (consts.py:1028, utils.py:616).  Walking the whole event covers those
    without enumerating them, and does not go stale when the SDK adds another.

    Safe to walk naively because client._prepare_event serializes the event
    before calling before_send (client.py:650 vs :658), so every leaf here is
    already a JSON primitive -- no live exception objects to coerce.
    """
    return cast("Event", _scrub_node(event))


def _scrub_node(node: Any) -> Any:  # noqa: ANN401 -- walks arbitrary JSON
    """Recurse through the serialized event, rewriting strings in place."""
    if isinstance(node, str):
        return _scrub_pg_detail(node)
    if isinstance(node, dict):
        for key, value in node.items():
            node[key] = _scrub_node(value)
        return node
    if isinstance(node, list):
        node[:] = [_scrub_node(item) for item in node]
        return node
    if isinstance(node, tuple):
        return tuple(_scrub_node(item) for item in node)
    return node


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
