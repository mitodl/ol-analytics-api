"""Structlog configuration, modeled on mitol-django-observability's
configure_structlog() (ol-django:src/observability/mitol/observability/logging.py)
but routing Granian's loggers instead of Django's — this service has no
Django dependency, so that plugin can't be imported directly, but the same
processor chain, JSON-in-prod / console-in-dev split, and structured
exception rendering are reproduced here so log shape matches every other
service shipping to the same Loki/Grafana Alloy pipeline.
"""

from __future__ import annotations

import logging
import logging.config
from typing import Any

import structlog
from structlog.tracebacks import ExceptionDictTransformer

from ol_analytics_api.core.observability.processors import (
    inject_k8s_context,
    inject_otel_context,
)

_configured = False

# show_locals=False keeps local variable values out of log output (security +
# size); max_frames=20 keeps payloads reasonable. A structured `exception`
# dict (rather than a flat traceback string) lets Loki/Grafana index
# individual fields (exc_type, exc_value, frames[].filename, ...).
_EXCEPTION_RENDERER = structlog.processors.ExceptionRenderer(
    ExceptionDictTransformer(show_locals=False, max_frames=20)
)


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
        inject_otel_context,
        inject_k8s_context,
        structlog.processors.StackInfoRenderer(),
    ]


def configure_structlog(*, debug: bool, log_level: str = "INFO", force: bool = False) -> None:
    """Configure structlog and route stdlib/Granian logging through it.

    Idempotent — safe to call multiple times (e.g. under `--reload`).
    """
    global _configured  # noqa: PLW0603
    if _configured and not force:
        return
    _configured = True

    shared = _shared_processors()

    if debug:
        # ConsoleRenderer handles exc_info tuples natively.
        exc_processor: Any = structlog.dev.set_exc_info
        renderer: Any = structlog.dev.ConsoleRenderer(colors=True)
    else:
        # _EXCEPTION_RENDERER converts exc_info into a structured `exception`
        # dict before JSONRenderer serializes the event — needed for BOTH the
        # structlog pipeline and foreign stdlib records (Granian's own
        # loggers), so it also appears in formatter_processors below.
        exc_processor = _EXCEPTION_RENDERER
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared, exc_processor, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter_processors: list[Any] = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
    if not debug:
        formatter_processors.append(_EXCEPTION_RENDERER)
    formatter_processors.append(renderer)

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=formatter_processors,
        foreign_pre_chain=shared,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {"console": {"()": lambda: handler}},
            "loggers": {
                # Granian's own loggers are this service's equivalent of
                # Django's "django"/"django.request" — route them through the
                # same structlog pipeline instead of Granian's default plain
                # format. Granian configures logging itself (granian/log.py's
                # configure_logging(), with propagate=False and its own plain
                # StreamHandler) before a worker imports the app, so this
                # dictConfig runs second and wins. The names are Granian's,
                # not guessable: the server logger is "_granian" (matching the
                # Rust-side logger name), not "granian".
                "_granian": {"handlers": ["console"], "level": log_level, "propagate": False},
                # Granian's access log is disabled by default and the
                # structured middleware in ./middleware.py replaces it, so
                # this normally emits nothing. It is routed anyway so that
                # turning --access-log on yields JSON rather than a second,
                # differently-formatted line. Note this only *formats* the
                # access log; unlike uvicorn (where re-attaching a handler
                # here silently defeated --no-access-log, which works by
                # stripping handlers), Granian gates access logging when
                # building its request callback, so a handler here cannot
                # re-enable it.
                "granian.access": {
                    "handlers": ["console"],
                    "level": log_level,
                    "propagate": False,
                },
            },
            "root": {"handlers": ["console"], "level": log_level},
        }
    )


def reset_configuration() -> None:
    """Reset configuration state — test-only, mirrors the Django plugin's helper."""
    global _configured  # noqa: PLW0603
    _configured = False
