"""Compose one OpenAPI document per mounted tenant.

Each tenant is an independent ``FastAPI()`` mounted under the root app (see
main.py), so it owns its own ``/openapi.json`` and the root app's schema does
not contain a single tenant path. Dumping ``app.openapi()`` therefore yields
the health endpoints and nothing a client would generate against — the schema
a consumer needs has to come from the sub-app.

Two things are fixed up on the way out, and both exist because the document is
written for a *client generator* rather than for the sub-app's own ``/docs``:

- **Paths are re-prefixed with the mount path.** A sub-app describes its routes
  relative to its own root ("/organizations/{id}/..."), because Starlette's
  Mount strips the prefix before the sub-app ever sees the request. A generated
  client configured with the service host as its base URL would then request
  the wrong URL. Prefixing here keeps the generated client's paths identical to
  the absolute paths the service actually serves, which is also what
  mit-learn's hand-written client hardcodes today.

- **The document version is pinned, not read from the package.** Sourcing it
  from the package's CalVer would rewrite every spec on every release, and the
  committed spec exists to make *interface* changes visible in review. A
  release that changes no route should produce no diff here.

``tenant_specs()`` builds the documents and ``render()`` serializes one exactly
as the committed file holds it. Both live here rather than in
``bin/generate-openapi-spec`` so the drift test compares against the same
serializer that wrote the file, instead of a second one that can disagree.

This module is a build-time tool. Nothing the server imports reaches it, which
is why PyYAML and cyclopts are dev dependencies: the running service never
needs either.
"""

from __future__ import annotations

from typing import Any

import yaml

from ol_analytics_api.main import TENANTS, create_app

# Pinned rather than derived from the package version — see the module
# docstring. Bump deliberately when a tenant's interface breaks.
SPEC_VERSION = "0.0.1"


def _prefix_paths(paths: dict[str, Any], mount_path: str) -> dict[str, Any]:
    return {f"{mount_path}{path}": item for path, item in paths.items()}


def tenant_specs() -> dict[str, dict[str, Any]]:
    """Returns ``{tenant name: OpenAPI document}`` for every mounted tenant.

    Builds the apps through the same ``create_app()`` the server runs, so a
    route the registry does not actually mount cannot reach a published spec.
    """
    root = create_app()
    tenant_apps = root.state.tenant_apps
    specs: dict[str, dict[str, Any]] = {}
    for tenant in TENANTS:
        spec = tenant_apps[tenant.mount_path].openapi()
        spec["info"]["version"] = SPEC_VERSION
        spec["paths"] = _prefix_paths(spec["paths"], tenant.mount_path)
        specs[tenant.name] = spec
    return specs


def render(spec: dict[str, Any]) -> str:
    """Serialize one OpenAPI document exactly as the committed file holds it.

    ``sort_keys=False`` keeps FastAPI's own ordering (paths in registration
    order, then components) rather than alphabetising the whole document. That
    order is already deterministic across runs, and alphabetising it would put
    every path's method, parameters and responses in an order nobody wrote,
    making real changes harder to find in a diff.
    """
    return yaml.safe_dump(spec, sort_keys=False, default_flow_style=False, allow_unicode=True)
