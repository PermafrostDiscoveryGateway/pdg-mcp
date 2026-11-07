"""Per-user authentication via Keycloak (OIDC).

Each user signs in to the Keycloak realm; the MCP client sends the resulting access token as
``Authorization: Bearer <jwt>``. The server verifies the token's signature (realm JWKS), issuer,
expiry and optional audience, and publishes OAuth discovery metadata so MCP clients can start the
login flow themselves. No user secrets are stored here.
"""

from __future__ import annotations

import logging
import secrets

from fastmcp.server.auth import AccessToken, AuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider

from arcticdata_mcp.config import AuthSettings
from arcticdata_mcp.observability import hash_user


log = logging.getLogger(__name__)


class RoleJWTVerifier(JWTVerifier):
    """JWT verifier that also requires a Keycloak realm role, so admins approve registered users.

    Self-registered users can sign in but hold no role until an admin grants it; they are rejected here.
    """

    def __init__(self, *, required_role: str, user_hash_key: str = "", **kwargs):
        super().__init__(**kwargs)
        self.required_role = required_role
        # Same keyed hash as the tool-call logs; never log the raw ``sub``. Random if no key is given.
        self.user_hash_key = user_hash_key or secrets.token_hex(32)

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None or not self.required_role:
            return access
        roles = (access.claims.get("realm_access") or {}).get("roles", [])
        if self.required_role not in roles:
            user = hash_user(str(access.claims.get("sub") or "unknown"), self.user_hash_key)
            log.warning("Rejected user %s: missing realm role %r", user, self.required_role)
            return None
        return access


def build_auth(settings: AuthSettings, user_hash_key: str = "") -> AuthProvider:
    """Create the Keycloak auth provider from ``[auth]`` settings.

    ``user_hash_key`` keys the hash that identifies users in log lines (see ``observability.user_hash_key``).
    """
    if not settings.realm_url:
        raise ValueError("auth.realm_url is required (e.g. https://keycloak.example.org/realms/PDG)")
    realm_url = settings.realm_url.rstrip("/")
    verifier = RoleJWTVerifier(
        required_role=settings.required_role,
        user_hash_key=user_hash_key,
        jwks_uri=f"{realm_url}/protocol/openid-connect/certs",
        issuer=realm_url,
        algorithm="RS256",
        required_scopes=settings.required_scopes or None,
        audience=settings.audience or None,
    )
    return KeycloakAuthProvider(realm_url=realm_url, base_url=settings.base_url, token_verifier=verifier)
