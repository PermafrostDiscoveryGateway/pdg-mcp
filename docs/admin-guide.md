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

## Hosted chat (LibreChat)

`https://pdg-chat.software-dev.ncsa.illinois.edu` is a LibreChat instance with `pdg-mcp` preinstalled. Users sign in once with the PDG realm and bring their own model. LibreChat forwards each user's Keycloak access token to `pdg-mcp`, so they don't sign in a second time. `pdg-mcp` checks the token exactly as it does for Claude, so the `mcp-user` role and per-user rate limits still apply.

It runs in the `pdg` namespace as the `pdg-chat` release of [`charts/pdg-chat`](../charts/pdg-chat). That chart only wraps the upstream LibreChat chart (`oci://ghcr.io/librechat-ai/librechat-chart/librechat` 3.2.1, as in the [LibreChat Helm docs](https://www.librechat.ai/docs/local/helm_chart)) and carries our values. It is a separate release from `pdg-mcp`, so either can be upgraded or rolled back on its own.

| Component | Storage |
| --- | --- |
| LibreChat v0.8.9 | uploads on `nfs-taiga`, 5Gi |
| MongoDB (`bitnamilegacy/mongodb`, auth on) | `csi-cinder-sc-retain`, 2Gi. Cinder quota is shared across the cluster, so grow it only when needed (the storage class allows expansion). |
| Meilisearch (conversation search) | none. The index is rebuilt from MongoDB on restart. |

### Keycloak client (one time)

This is a second client in the **PDG** realm, separate from the public `pdg-mcp` client.

1. Create a client `librechat` with **Client authentication** On (confidential) and **Standard flow** On. Turn **Direct access grants** off.
2. Set **Valid redirect URIs** to `https://pdg-chat.software-dev.ncsa.illinois.edu/oauth/openid/callback`, and **Web origins** to `+`.
3. On **Client scopes**, make sure `roles`, `profile` and `email` are Default scopes. `roles` puts `mcp-user` in the access token, which both LibreChat and `pdg-mcp` check.
4. Copy the client secret from the **Credentials** tab for the next step.

### Secret (one time)

All secrets go in one Secret, `librechat-credentials-env`, which LibreChat, MongoDB and Meilisearch all read. Generate the values locally and never commit them. Type the Keycloak client secret at the prompt so it stays out of your shell history:

```bash
read -rs "KC_SECRET?librechat client secret: "; echo
kubectl --context software-dev -n pdg create secret generic librechat-credentials-env \
  --from-literal=CREDS_KEY=$(openssl rand -hex 32) \
  --from-literal=CREDS_IV=$(openssl rand -hex 16) \
  --from-literal=JWT_SECRET=$(openssl rand -hex 32) \
  --from-literal=JWT_REFRESH_SECRET=$(openssl rand -hex 32) \
  --from-literal=MEILI_MASTER_KEY=$(openssl rand -hex 32) \
  --from-literal=OPENID_SESSION_SECRET=$(openssl rand -hex 32) \
  --from-literal=OPENID_CLIENT_SECRET="$KC_SECRET" \
  --from-literal=mongodb-root-password=$(openssl rand -hex 24)
```

The chart builds `MONGO_URI` from `mongodb-root-password`, so the Secret doesn't need it. (`read -rs "VAR?prompt"` is zsh; in bash use `read -rsp "prompt" KC_SECRET`.)

Keep these values for the life of the install. `CREDS_KEY` and `CREDS_IV` encrypt the model keys users save, so changing them makes every user re-enter their keys. MongoDB only reads its root password when the volume is first created, so to rotate it, change it inside MongoDB as well as in the Secret, then `kubectl rollout restart deployment/pdg-chat-librechat`.

### Install and upgrade

```bash
helm dependency build charts/pdg-chat
```

```bash
helm --kube-context software-dev -n pdg upgrade --install pdg-chat charts/pdg-chat
```

To upgrade LibreChat, bump the `librechat` dependency version in `Chart.yaml` and `librechat.image.tag` in `values.yaml` together, run `helm dependency update charts/pdg-chat`, commit the new `Chart.lock`, and check the release notes for `librechat.yaml` changes (`version:` in `configYamlContent`).

### Models

There are no server-side model keys. Every provider asks the user for their own key:

- **Lumen**: the base URL is fixed (`LUMEN_BASE_URL`). We give Lumen keys only to selected users, outside LibreChat.
- **Your own (OpenAI-compatible)**: the user enters both the base URL and the key, for example OpenRouter, vLLM or Ollama. LibreChat calls the model from the cluster, so the URL must be reachable from there and must be a public address, because LibreChat blocks private IPs. `http://localhost:11434` won't work.
- **OpenAI, Anthropic, Google**: the user enters their own key.

Tools only work with models that support tool calling.

### Chat troubleshooting

| User sees | Cause | Fix |
| --- | --- | --- |
| Keycloak: "Invalid parameter: redirect\_uri" | The callback isn't registered on the `librechat` client | Add `https://pdg-chat.software-dev.ncsa.illinois.edu/oauth/openid/callback` |
| LibreChat rejects the login | The user lacks `mcp-user` | Assign the role (see Approving and revoking users) |
| Chat works, but `pdg` tools fail with 401 | The forwarded token is missing the role or has expired | Check the `pdg-mcp` logs for `Rejected user`; ask the user to sign out and back in |
| The model never calls the tools | The model doesn't support tool calling | Suggest a model that does |
