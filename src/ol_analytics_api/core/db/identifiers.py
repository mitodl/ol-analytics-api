"""Guard against unsafe SQL identifiers (schema/table names).

Query params are always passed positionally (see core/db/client.py) — but
schema names come from tenant config and get spliced directly into query
strings, since StarRocks doesn't support parameterizing identifiers. This
closes that gap: any tenant config field holding a schema/table name should
validate through here at settings-load time, so an f-string built from it is
provably safe rather than merely "not attacker-controlled today."
"""

from __future__ import annotations

import re

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_sql_identifier(value: str) -> str:
    if not _SAFE_IDENTIFIER.match(value):
        msg = f"Not a safe SQL identifier: {value!r}"
        raise ValueError(msg)
    return value
