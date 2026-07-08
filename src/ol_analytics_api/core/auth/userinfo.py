"""Read pre-validated auth claims forwarded by APISIX.

APISIX validates the Keycloak RS256 JWT for every request in front of this
service and forwards the decoded claims as a base64-encoded JSON blob in the
X-Userinfo header (the same pattern MITx Online and MIT Learn use via
mitol-django-authentication's decode_x_header()). This service trusts APISIX
and does not re-validate the token or fetch JWKS itself.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from fastapi import HTTPException, Request, status


def get_userinfo(request: Request) -> dict[str, Any]:
    raw = request.headers.get("X-Userinfo")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Userinfo header",
        )
    try:
        # validate=True: b64decode() silently discards non-alphabet
        # characters by default, which would let malformed input mask
        # itself as valid until the JSON parse (or worse, decode to
        # different bytes than intended). Reject it as unambiguously
        # invalid instead.
        decoded = base64.b64decode(raw, validate=True)
        userinfo: dict[str, Any] = json.loads(decoded)
    except (binascii.Error, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed X-Userinfo header",
        ) from exc
    return userinfo
