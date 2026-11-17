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

Prometheus has to scrape the pods directly, and there are two ways to tell it to:

- **Prometheus Operator** (a cluster with the `monitoring.coreos.com` CRDs, such as Rancher Monitoring): set
  `metrics.podMonitor.enabled: true`. The chart then creates a `PodMonitor` that targets the pods' `metrics`
  port. The Operator ignores the `prometheus.io/*` annotations, so without this nothing scrapes the pod. If your
  Prometheus has a `podMonitorSelector`, add the labels it needs under `metrics.podMonitor.labels`; an empty
  selector picks up every PodMonitor. The option is off by default so the chart still installs on clusters
  without those CRDs.
- **Annotation-based Prometheus:** by default the chart adds `prometheus.io/scrape`, `prometheus.io/port` and
  `prometheus.io/path` annotations to the pod. Turn them off with `metrics.scrapeAnnotations: false` if unused.

Counters live in each pod, so scrape every pod (each replica has its own limits and counts). Set
`metrics.enabled: false` to turn the endpoint off. Metric names are listed in the README.

On software-dev the app is deployed by an ArgoCD `Application` that sets Helm values inline (see the README), so
the environment-specific settings (`metrics.podMonitor.enabled: true` and `observability.userHashKeySecret`) go
into that `Application`'s `helm.values`, not into this repository.

**software-dev cluster, as checked on 2026-11-17** (it may have changed):

- Rancher Monitoring runs in `cattle-monitoring-system` (Prometheus Operator, Prometheus v3.5.0, Grafana 12.1.1).
  Its `podMonitorSelector` and `podMonitorNamespaceSelector` are empty, so a PodMonitor in the `pdg` namespace is
  picked up with no extra labels.
- Prometheus scrapes every 30 seconds and **keeps data for 10 days (and at most 10 GiB)**. The dashboard and
  `scripts/usage_report.py` can only look back that far, so numbers for a month or longer have to be recorded
  before they age out. The chart's daily archive job does this (see "Keeping numbers past Prometheus's
  retention" below); longer retention or remote storage would be the alternative.
- Grafana's dashboard sidecar loads ConfigMaps and Secrets labelled `grafana_dashboard: "1"` from the
  `cattle-dashboards` namespace only, and has no folder annotation configured. The chart's optional dashboard
  ConfigMap (`grafana.dashboard.enabled`) is built for exactly this.
- No log aggregation (no Loki, Elasticsearch or log collector) was found, so usage cannot be counted from the logs
  there; `kubectl logs` shows only recent output.

### Dashboard and reports

**Dashboard.** `charts/pdg-mcp/dashboards/pdg-mcp.json` is a Grafana 12 dashboard ("PDG MCP Usage") built on the metrics above:
tool calls, success rate and calls per tool for any time range, latency, Arctic Data Center load (status codes,
retries, queue wait, slots) and process memory. The time picker answers "how many calls, which tools" for any
period, and the Tool variable filters to specific tools. It shows aggregates only. An Active users panel is
reserved for a later change.

There are two ways to get it into Grafana:

- **By hand:** in Grafana choose Dashboards, New, Import, upload `pdg-mcp.json`, and pick the Prometheus data source.
- **From the chart:** set `grafana.dashboard.enabled: true`. The chart then creates a ConfigMap
  (`<release>-dashboard`) holding the dashboard, labelled `grafana_dashboard: "1"`. Grafana's dashboard sidecar, as
  shipped with Rancher Monitoring and kube-prometheus-stack, loads every ConfigMap that carries its label, so the
  dashboard appears within a minute or two and is updated whenever the chart changes. The dashboard is
  provisioned, so edits made in the Grafana UI are not saved back; change `charts/pdg-mcp/dashboards/pdg-mcp.json`
  instead. It uses a data source picker, so it works with whichever Prometheus data source Grafana has.

The sidecar only looks in the namespaces it is configured for, and that is usually **not** the app's namespace, so
the ConfigMap is created in `grafana.dashboard.namespace`. The default, `cattle-dashboards`, is what the software-dev
Grafana's sidecar watches (verified on 2026-11-17: `LABEL=grafana_dashboard`, `LABEL_VALUE=1`,
`NAMESPACE=cattle-dashboards`, `RESOURCE=both`, so it also loads Secrets). On another cluster, check what your
Grafana's sidecar uses before relying on the default:

```bash
kubectl -n cattle-monitoring-system get deploy rancher-monitoring-grafana -o yaml | grep -B1 -A3 -E 'name: (LABEL|NAMESPACE|FOLDER)'
```

If `LABEL` is not `grafana_dashboard` or `NAMESPACE` does not include the namespace you chose, set
`grafana.dashboard.labels` and `grafana.dashboard.namespace` to match, or the dashboard silently never shows up.

Folders: a dashboard goes into a Grafana folder only if the sidecar has a `FOLDER_ANNOTATION` setting, and the
ConfigMap then carries that annotation (`grafana.dashboard.annotations`). The software-dev sidecar has no such
setting, so the dashboard appears in the default "General" folder there, and an annotation such as
`grafana_folder: PDG` would have no effect.

If Argo CD deploys the chart into `pdg`, its `AppProject` must allow `cattle-dashboards` as a destination namespace,
or an administrator has to apply the ConfigMap. After syncing, confirm it exists and appears in Grafana:

```bash
kubectl -n cattle-dashboards get configmap pdg-mcp-dashboard
```

