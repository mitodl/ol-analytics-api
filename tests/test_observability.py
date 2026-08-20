from unittest.mock import MagicMock, patch

from ol_analytics_api.core.observability import logging as obs_logging
from ol_analytics_api.core.observability import telemetry
from ol_analytics_api.core.observability.sentry import _before_send, init_sentry


def _clear_endpoint_env(monkeypatch):
    """Clear every variable configure_opentelemetry() consults for an endpoint.

    All three matter: a value leaking in from the ambient environment would
    turn a "no endpoint configured" test into a configured one.
    """
    for name in (
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OPENTELEMETRY_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_configure_structlog_is_idempotent():
    obs_logging.reset_configuration()
    obs_logging.configure_structlog(debug=True)
    obs_logging.configure_structlog(debug=True)  # should not raise or reconfigure
    assert obs_logging._configured is True  # noqa: SLF001


def test_configure_opentelemetry_noop_without_endpoint_or_debug(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    telemetry.reset_configuration()
    provider = telemetry.configure_opentelemetry(
        service_name="test", service_version="0", environment="test", debug=False
    )
    assert provider is None


def test_configure_opentelemetry_configures_when_debug(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    telemetry.reset_configuration()
    provider = telemetry.configure_opentelemetry(
        service_name="test", service_version="0", environment="test", debug=True
    )
    assert provider is not None
    telemetry.reset_configuration()


def test_configure_opentelemetry_is_idempotent(monkeypatch):
    # OTel's global tracer provider can only ever be set once per process
    # (later calls are silently ignored, by design) — so this test can't
    # rely on the real global to observe main.py's own `_configured` guard.
    # It instead mocks trace.set_tracer_provider/get_tracer_provider to
    # verify configure_opentelemetry's second-call branch specifically:
    # `_configured` is True -> return the existing provider without
    # rebuilding one.
    _clear_endpoint_env(monkeypatch)
    telemetry.reset_configuration()
    with patch("ol_analytics_api.core.observability.telemetry.trace.set_tracer_provider"):
        first = telemetry.configure_opentelemetry(
            service_name="test", service_version="0", environment="test", debug=True
        )
    with patch(
        "ol_analytics_api.core.observability.telemetry.trace.get_tracer_provider",
        return_value=first,
    ):
        second = telemetry.configure_opentelemetry(
            service_name="test", service_version="0", environment="test", debug=True
        )
    assert second is first
    telemetry.reset_configuration()


def test_configure_opentelemetry_adds_console_exporter_when_enabled(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    monkeypatch.setenv("OPENTELEMETRY_CONSOLE_EXPORTER", "true")
    telemetry.reset_configuration()
    provider = telemetry.configure_opentelemetry(
        service_name="test", service_version="0", environment="test", debug=True
    )
    assert provider is not None
    telemetry.reset_configuration()


def _exporter_call(monkeypatch, env):
    _clear_endpoint_env(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    telemetry.reset_configuration()
    with patch("ol_analytics_api.core.observability.telemetry.OTLPSpanExporter") as exporter:
        provider = telemetry.configure_opentelemetry(
            service_name="test", service_version="0", environment="test", debug=False
        )
    assert provider is not None
    telemetry.reset_configuration()
    return exporter


def test_otel_exporter_otlp_endpoint_is_left_to_the_sdk(monkeypatch):
    # A base URL passed explicitly is used verbatim -- the exporter only appends
    # /v1/traces when it resolves the variable itself -- so forwarding it here
    # would POST every batch at the collector root for a 404 apiece.
    exporter = _exporter_call(
        monkeypatch, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example.test:4318"}
    )
    exporter.assert_called_once_with(endpoint=None)


def test_signal_specific_endpoint_enables_tracing_and_is_left_to_the_sdk(monkeypatch):
    # Previously ignored outright: only OTEL_EXPORTER_OTLP_ENDPOINT and
    # OPENTELEMETRY_ENDPOINT were consulted, so setting just this one disabled
    # tracing instead of enabling it.
    exporter = _exporter_call(
        monkeypatch,
        {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector.example.test:4318/v1/traces"},
    )
    exporter.assert_called_once_with(endpoint=None)


def test_opentelemetry_endpoint_setting_is_passed_verbatim(monkeypatch):
    # The library-specific variable is a full signal URL, so verbatim is what
    # the caller means. This is the path the deployed stacks still use.
    exporter = _exporter_call(
        monkeypatch,
        {"OPENTELEMETRY_ENDPOINT": "http://collector.example.test:4318/v1/traces"},
    )
    exporter.assert_called_once_with(endpoint="http://collector.example.test:4318/v1/traces")


def test_environment_endpoint_wins_over_the_setting(monkeypatch):
    exporter = _exporter_call(
        monkeypatch,
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example.test:4318",
            "OPENTELEMETRY_ENDPOINT": "http://legacy.example.test:4318/v1/traces",
        },
    )
    exporter.assert_called_once_with(endpoint=None)


def test_configure_opentelemetry_logs_but_does_not_raise_when_otlp_exporter_fails(
    monkeypatch, caplog
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.example.test:4318")
    telemetry.reset_configuration()
    with patch(
        "ol_analytics_api.core.observability.telemetry.OTLPSpanExporter",
        side_effect=RuntimeError("boom"),
    ):
        provider = telemetry.configure_opentelemetry(
            service_name="test", service_version="0", environment="test", debug=False
        )
    assert provider is not None
    assert "failed to configure OTLP exporter" in caplog.text
    telemetry.reset_configuration()


def test_auto_instrument_is_idempotent():
    telemetry.reset_configuration()
    with patch(
        "ol_analytics_api.core.observability.telemetry.importlib.metadata.entry_points",
        return_value=[],
    ) as entry_points:
        telemetry._auto_instrument()  # noqa: SLF001
        telemetry._auto_instrument()  # noqa: SLF001
    entry_points.assert_called_once()
    telemetry.reset_configuration()


def _fake_entry_point(name, *, raises=False):
    ep = MagicMock()
    ep.name = name
    if raises:
        ep.load.return_value.side_effect = RuntimeError("instrument failed")
    return ep


def test_auto_instrument_skips_configured_instrumentors_and_survives_a_failure(monkeypatch, caplog):
    telemetry.reset_configuration()
    monkeypatch.setenv("OL_ANALYTICS_API_OTEL_SKIP_INSTRUMENTORS", "skip-me")
    skip_ep = _fake_entry_point("skip-me")
    ok_ep = _fake_entry_point("instrument-me")
    failing_ep = _fake_entry_point("fails-to-instrument", raises=True)

    with patch(
        "ol_analytics_api.core.observability.telemetry.importlib.metadata.entry_points",
        return_value=[skip_ep, ok_ep, failing_ep],
    ):
        telemetry._auto_instrument()  # noqa: SLF001

    skip_ep.load.assert_not_called()
    ok_ep.load.assert_called_once()
    assert "Failed to auto-instrument" in caplog.text
    telemetry.reset_configuration()


def test_init_sentry_with_empty_dsn_does_not_raise():
    # sentry-sdk treats an empty DSN as "disabled" — this should be a safe no-op,
    # matching how every other service here calls init_sentry() unconditionally.
    init_sentry(
        dsn="",
        environment="test",
        version="0",
        log_level="ERROR",
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
    )


def test_init_sentry_clamps_out_of_range_sample_rates(caplog):
    init_sentry(
        dsn="",
        environment="test",
        version="0",
        log_level="ERROR",
        traces_sample_rate=5.0,
        profiles_sample_rate=-1.0,
    )
    assert "SENTRY_TRACES_SAMPLE_RATE" in caplog.text
    assert "SENTRY_PROFILES_SAMPLE_RATE" in caplog.text


def test_before_send_drops_shutdown_errors():
    event = {"event_id": "abc"}
    assert _before_send(event, {"exc_info": (SystemExit, SystemExit(), None)}) is None


def test_before_send_passes_through_other_errors():
    event = {"event_id": "abc"}
    hint = {"exc_info": (ValueError, ValueError("boom"), None)}
    assert _before_send(event, hint) is event


def test_before_send_passes_through_when_no_exc_info():
    event = {"event_id": "abc"}
    assert _before_send(event, {}) is event
