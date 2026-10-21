import pytest
from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider

from arcticdata_mcp.auth import build_auth
from arcticdata_mcp.config import AuthSettings, load_settings

REALM = "https://keycloak.example.org/realms/PDG"


def test_requires_realm_url():
    with pytest.raises(ValueError):
        build_auth(AuthSettings())


def test_builds_keycloak_provider():
    auth = build_auth(AuthSettings(realm_url=REALM, audience="arcticdata-mcp"))
    assert isinstance(auth, KeycloakAuthProvider)
    assert auth.realm_url == REALM


def test_env_override_parses_scope_list(monkeypatch):
    monkeypatch.setenv("ARCTICDATA_MCP_AUTH_REQUIRED_SCOPES", "openid, mcp:read")
    assert load_settings().auth.required_scopes == ["openid", "mcp:read"]


async def _verify(monkeypatch, roles, required="mcp-user"):
    from fastmcp.server.auth import AccessToken
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    from arcticdata_mcp.auth import RoleJWTVerifier

    async def fake(self, token):
        return AccessToken(token=token, client_id="c", scopes=[], claims={"sub": "u", "realm_access": {"roles": roles}})

    monkeypatch.setattr(JWTVerifier, "verify_token", fake)
    v = RoleJWTVerifier(required_role=required, jwks_uri=f"{REALM}/protocol/openid-connect/certs", issuer=REALM)
    return await v.verify_token("t")


async def test_role_grants_access(monkeypatch):
    assert await _verify(monkeypatch, ["mcp-user"]) is not None


async def test_missing_role_rejected(monkeypatch):
    assert await _verify(monkeypatch, ["default-roles-x"]) is None


async def test_empty_required_role_allows_any_user(monkeypatch):
    assert await _verify(monkeypatch, [], required="") is not None
