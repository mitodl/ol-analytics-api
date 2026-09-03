"""OpenTelemetry configuration, modeled on mitol-django-observability's
configure_opentelemetry() (ol-django:src/observability/mitol/observability/telemetry.py).

Activation matches the org's current convention (see learn-ai's settings.py):
OTel is enabled when OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
OTEL_EXPORTER_OTLP_ENDPOINT or OPENTELEMETRY_ENDPOINT is set, or when running
in debug mode — no separate "enabled" flag. The two standard variables are
resolved by the SDK rather than read here; only OPENTELEMETRY_ENDPOINT, which
is a full signal URL, is passed to the exporter. Traces only;
this service (like the Django plugin it's modeled on) doesn't run a separate
OTel Logs SDK pipeline — trace/span correlation happens by embedding
trace_id/span_id into the structured JSON logs (see logging.py +
observability/processors.py) that Grafana Alloy scrapes from stdout.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

log = logging.getLogger(__name__)

_configured = False
_instrumented = False
_tracing_enabled = False

# FastAPI is instrumented per-app by instrument_fastapi_app(), never through the
# entry-point sweep in _auto_instrument().
#
# FastAPIInstrumentor().instrument() works by rebinding the module attribute:
# `fastapi.FastAPI = _InstrumentedFastAPI`. Any module that already did
# `from fastapi import FastAPI` holds the ORIGINAL class and never sees the
# replacement -- and every module here does, because Python executes a module's
# imports before its module-level statements, so main.py and each tenant's
# app.py bind the name long before main.py calls configure_opentelemetry().
# Constructing the apps later (see main.Tenant) does not help: the call site
# resolves `FastAPI` from its own namespace, not from the fastapi module.
#
# The result is silent: instrument() succeeds, the sweep logs nothing, and the
# app is simply never instrumented -- no server spans, and no trace_id on any
# log line, while httpx tracing keeps working because that instrumentor patches
# methods on existing classes instead of rebinding a name.
#
# instrument_app() takes the app instance, so it is immune to all of this.
_MANUALLY_INSTRUMENTED = frozenset({"fastapi"})


def _get_resource(service_name: str, service_version: str, environment: str) -> Resource:
    # Resource.create() also merges in OTEL_RESOURCE_ATTRIBUTES / OTEL_SERVICE_NAME
    # from the environment per the OTel spec — this is how service.namespace
    # and service.instance.id (set via OTEL_RESOURCE_ATTRIBUTES at the K8s
    # deployment level) reach the resource without this service parsing them.
    attributes: dict[str, str] = {
        "service.name": service_name,
        "service.version": service_version,
        "deployment.environment": environment,
    }
    k8s_map = {
        "k8s.pod.name": os.environ.get("KUBERNETES_POD_NAME"),
        "k8s.namespace.name": os.environ.get("KUBERNETES_NAMESPACE"),
        "k8s.node.name": os.environ.get("KUBERNETES_NODE_NAME"),
    }
    attributes.update({k: v for k, v in k8s_map.items() if v})
    return Resource.create(attributes)


def _endpoint_from_env() -> str | None:
    """Return the OTLP traces endpoint the environment configures, if any.

    Used only to decide *whether* the environment configures an endpoint, never
    to build the exporter. The SDK resolves these two variables correctly on its
    own -- a signal-specific endpoint verbatim, a base endpoint with
    /v1/traces appended (see _append_trace_path in the http trace exporter) --
    and an endpoint passed explicitly to an exporter is always used verbatim,
    which defeats that. Handing OTEL_EXPORTER_OTLP_ENDPOINT to the exporter
    would turn a spec-correct base URL into POSTs at the collector root: a 404
    per batch, surfaced as nothing louder than a BatchSpanProcessor warning.

    Checked in the SDK's own precedence order, most specific first.
    """
    for env_var in ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        value = os.environ.get(env_var)
        if value:
            return value
    return None


def _auto_instrument() -> None:
    """Auto-discover and apply installed OTel instrumentors via entry points.

    Same mechanism mitol-django-observability uses — any installed
    `opentelemetry-instrumentation-*` package (fastapi, httpx, ...) registers
    itself under the `opentelemetry_instrumentor` entry-point group and is
    picked up here with no per-package wiring.

    OL_ANALYTICS_API_OTEL_SKIP_INSTRUMENTORS (comma-separated) excludes
    specific instrumentors; unset means instrument everything found.
    """
    global _instrumented  # noqa: PLW0603
    if _instrumented:
        return
    _instrumented = True

    skip = _MANUALLY_INSTRUMENTED | {
        name.strip()
        for name in os.environ.get("OL_ANALYTICS_API_OTEL_SKIP_INSTRUMENTORS", "").split(",")
        if name.strip()
    }

    for ep in importlib.metadata.entry_points(group="opentelemetry_instrumentor"):
        if ep.name in skip:
            log.debug("Skipping instrumentor: %s", ep.name)
            continue
        try:
            ep.load()().instrument()
            log.debug("Instrumented: %s", ep.name)
        except Exception:  # noqa: BLE001
            log.warning("Failed to auto-instrument %s", ep.name, exc_info=True)


def configure_opentelemetry(
    *, service_name: str, service_version: str, environment: str, debug: bool
) -> TracerProvider | None:
    """Configure OpenTelemetry tracing. Call once, at process startup, before
    the instrumented libraries are used: the entry-point sweep at the end
    patches them in place, so an httpx client constructed and called before
    this runs is untraced. FastAPI is deliberately excluded from that sweep and
    instrumented per-app by instrument_fastapi_app() — see
    _MANUALLY_INSTRUMENTED for why the sweep cannot work for it.

    Idempotent — safe to call multiple times.
    """
    global _configured, _tracing_enabled  # noqa: PLW0603
    if _configured:
        existing = trace.get_tracer_provider()
        return existing if isinstance(existing, TracerProvider) else None
    _configured = True

    env_endpoint = _endpoint_from_env()
    settings_endpoint = os.environ.get("OPENTELEMETRY_ENDPOINT")
    endpoint = env_endpoint or settings_endpoint
    if not endpoint and not debug:
        log.debug("OpenTelemetry: no endpoint configured and not debug, skipping")
        return None

    set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )

    provider = TracerProvider(resource=_get_resource(service_name, service_version, environment))
    trace.set_tracer_provider(provider)

    if debug and os.environ.get("OPENTELEMETRY_CONSOLE_EXPORTER", "").lower() == "true":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    if endpoint:
        try:
            # Pass an endpoint only when it came from OPENTELEMETRY_ENDPOINT, a
            # full signal URL where verbatim use is what the caller means. When
            # the environment configures it, hand the SDK nothing and let it
            # resolve -- see _endpoint_from_env.
            exporter_endpoint = None if env_endpoint else settings_endpoint
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=exporter_endpoint))
            )
            log.info(
                "OpenTelemetry: OTLP exporter configured from %s",
                "environment" if env_endpoint else "OPENTELEMETRY_ENDPOINT",
            )
        except Exception:  # noqa: BLE001
            log.warning("OpenTelemetry: failed to configure OTLP exporter", exc_info=True)

    _tracing_enabled = True
    _auto_instrument()
    return provider


def instrument_fastapi_app(app: FastAPI) -> None:
    """Instrument one FastAPI instance, bypassing the module-attribute patch.

    Takes the app object, so it does not care whether the caller's `FastAPI`
    name refers to the instrumented subclass — which is the failure the
    entry-point sweep hits (see _MANUALLY_INSTRUMENTED).

    Apply to the root app only. Starlette's Mount runs a tenant sub-app inside
    the root app's own ASGI call, so instrumenting a tenant as well would nest a
    redundant second span inside the root's on every tenant request —
    _start_internal_or_server_span downgrades it to INTERNAL because the root's
    SERVER span is already active, so it is duplicated work rather than a second
    server span. Same reasoning as add_request_logging() living on the root
    alone.

    A no-op when configure_opentelemetry() did not set tracing up, so a local
    run with no endpoint doesn't pay for middleware that records nothing.
    """
    if not _tracing_enabled:
        return
    FastAPIInstrumentor.instrument_app(app)


def reset_configuration() -> None:
    """Reset configuration state — test-only."""
    global _configured, _instrumented, _tracing_enabled  # noqa: PLW0603
    _configured = False
    _instrumented = False
    _tracing_enabled = False
