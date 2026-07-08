from unittest.mock import MagicMock, patch

import pytest

from ol_analytics_api.core.db.vault_credentials import (
    _read_service_account_token,
    fetch_starrocks_credentials,
)


def test_read_service_account_token_strips_whitespace(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("a-jwt-token\n")
    with patch(
        "ol_analytics_api.core.db.vault_credentials._SERVICE_ACCOUNT_TOKEN_PATH", token_path
    ):
        assert _read_service_account_token() == "a-jwt-token"


def _fake_hvac_client(read_response):
    client = MagicMock()
    client.auth.kubernetes.login = MagicMock()
    client.read = MagicMock(return_value=read_response)
    return client


def test_fetch_starrocks_credentials_returns_user_password_and_lease():
    read_response = {
        "data": {"username": "vault-user", "password": "vault-password"},
        "lease_duration": 3600,
    }
    with (
        patch(
            "ol_analytics_api.core.db.vault_credentials.hvac.Client",
            return_value=_fake_hvac_client(read_response),
        ),
        patch(
            "ol_analytics_api.core.db.vault_credentials._read_service_account_token",
            return_value="fake-jwt",
        ),
    ):
        result = fetch_starrocks_credentials()

    assert result == ("vault-user", "vault-password", 3600)


def test_fetch_starrocks_credentials_raises_when_path_not_found():
    # hvac's client.read() returns None for a 404 on the secret path (no
    # dynamic role/mount configured) rather than raising — must fail loudly
    # here instead of the caller's response["data"] raising a bare
    # TypeError/KeyError.
    with (
        patch(
            "ol_analytics_api.core.db.vault_credentials.hvac.Client",
            return_value=_fake_hvac_client(None),
        ),
        patch(
            "ol_analytics_api.core.db.vault_credentials._read_service_account_token",
            return_value="fake-jwt",
        ),
        pytest.raises(RuntimeError, match="Vault path not found"),
    ):
        fetch_starrocks_credentials()
