### Changed

- `b2b_dashboard`: `/admin/contract-health` turns `health_status` to `at_risk` per request from the view's `at_risk_since`, so the status no longer stays as it was at the view's last refresh. The response shape does not change. Requires a `mv_b2b_mit_admin_contract_health` with `at_risk_since`; before that the SELECT names a column the view does not have.
