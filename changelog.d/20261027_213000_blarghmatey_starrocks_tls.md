### Added

- `OL_ANALYTICS_API_STARROCKS_TLS_SERVER_NAME` turns on TLS to the StarRocks FE and verifies its certificate against that name instead of the in-cluster host the pool dials. A pooled connection that comes up without TLS is refused with `PlaintextConnectionError` before any query runs on it. Unset, the pool connects in plaintext as before.
