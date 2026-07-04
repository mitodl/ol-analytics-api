"""OpenTelemetry configuration, modeled on mitol-django-observability's
configure_opentelemetry() (ol-django:src/observability/mitol/observability/telemetry.py).

Activation matches the org's current convention (see learn-ai's settings.py):
OTel is enabled when OTEL_EXPORTER_OTLP_ENDPOINT or OPENTELEMETRY_ENDPOINT is
set, or when running in debug mode — no separate "enabled" flag. Traces only;
this service (like the Django plugin it's modeled on) doesn't run a separate
OTel Logs SDK pipeline — trace/span correlation happens by embedding
trace_id/span_id into the structured JSON logs (see logging.py +
observability/processors.py) that Grafana Alloy scrapes from stdout.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os

from opentelemetry import trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

log = logging.getLogger(__name__)

_configured = False
_instrumented = False


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

    skip = {
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
    any FastAPI() instances (including mounted tenant sub-apps) are
    constructed — FastAPI auto-instrumentation patches FastAPI.__init__, so
    apps created before this runs won't be instrumented.

    Idempotent — safe to call multiple times.
    """
    global _configured  # noqa: PLW0603
    if _configured:
        existing = trace.get_tracer_provider()
        return existing if isinstance(existing, TracerProvider) else None
    _configured = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(
        "OPENTELEMETRY_ENDPOINT"
    )
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
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            log.info("OpenTelemetry: OTLP exporter configured to %s", endpoint)
        except Exception:  # noqa: BLE001
            log.warning("OpenTelemetry: failed to configure OTLP exporter", exc_info=True)

    _auto_instrument()
    return provider


def reset_configuration() -> None:
    """Reset configuration state — test-only."""
    global _configured, _instrumented  # noqa: PLW0603
    _configured = False
    _instrumented = False
