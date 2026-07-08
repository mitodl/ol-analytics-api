import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ol_analytics_api.core.auth.userinfo import get_userinfo


def _request(headers: dict) -> SimpleNamespace:
    return SimpleNamespace(headers=headers)


def test_missing_header_is_401():
    with pytest.raises(HTTPException) as exc_info:
        get_userinfo(_request({}))
    assert exc_info.value.status_code == 401
    assert "Missing" in exc_info.value.detail


def test_valid_header_decodes_to_dict():
    payload = {"sub": "kc-uuid-1", "organization": {"org-a": {"id": "o1"}}}
    header = base64.b64encode(json.dumps(payload).encode()).decode()
    assert get_userinfo(_request({"X-Userinfo": header})) == payload


def test_non_base64_header_is_401_malformed():
    # validate=True on b64decode rejects this outright, rather than silently
    # discarding the invalid characters and decoding something unintended.
    with pytest.raises(HTTPException) as exc_info:
        get_userinfo(_request({"X-Userinfo": "not-valid-base64!!!"}))
    assert exc_info.value.status_code == 401
    assert "Malformed" in exc_info.value.detail


def test_valid_base64_but_invalid_json_is_401_malformed():
    header = base64.b64encode(b"not json").decode()
    with pytest.raises(HTTPException) as exc_info:
        get_userinfo(_request({"X-Userinfo": header}))
    assert exc_info.value.status_code == 401
    assert "Malformed" in exc_info.value.detail
