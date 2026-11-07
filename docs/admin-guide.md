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

Set a stable secret for the user hash so one user keeps one hash across pods and restarts. Without it each
process uses its own random key: logs still hide user identity, but the same user gets a different hash after a
restart or on another pod, so per-user and active-user counts over time are unreliable. Create it once, then
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

### Metrics (Prometheus)

The pod serves Prometheus metrics on a separate port (`metrics.port`, default 9100), declared as the container port
`metrics`. The chart's Service and Ingress only route the MCP port, so `/metrics` is not reachable from outside the
cluster. Keep it that way: do not add the metrics port to the Service or Ingress unless it is protected some other
way.

Prometheus has to scrape the pods directly. By default the chart adds `prometheus.io/scrape`, `prometheus.io/port`
and `prometheus.io/path` annotations to the pod, which works if the cluster's Prometheus discovers pods by
annotation. If it uses a `ServiceMonitor` or a static config instead, set `metrics.scrapeAnnotations: false` and
point it at the pods' `metrics` port. Counters live in each pod, so scrape every pod (each replica has its own
limits and counts). Set `metrics.enabled: false` to turn the endpoint off. Metric names are listed in the README.

### Dashboard and reports

**Dashboard.** `dashboards/pdg-mcp.json` is a Grafana 12 dashboard ("PDG MCP Usage") built on the metrics above:
tool calls, success rate and calls per tool for any time range, latency, Arctic Data Center load (status codes,
retries, queue wait, slots) and process memory. The time picker answers "how many calls, which tools" for any
period, and the Tool variable filters to specific tools. It shows aggregates only. An Active users panel is
reserved for a later change.

To use it, in Grafana choose Dashboards, New, Import, upload the JSON, and pick the Prometheus data source. The
chart does not deploy it automatically, because that depends on how your Grafana loads dashboards.

**Trying it locally.** With Docker running and the server started with
`uv run arcticdata-mcp --insecure` (metrics on port 9100):

```bash
docker compose -f dev/observability/docker-compose.yml up -d
```

Open http://localhost:3000 (no login; localhost only). If port 9090 or 3000 is already in use, set
`PROMETHEUS_PORT` or `GRAFANA_PORT` (for example `PROMETHEUS_PORT=9091 docker compose ... up -d`) and point the
report script at that port. Make some tool calls and the panels fill in. Stop and
delete the data with `docker compose -f dev/observability/docker-compose.yml down -v`. This is for development
only: Grafana runs with anonymous admin access.

**Numbers for reports.** `scripts/usage_report.py` queries Prometheus and prints totals and a per-tool table for a
period:

```bash
uv run python scripts/usage_report.py --prometheus-url http://localhost:9090 --from 2026-10-01 --to 2026-11-01
uv run python scripts/usage_report.py --from 2026-10-01 --format csv --section tools > tools.csv
```

`--format` is `table`, `csv` or `json`, and `--section` is `summary`, `tools` or `all`. The default period is the
last 30 days. `PROMETHEUS_URL` and `PROMETHEUS_TOKEN` (sent as a bearer token) can replace the flags. Output is
aggregate counts only. The same numbers in PromQL, for a range of `$range`:

| Number | PromQL |
| --- | --- |
| Tool calls | `sum(increase(pdg_mcp_tool_calls_total[$range]))` |
| Calls per tool | `sum by (tool) (increase(pdg_mcp_tool_calls_total[$range]))` |
| Success rate | `sum(increase(pdg_mcp_tool_calls_total{status="ok"}[$range])) / sum(increase(pdg_mcp_tool_calls_total[$range]))` |
| ADC 5xx | `sum(increase(pdg_mcp_adc_requests_total{status=~"5.."}[$range]))` |

Things that affect the numbers:

- Prometheus only holds data for its retention period, so a report cannot reach further back than that.
- `increase()` extrapolates at the edges of the period, by up to about one scrape interval's worth of calls at
  each end. On a short period that is a few percent (a test showed 70 calls against a true 68); over days or weeks
  it is negligible. The script rounds to whole calls.
- Counters live in each pod and reset when a pod restarts. `increase()` handles resets, and the queries sum
  across pods. Every tool's series is created at zero on startup, so tool-call counts are not affected by
  restarts. The ADC request counters are created on first use, so after a restart the requests made before
  Prometheus first scrapes a new series are not counted: ADC totals can run a few requests low per restart.

## Usage data and privacy

This is the interim policy for the usage data described under "Usage logs" and "Metrics". It records what the
code does today and the decisions made so far. Items marked **open** still need an answer from the project or
from NCSA.

### What is and is not collected

| Data | Policy |
| --- | --- |
| Search terms, tool arguments, error messages | **Never logged.** Not in log lines, not in metrics. |
| Which tool was called, when, how long, success or failure | Logged and counted. |
| Which ADC endpoint a call used, with status and timing | Logged and counted. Only the endpoint name (`query`, `object`, ...), never the URL or a PID. |
| User identity | Only `user_hash`, a keyed HMAC of the Keycloak `sub`. The raw `sub` is never logged, including in the "Rejected user" warning for people who lack the `mcp-user` role. |

How "never log query text" is enforced:

- The call logger records the tool name, outcome, timing and ADC request summary only. It does not read the
  arguments, and it does not log error text, because that can echo the query.
- Metric labels come from small fixed sets: tool names the server registers, ADC endpoint names and status codes.
  A tool name a client invents is counted as `unknown`.
- The `httpx` and `httpcore` loggers are held at WARNING. HTTP client libraries log full request URLs at INFO, and
  those URLs contain the search terms. A test fails if query text reaches any log, even with DEBUG logging on.

Two limits to be aware of:

- ADC receives every search, as it always has, and keeps its own logs. This server does not control them.
- `user_hash` is **pseudonymous, not anonymous**. Anyone holding both the hash key and the list of Keycloak users
  could work out which hash belongs to whom. Treat the key and the logs as sensitive.

### Decisions so far

1. **Query text is never logged.** An opt-in for debugging may be added later; it needs its own issue and a
   review of this policy first.
2. **Retention: logs containing `user_hash` are kept indefinitely for now.** There is no deletion schedule yet.
   **Open:** agree a retention period with the project and NCSA, and set it on the log store.
3. **Reporting is aggregate only.** Dashboards and reports show counts of users, tool calls and tools. They do not
   show per-user tables, per-user panels or exports of individual `user_hash` values.
4. **The hash key is not rotated for now.** Keep it stable so a user keeps one hash, which active-user counts
   depend on. **Open:** whether to rotate, and how often. Rotating makes older and newer hashes unlinkable, which
   is better for privacy and breaks returning-user trends across the rotation.

### Open questions

- Retention period for logs with `user_hash` (decision 2).
- Key rotation policy (decision 4).
- Who may read raw logs and the hash Secret.
- Whether very small counts (for example one user using a tool) should be hidden in reports.
- Whether the notice in the user guide is enough, or users need to agree to something.

