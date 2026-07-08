from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ol_analytics_api.core.observability.middleware import _log_requests


def _fake_request(path: str = "/boom") -> SimpleNamespace:
    return SimpleNamespace(method="GET", url=SimpleNamespace(path=path))


async def test_failed_request_is_still_logged():
    # Regression test: if call_next() raises (an unhandled exception, or a
    # cancelled/disconnected request), the request must still produce a log
    # line — previously the log.info() call after `await call_next(...)`
    # was simply never reached, so failed requests vanished from the access
    # log entirely.
    async def _raising_call_next(_request):
        msg = "boom"
        raise RuntimeError(msg)

    with (
        patch("ol_analytics_api.core.observability.middleware.log.exception") as log_exception,
        pytest.raises(RuntimeError, match="boom"),
    ):
        await _log_requests(_fake_request(), _raising_call_next)

    assert log_exception.call_count == 1
    _, kwargs = log_exception.call_args
    assert kwargs["method"] == "GET"
    assert kwargs["path"] == "/boom"


async def test_successful_request_logs_status_and_duration():
    response = SimpleNamespace(status_code=200)

    async def _call_next(_request):
        return response

    with patch("ol_analytics_api.core.observability.middleware.log.info") as log_info:
        result = await _log_requests(_fake_request("/ok"), _call_next)

    assert result is response
    _, kwargs = log_info.call_args
    assert kwargs["status_code"] == 200
    assert kwargs["path"] == "/ok"
