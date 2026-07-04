"""Regression coverage for settings env-var prefixes.

pydantic-settings silently ignores unrecognized env vars and falls back to
field defaults — an accidental prefix rename (like the one this diff made,
OL_ANALYTICS_API_ -> OL_ANALYTICS_API_B2B_DASHBOARD_ for tenant policy
settings) would otherwise ship with no error, just a silently-reverted
default. These tests pin the actual prefixes so a future rename fails CI
instead of a deployment.
"""

from __future__ import annotations

from ol_analytics_api.core.config import CoreSettings
from ol_analytics_api.tenants.b2b_dashboard.config import B2BDashboardSettings


def test_b2b_dashboard_settings_load_from_their_actual_env_prefix(monkeypatch):
    monkeypatch.setenv("OL_ANALYTICS_API_B2B_DASHBOARD_ANONYMIZATION_FLOOR", "9")
    monkeypatch.setenv(
        "OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_API_BASE_URL", "https://example.test"
    )
    settings = B2BDashboardSettings()
    assert settings.anonymization_floor == 9
    assert settings.mitxonline_api_base_url == "https://example.test"


def test_b2b_dashboard_settings_ignore_the_old_core_prefix(monkeypatch):
    # Pre-multi-tenant-refactor these fields lived under OL_ANALYTICS_API_ —
    # confirm that prefix is now inert (silently ignored) rather than
    # accidentally still matching, which would mask a real config bug.
    monkeypatch.setenv("OL_ANALYTICS_API_ANONYMIZATION_FLOOR", "9")
    settings = B2BDashboardSettings()
    assert settings.anonymization_floor == 5  # unchanged default


def test_core_settings_load_from_ol_analytics_api_prefix(monkeypatch):
    monkeypatch.setenv("OL_ANALYTICS_API_STARROCKS_HOST", "starrocks.example.test")
    settings = CoreSettings()
    assert settings.starrocks_host == "starrocks.example.test"


def test_core_settings_observability_fields_use_bare_env_names(monkeypatch):
    # These bypass env_prefix entirely (validation_alias) to match the
    # cross-service convention shared with mitxonline/mit-learn/learn-ai.
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("SENTRY_DSN", "https://example.test/1")
    settings = CoreSettings()
    assert settings.log_level == "DEBUG"
    assert settings.sentry_dsn == "https://example.test/1"
