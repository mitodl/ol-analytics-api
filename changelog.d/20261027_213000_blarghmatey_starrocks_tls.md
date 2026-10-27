### Added

- `OL_ANALYTICS_API_STARROCKS_TLS_SERVER_NAME` turns on TLS to the StarRocks FE and verifies its certificate against that name instead of the in-cluster host the pool dials. Unset, the pool connects in plaintext as before.
