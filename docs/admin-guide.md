# PDG MCP Server: Admin and Deployment Guide

## Keycloak client setup (one time)

One public client, `pdg-mcp`, in the **PDG** realm serves Claude, ChatGPT and Claude Code. Neither Claude nor ChatGPT can register its own client automatically (Keycloak rejects that), so users enter this client ID in each app.

| Item | Value |
| --- | --- |
| MCP server URL | `https://pdg-mcp.software-dev.ncsa.illinois.edu/mcp` |
| Keycloak realm | `https://keycloak.software-dev.ncsa.illinois.edu/realms/PDG` |
| Client ID | `pdg-mcp` (public, token endpoint auth method `none`) |
| Required realm role | `mcp-user` |

1. In the Keycloak admin console, switch to the **PDG** realm and open **Clients → pdg-mcp → Settings**.
2. Under **Capability config**, set:
   - **Client authentication:** Off (public client, no secret).
   - **Standard flow:** On. Turn **Direct access grants** and **Implicit flow** off.
3. Set **Valid redirect URIs** to all of the following:
   - `https://claude.ai/api/mcp/auth_callback`
   - `https://claude.com/api/mcp/auth_callback`
   - `https://chatgpt.com/connector/oauth/*` (ChatGPT adds a per-connector ID to the end)
   - `http://localhost:33418/callback` (Claude Code; must match the `--callback-port` in the user guide)
4. Set **Web origins** to `+`, then click **Save**.
5. On the **Advanced** tab, set **Proof Key for Code Exchange Code Challenge Method** to `S256` and save.
6. On the **Client scopes** tab, make sure these are assigned:
   - `roles` (Default). This puts `mcp-user` in the token.
   - `email` (Default or Optional). ChatGPT requests it.
   - `offline_access` (Optional). ChatGPT requests it so it can refresh the login.

## Approving and revoking users

Users can register on their own, but the server rejects them until an admin grants the `mcp-user` realm role.

1. Open **Users** in the PDG realm and find the user.
2. Go to **Role mapping → Assign role**, filter by realm roles, and assign `mcp-user`.
3. To revoke access, remove the role or disable the user. Tokens already issued stop working when they expire.

## Admin troubleshooting

When a user reports a sign-in error, ask for the URL in their browser's address bar. It shows the `redirect_uri` and `scope` the app sent.

| User sees | Cause | Fix |
| --- | --- | --- |
| Keycloak: "Invalid parameter: redirect\_uri" | The app's callback URL isn't registered | Add that `redirect_uri` to **Valid redirect URIs** (for ChatGPT, use `https://chatgpt.com/connector/oauth/*`) |
| Keycloak: "Invalid scopes" | `email` or `offline_access` isn't assigned to the client | Add both on the client's **Client scopes** tab |
| Sign-in works, then every tool call returns 401 | The user lacks `mcp-user`, or the `roles` scope is missing | Assign the role; server logs show `Rejected user ... missing realm role` |
| Claude Code: connection refused after sign-in | The user picked a callback port that isn't registered | Register `http://localhost:<port>/callback`, or have them use 33418 |
| Frequent "retry in N s" errors | Per-user rate limit (2 calls/s, burst 20) | Raise `ratelimit.per_user_rate` / `per_user_burst` (see Deployment) |

## Deployment

The server runs on software-dev from the `charts/pdg-mcp` Helm chart, with the image in the private `pdg` project on NCSA Harbor.

### Image pull secret

The cluster pulls the image with a Harbor robot account, stored as the `ncsa-hub` secret in the `pdg` namespace. Get the robot name and secret from the Harbor `pdg` project (**Robot Accounts**); never paste them into docs or chat.

```bash
kubectl --context software-dev -n pdg create secret docker-registry ncsa-hub --docker-server=hub.ncsa.illinois.edu --docker-username='<robot account name>' --docker-password='<robot secret>'
```

Then set `imagePullSecrets: [ncsa-hub]` in the chart values. To rotate the credential, regenerate the robot secret in Harbor, then delete and recreate `ncsa-hub`.

### Rate limits

The per-user and global limits are set through `extraEnv` in the chart values, for example `ARCTICDATA_MCP_RATELIMIT_PER_USER_RATE: "2"`. Limits apply per pod, so with more than one replica the effective limits multiply.

### Usage logs

Each tool call logs one JSON line with the tool, latency, outcome, a hashed user ID and the ADC requests it made
(fields are described in the README, "Usage logging"). Tool arguments and error text are never logged.

Set a stable secret for the user hash so one user keeps one hash across pods and restarts. Create it once, then
reference it in the chart values:

```bash
kubectl create secret generic pdg-mcp-observability --from-literal=user-hash-key="$(openssl rand -hex 32)"
```

```yaml
observability:
  userHashKeySecret: {name: pdg-mcp-observability, key: user-hash-key}
```

Do not rotate the key casually: hashes before and after a rotation cannot be matched, which breaks active-user
counts across the change. Other settings (`ARCTICDATA_MCP_OBSERVABILITY_LOG_LEVEL`, `..._LOG_FORMAT`,
`..._LOG_TOOL_CALLS`) go through `extraEnv`.
