"""A stand-in for the b2b_dashboard membership probes (tenants/b2b_dashboard/auth.py).

The gates send up to two ``SELECT 1`` probes per request and act on which of
them matched, so a fake that answers every probe the same way cannot tell
"another organization's contract" from "not loaded yet". This answers each one
against the rows it is given, the way StarRocks would, NULLs included.
"""

import re

_PREDICATE = re.compile(r"(\w+) (=|<>) %s")


def is_membership_probe(query: str) -> bool:
    return query.startswith("SELECT 1 ")


def _holds(actual: object, operator: str, value: object) -> bool:
    # SQL: a NULL column satisfies neither `=` nor `<>`.
    if actual is None:
        return False
    return actual == value if operator == "=" else actual != value


def answer_membership_probe(
    query: str, params: tuple, loaded: tuple[tuple[str | None, int], ...]
) -> list[dict]:
    """Answer one probe over ``loaded``, the ``(sso_organization_id,
    contract_id)`` rows of the membership view."""
    predicates = [
        (column, operator, value)
        for (column, operator), value in zip(_PREDICATE.findall(query), params, strict=True)
    ]
    rows = ({"sso_organization_id": org, "contract_id": contract} for org, contract in loaded)
    matched = any(
        all(_holds(row[column], operator, value) for column, operator, value in predicates)
        for row in rows
    )
    return [{"1": 1}] if matched else []