(`<release>-dashboard`, so `pdg-mcp-dashboard` for the release named `pdg-mcp`.)

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
  each end. On a short period that can be several percent, up to about 10% (tests showed 70 calls against a true 68,
  and 54 against 49); over days or weeks it is negligible. The script rounds to whole calls.
- Counters live in each pod and reset when a pod restarts. `increase()` handles resets, and the queries sum
  across pods. Every tool's series is created at zero on startup, so tool-call counts are not affected by
  restarts. The ADC request counters are created on first use, so after a restart the requests made before
  Prometheus first scrapes a new series are not counted: ADC totals can run a few requests low per restart.

### Keeping numbers past Prometheus's retention

Prometheus on software-dev keeps 10 days of data. The chart can run a daily CronJob that archives each finished UTC
day to a persistent volume, so monthly and longer totals stay possible. It is off by default.

**What it writes.** In the volume (mounted at `/data`): one `usage-YYYY-MM-DD.json` per day (the same totals and
per-tool table that `scripts/usage_report.py` prints), plus `daily-summary.csv` and `daily-tools.csv`, which are
rebuilt from all the daily files on every run, so re-running never duplicates rows. These are counts only. Distinct
users cannot be added up across days, so monthly active users need a different source.

**How it behaves.**
- It runs at 01:15 (`report.schedule`, in the cluster controller's timezone, normally UTC) and archives each of the
  previous 3 finished UTC days (`report.days`) that is not archived yet, so a missed run is caught up on the next one.
- A day for which Prometheus has no data (outside its retention, or never scraped) is **not written**, because it
  would look like a quiet day. If yesterday could not be archived the job exits 1, so a failed Job
  (`kubectl -n pdg get jobs`) means scraping or retention is broken. It also fails on the first run after
  deployment, until a full UTC day of data exists; that is expected.
- Days are UTC days, and each total is Prometheus's `increase()` over the day, so a day can differ from the true
  count by a few calls at its edges.

**Enable it.** In the Argo CD `Application`'s `helm.values` (or `--set` for Helm), together with the PodMonitor:

```yaml
metrics:
  podMonitor:
    enabled: true
report:
  enabled: true
```

The Prometheus URL defaults to `http://rancher-monitoring-prometheus.cattle-monitoring-system:9090`
(`report.prometheusUrl`). The job's image is the app image, which contains `scripts/usage_report.py`, so the image
must be rebuilt after this change.

**Storage.** By default the chart creates a `PersistentVolumeClaim` (`<release>-usage`, 1Gi, `ReadWriteOnce`) and
the cluster's default storage class provisions the volume, so no PersistentVolume file is needed. Check what the
cluster offers with `kubectl get storageclass`, and name one with `report.persistence.storageClassName` if the
default is not what you want. To use your own claim, set `report.persistence.existingClaim`.

If volumes must be created by hand (a static PersistentVolume), set `report.persistence.pv.enabled: true` and give
the backend in `report.persistence.pv.source`, for example an NFS export:

```yaml
report:
  enabled: true
  persistence:
    pv:
      enabled: true
      source:
        nfs: {server: nfs.example.org, path: /exports/pdg-mcp-usage}
```

The chart then also creates a `PersistentVolume` (reclaim policy `Retain`, reserved for this claim) and binds the
claim to it by name. To get plain YAML files you can review or apply yourself:

```bash
helm template pdg-mcp charts/pdg-mcp -n pdg --set report.enabled=true --set report.persistence.pv.enabled=true \
  --set report.persistence.pv.source.nfs.server=nfs.example.org --set report.persistence.pv.source.nfs.path=/exports/pdg-mcp-usage \
  --show-only templates/usage-report-pv.yaml --show-only templates/usage-report-pvc.yaml > usage-volume.yaml
```

Things to know about the volume:
- The claim carries `helm.sh/resource-policy: keep` and `argocd.argoproj.io/sync-options: Prune=false`, so removing
  the release or an Argo CD prune does not delete the archive. Delete it by hand if you want it gone.
- A PersistentVolume is cluster-scoped. If Argo CD manages the chart, its `AppProject` must allow that kind
  (`clusterResourceWhitelist`), or an administrator has to create the volume. Dynamic provisioning avoids this.
- The job runs as user 10001 with `fsGroup: 10001`. Some storage (plain NFS in particular) ignores `fsGroup`, so
  the directory may need to be writable by that user.

**Try it now, without waiting for 01:15.**

```bash
kubectl -n pdg create job --from=cronjob/pdg-mcp-usage-report usage-report-test
kubectl -n pdg logs job/usage-report-test
kubectl -n pdg delete job usage-report-test
```

The log lists each day as `archived`, `already archived` or `no data in Prometheus ...`. If it cannot reach
Prometheus the job fails with `error: Prometheus query failed ...`; that would also reveal a network policy in the way.

**Reading the files.** Mount the claim in a short-lived pod and copy the files out (run it when the job is not
running, because a `ReadWriteOnce` volume attaches to one node):

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: usage-reader
  namespace: pdg
spec:
  restartPolicy: Never
  securityContext: {runAsNonRoot: true, runAsUser: 10001, fsGroup: 10001}
  containers:
    - name: reader
      image: busybox:1.37
      command: ["sleep", "3600"]
      securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
      volumeMounts: [{name: usage, mountPath: /data, readOnly: true}]
  volumes:
    - name: usage
      persistentVolumeClaim: {claimName: pdg-mcp-usage}   # <release>-usage
```

```bash
kubectl -n pdg apply -f usage-reader.yaml
kubectl -n pdg cp usage-reader:/data ./usage-archive
kubectl -n pdg delete pod usage-reader
```

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

