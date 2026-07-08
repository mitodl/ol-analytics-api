from unittest.mock import MagicMock, patch

from opentelemetry.trace.span import format_span_id, format_trace_id

from ol_analytics_api.core.observability.processors import inject_k8s_context, inject_otel_context


def _fake_span(*, is_valid: bool, is_recording: bool, trace_id: int = 1, span_id: int = 2):
    ctx = MagicMock(is_valid=is_valid, trace_id=trace_id, span_id=span_id)
    span = MagicMock()
    span.get_span_context.return_value = ctx
    span.is_recording.return_value = is_recording
    return span


def test_inject_otel_context_short_circuits_when_already_present():
    event_dict = {"trace_id": "already-set", "span_id": "already-set", "event": "hi"}
    with patch("ol_analytics_api.core.observability.processors.trace.get_current_span") as get_span:
        result = inject_otel_context(None, "info", dict(event_dict))
    get_span.assert_not_called()
    assert result == event_dict


def test_inject_otel_context_no_trace_or_span_id_when_context_invalid():
    span = _fake_span(is_valid=False, is_recording=True)
    with patch(
        "ol_analytics_api.core.observability.processors.trace.get_current_span",
        return_value=span,
    ):
        result = inject_otel_context(None, "info", {"event": "hi"})
    assert "trace_id" not in result
    assert "span_id" not in result


def test_inject_otel_context_no_trace_or_span_id_when_not_recording():
    span = _fake_span(is_valid=True, is_recording=False)
    with patch(
        "ol_analytics_api.core.observability.processors.trace.get_current_span",
        return_value=span,
    ):
        result = inject_otel_context(None, "info", {"event": "hi"})
    assert "trace_id" not in result
    assert "span_id" not in result


def test_inject_otel_context_injects_when_valid_and_recording():
    span = _fake_span(is_valid=True, is_recording=True, trace_id=0x1234, span_id=0x5678)
    with patch(
        "ol_analytics_api.core.observability.processors.trace.get_current_span",
        return_value=span,
    ):
        result = inject_otel_context(None, "info", {"event": "hi"})
    assert result["trace_id"] == format_trace_id(0x1234)
    assert result["span_id"] == format_span_id(0x5678)


def test_inject_k8s_context_adds_fields_when_present():
    with patch(
        "ol_analytics_api.core.observability.processors._K8S_CONTEXT",
        {"pod_name": "pod-1", "namespace": "ns-1"},
    ):
        result = inject_k8s_context(None, "info", {"event": "hi"})
    assert result["pod_name"] == "pod-1"
    assert result["namespace"] == "ns-1"


def test_inject_k8s_context_noop_when_empty():
    with patch("ol_analytics_api.core.observability.processors._K8S_CONTEXT", {}):
        result = inject_k8s_context(None, "info", {"event": "hi"})
    assert result == {"event": "hi"}
