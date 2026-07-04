from ol_analytics_api.core.observability import logging as obs_logging
from ol_analytics_api.core.observability import telemetry
from ol_analytics_api.core.observability.sentry import init_sentry


def test_configure_structlog_is_idempotent():
    obs_logging.reset_configuration()
    obs_logging.configure_structlog(debug=True)
    obs_logging.configure_structlog(debug=True)  # should not raise or reconfigure
    assert obs_logging._configured is True  # noqa: SLF001


def test_configure_opentelemetry_noop_without_endpoint_or_debug(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENTELEMETRY_ENDPOINT", raising=False)
    telemetry.reset_configuration()
    provider = telemetry.configure_opentelemetry(
        service_name="test", service_version="0", environment="test", debug=False
    )
    assert provider is None


def test_configure_opentelemetry_configures_when_debug(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENTELEMETRY_ENDPOINT", raising=False)
    telemetry.reset_configuration()
    provider = telemetry.configure_opentelemetry(
        service_name="test", service_version="0", environment="test", debug=True
    )
    assert provider is not None
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
