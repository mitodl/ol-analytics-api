"""A stand-in for the b2b_dashboard membership probes (tenants/b2b_dashboard/auth.py).

The gates send up to two ``SELECT 1`` probes per request and act on which of
them matched, so a fake that answers every probe the same way cannot tell
"another organization's contract" from "not loaded yet". This answers each one
against the rows it is given, the way StarRocks would.
"""

import re

_PREDICATE = re.compile(r"(\w+) = %s")


def is_membership_probe(query: str) -> bool:
    return query.startswith("SELECT 1 ")


def answer_membership_probe(
    query: str, params: tuple, loaded: tuple[tuple[str, int], ...]
) -> list[dict]:
    """Answer one probe over ``loaded``, the ``(sso_organization_id,
    contract_id)`` rows of the membership view."""
    wanted = dict(zip(_PREDICATE.findall(query), params, strict=True))
    rows = ({"sso_organization_id": org, "contract_id": contract} for org, contract in loaded)
    matched = any(all(row[column] == value for column, value in wanted.items()) for row in rows)
    return [{"1": 1}] if matched else []
