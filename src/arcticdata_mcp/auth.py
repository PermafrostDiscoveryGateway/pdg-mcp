"""Shared-password bearer-token authentication.

Clients send ``Authorization: Bearer <password>``; the password comes from the
server configuration file.
"""

from __future__ import annotations

import hmac

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken


class PasswordTokenVerifier(TokenVerifier):
    """Accept a bearer token only if it equals the configured password."""

    def __init__(self, password: str):
        if not password:
            raise ValueError("PasswordTokenVerifier requires a non-empty password")
        super().__init__()
        self._password = password.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not hmac.compare_digest(token.encode(), self._password):
            return None
        return AccessToken(
            token="redacted",
            client_id="arcticdata-mcp-client",
            scopes=[],
            expires_at=None,
        )
