"""Authentication against Model 1, and the operator identity everything else records.

Model 1's handover specifies two credentials:

* **RS256 JWT**, verified against its JWKS endpoint. This is an operator logging
  in. The token carries their identity and roles; we verify the signature, never
  the password.
* **X-API-Key**, for service-to-service calls (the worker posting alerts, a batch
  indexer). No user identity, so it gets a synthetic one.

Nothing here mints tokens. This service is a consumer of Model 1's identity, and
an inference layer that can issue its own credentials is an inference layer that
can be used to escalate.

Development mode
----------------
With no JWKS URL configured and ``require_auth`` off, requests are accepted as a
local operator. That is what makes the offline demo runnable. It is deliberately
loud — the startup log says so, ``/api/health`` reports ``auth: "disabled"``, and
setting ``require_auth=true`` makes the *absence* of configuration a startup
failure rather than a silent open door.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, Query, status
from jwt import PyJWKClient

from backend.app.config import settings

log = logging.getLogger("setu.api.auth")

_jwk_client: PyJWKClient | None = None
_jwk_client_at: float = 0.0


@dataclass
class Operator:
    """Who is making this request. Recorded on every mutation."""

    subject: str
    name: str = ""
    roles: list[str] = field(default_factory=list)
    kind: str = "operator"          # operator | service | local
    district: str | None = None  # Model 1 may scope a user to a district

    @property
    def label(self) -> str:
        return self.name or self.subject

    def can(self, role: str) -> bool:
        # 'admin' is a superset by convention in Model 1's role vocabulary. That
        # vocabulary is data, not an enum, so unknown roles are carried through
        # rather than rejected — a new department adding a role must not 403.
        return role in self.roles or "admin" in self.roles


LOCAL_OPERATOR = Operator(
    subject="local", name="Local Operator", roles=["admin"], kind="local",
)
SERVICE_OPERATOR = Operator(
    subject="service", name="Setu Service", roles=["service", "admin"], kind="service",
)


def _jwks() -> PyJWKClient:
    """Cached JWKS client. Rotating keys are re-fetched when the cache expires."""
    global _jwk_client, _jwk_client_at
    now = time.time()
    if _jwk_client is None or now - _jwk_client_at > settings.jwks_cache_seconds:
        _jwk_client = PyJWKClient(settings.model1_jwks_url, cache_keys=True)
        _jwk_client_at = now
    return _jwk_client


def _decode(token: str) -> dict:
    try:
        key = _jwks().get_signing_key_from_jwt(token).key
        return jwt.decode(
            token, key,
            algorithms=["RS256"],
            audience=settings.model1_audience or None,
            issuer=settings.model1_issuer or None,
            options={
                # Verify what Model 1 actually asserts. `aud`/`iss` are only
                # checked when configured, because a deployment that has not been
                # told the expected values cannot meaningfully check them, and
                # pretending otherwise would reject every valid token.
                "verify_aud": bool(settings.model1_audience),
                "verify_iss": bool(settings.model1_issuer),
            },
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except jwt.PyJWTError as exc:
        # Deliberately not echoing the parser's message: it distinguishes
        # "bad signature" from "unknown key id", which tells an attacker
        # which half of the credential to keep working on.
        log.warning("JWT rejected: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


def _operator_from_claims(claims: dict) -> Operator:
    """Map Model 1's token claims onto the roles this service checks.

    Model 1 issues ``role`` (a single string, e.g. ``super_admin``) alongside
    ``scopes`` (a list, e.g. ``["admin", "cameras:read", "cameras:write"]``).
    This previously read only ``roles``/``permissions``, neither of which Model 1
    sends — so every authenticated operator arrived with an empty role list and
    was refused by every ``require_role`` endpoint, including a super admin.
    The symptom was a 403 reading "Requires role 'admin'" for a token that
    plainly carried ``admin`` in its scopes.

    All four spellings are accepted. ``scopes`` is the load-bearing one, because
    it is where Model 1 puts the permission vocabulary this service's ``can()``
    is written against; ``role`` is carried too so that the operator's rank is
    recorded on audit rows even when it grants nothing extra.
    """
    def _as_list(value) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return [str(v) for v in value]

    roles: list[str] = []
    for key in ("scopes", "roles", "permissions", "role"):
        for item in _as_list(claims.get(key)):
            if item not in roles:
                roles.append(item)

    return Operator(
        subject=str(claims.get("sub") or claims.get("uid") or "unknown"),
        name=str(claims.get("name") or claims.get("preferred_username") or ""),
        roles=roles,
        kind="operator",
        district=claims.get("district"),
    )


async def current_operator(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    access_token: str | None = Query(default=None),
) -> Operator:
    """FastAPI dependency: resolve the caller, or 401.

    ``access_token`` exists for one caller: the SSE stream. ``EventSource`` is
    the only way a browser subscribes to server-sent events and it cannot set
    request headers, so a bearer token has nowhere to go. Every SSE
    implementation solves this the same way, by moving the token into the query
    string, and the trade is real: query strings reach access logs and Referer
    headers in a way headers do not. It is accepted here because these tokens are
    short-lived, the alternative is no realtime under authentication at all, and
    the header path remains the only one every other endpoint uses.
    """
    if x_api_key and settings.model1_api_key:
        # Constant-time compare: a length-or-prefix-sensitive `==` on a secret is
        # a timing oracle, and this one is a static key that never rotates.
        import hmac
        if hmac.compare_digest(x_api_key, settings.model1_api_key):
            return SERVICE_OPERATOR
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")

    if authorization and authorization.lower().startswith("bearer "):
        if not settings.model1_jwks_url:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Bearer token presented but MODEL1_JWKS_URL is not configured",
            )
        return _operator_from_claims(_decode(authorization.split(" ", 1)[1]))

    if access_token:
        if not settings.model1_jwks_url:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Token presented but MODEL1_JWKS_URL is not configured",
            )
        return _operator_from_claims(_decode(access_token))

    if settings.require_auth:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return LOCAL_OPERATOR


def require_role(role: str):
    """Dependency factory for endpoints that need more than being logged in."""

    async def _check(op: Operator = Depends(current_operator)) -> Operator:
        if settings.require_auth and not op.can(role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires role '{role}'")
        return op

    return _check


def auth_mode() -> str:
    if settings.require_auth:
        return "required"
    if settings.model1_jwks_url or settings.model1_api_key:
        return "optional"
    return "disabled"


async def model1_health() -> dict:
    """Is Model 1 reachable? Reported, never enforced.

    The control room must keep working when the infrastructure service is down —
    it is a directory and an identity provider, not a dependency of watching a
    camera. Degraded, not dead.
    """
    if not settings.model1_base_url:
        return {"configured": False, "reachable": None}
    # Model 1 serves its liveness probe at /healthz, not /health — /health is a
    # router prefix there and answers 404. Probing the wrong path reported
    # "reachable, status 404", which is the worst of both readings: it looks
    # like a live service returning an error, when in fact the service is fine
    # and the path was wrong.
    url = settings.model1_base_url.rstrip("/") + "/healthz"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(url)
        return {
            "configured": True,
            "reachable": r.status_code < 500,
            "status": r.status_code,
            # The console signs operators in against Model 1 directly rather than
            # through this service, so that a password never transits the
            # analytics layer. That requires it to know where Model 1 is, and the
            # base URL is not a secret — the JWKS it serves is public by design.
            "base_url": settings.model1_base_url.rstrip("/"),
            "portal_url": settings.model1_portal_url.rstrip("/"),
            "requires_auth": settings.require_auth,
        }
    except Exception as exc:
        return {"configured": True, "reachable": False, "error": type(exc).__name__}
