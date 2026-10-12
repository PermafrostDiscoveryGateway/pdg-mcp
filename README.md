# arcticdata-mcp

A [FastMCP](https://gofastmcp.com) server that exposes the [NSF Arctic Data Center](https://arcticdata.io)
DataONE REST API (`https://arcticdata.io/metacat/d1/mn/v2`) to MCP clients. Agents can search the catalog,
inspect datasets, and download raw data files. HTTP access is per-user, through Keycloak login.

All tools are **read-only** and only see **public** content.

## Quick start

```bash
uv sync
cp config.example.toml config.toml   # then set [auth] realm_url
uv run arcticdata-mcp                 # serves http://127.0.0.1:8000/mcp
```

Connect Claude Code:

```bash
claude mcp add --transport http arcticdata http://127.0.0.1:8000/mcp   # then /mcp to sign in via Keycloak
```

Or from Python:

```python
from fastmcp import Client
async with Client("http://127.0.0.1:8000/mcp", auth="<keycloak-access-token>") as c:
    await c.call_tool("search_datasets", {"query": "permafrost"})
```

## Authentication

In HTTP mode every request must carry a Keycloak access token (`Authorization: Bearer <jwt>`). The server
verifies the signature against the realm's JWKS, plus issuer, expiry and (optionally) audience, and serves
OAuth discovery metadata so MCP clients can run the login flow themselves. Each user has their own Keycloak
account. Users can self-register but are rejected until an admin grants them the realm role in
`auth.required_role` (default `mcp-user`); revoke access by removing the role or disabling the user. Requests without a valid token get `401`. The server refuses to
start in HTTP mode without `auth.realm_url` unless you pass `--insecure`. `--transport stdio` runs locally for
a single client and skips auth.

Auth protects this MCP server only. The Arctic Data Center read endpoints are public and are called
anonymously, so no ADC credentials are involved.

## Configuration

See [config.example.toml](config.example.toml). Any key can be overridden with
`ARCTICDATA_MCP_<SECTION>_<KEY>`.

| Key | Default | Meaning |
|-----|---------|---------|
| `auth.realm_url` | — | Keycloak realm URL (required for HTTP) |
| `auth.base_url` | `http://127.0.0.1:8000` | Public URL of this server, for OAuth discovery |
| `auth.audience` | empty | Expected `aud` claim (skipped if empty) |
| `auth.required_role` | `mcp-user` | Realm role granted by an admin to approve a user (empty = any user) |
| `auth.required_scopes` | `openid` | Scopes the token must carry |
| `server.host` / `port` / `path` | `127.0.0.1` / `8000` / `/mcp` | HTTP listener |
| `downloads.max_inline_bytes` | 10 MiB | Largest file returned inline to the agent |
| `downloads.allow_server_save` | `false` | Allow `delivery="server"` to write files on the server host |
| `downloads.download_dir` | `./downloads` | Where server-side saves go |
| `ratelimit.max_concurrent` | `6` | Global cap on in-flight requests to ADC (0 = unlimited) |
| `ratelimit.queue_timeout` | `30` | Seconds to wait for a free slot before failing |
| `ratelimit.per_user_rate` | `2.0` | Sustained tool calls per second per user (0 = unlimited) |
| `ratelimit.per_user_burst` | `20` | Back-to-back calls allowed before the per-user rate applies |
| `ratelimit.max_retries` | `3` | Retries on 429/502/503/504 from ADC |
| `ratelimit.retry_backoff` / `retry_max_delay` | `1.0` / `30` | Exponential backoff base and cap, in seconds |

### Rate limiting

ADC does not rate-limit on its side, so this server limits itself. There are two layers:

- **Per user.** Each user gets a token bucket keyed on the Keycloak `sub` claim. The bucket holds `per_user_burst`
  calls and refills at `per_user_rate` per second. A tool call over the limit fails with a "retry in N s" error.
  Without auth (stdio or `--insecure`), all callers share one bucket.
- **Global.** At most `max_concurrent` requests are in flight to arcticdata.io at once, across all users. Other
  requests wait for up to `queue_timeout` seconds. Responses with 429/502/503/504 are retried with exponential
  backoff, and backing-off requests don't hold a slot.

`get_server_config` reports the active limits to agents. Limits are kept in memory per process, so with several
replicas the effective limits are multiplied by the replica count.

### How downloads reach the agent

`download_file` returns files up to `max_inline_bytes` **inline** as an MCP embedded resource: text for
CSV/XML/JSON, a base64 blob for anything else. For larger files it returns the direct
`/object/{pid}` URL with its size and checksum, plus a ready-made `curl` command. `preview_file` reads just
the first bytes or lines of any file, however large.

## Tools (47)

### Search (Solr `/query/solr`)
| Tool | Purpose |
|------|---------|
| `search_datasets` | Free-text search over all metadata |
| `search_by_title` | Title phrase or wildcard |
| `search_by_author` | Creator, last name, or investigator |
| `search_by_keyword` | Author keywords, GCMD keywords, topics |
| `search_by_bounding_box` | Spatial overlap or containment |
| `search_by_temporal_coverage` | Data collection period overlaps a range |
| `search_by_publication_date` | Uploaded, published, or modified in a range |
| `search_by_funding` | Award number, funder, or award title |
| `search_by_project` | Project name |
| `search_by_measured_variable` | Attribute name, label, description, or unit |
| `search_by_taxon` | Scientific name or taxonomic rank |
| `search_by_semantic_annotation` | Ontology concept URI (for example ECSO) |
| `search_by_location_name` | Place keyword or site name |
| `search_by_organization` | Contact or related organization |
| `search_by_license` | Indexed license name |
| `search_by_rights_holder` | Owner or submitter ORCID |
| `search_by_provenance` | PROV relations to a PID |
| `search_data_files` | Data files by name, format, size, or parent dataset |
| `search_by_format` | Any object by formatId |
| `find_dataset_by_identifier` | Resolve a DOI, PID, series ID, or file PID to its dataset |
| `count_datasets` | Number of matches only |
| `get_facet_counts` | Top values and counts of any field |
| `list_search_fields` | All index fields and their types |
| `advanced_solr_query` | Raw q/fq/fl/sort/facets |

### Datasets and packages
`get_dataset_details`, `list_dataset_files`, `get_metadata_document` (EML XML), `get_metadata_html`
(MNView), `get_resource_map` (OAI-ORE), `get_package_download_info` (BagIt zip), `get_dataset_citation`,
`get_version_history`.

### Objects and files (MNRead / MNAuthorization)
`download_file`, `preview_file`, `get_download_url`, `describe_object` (HEAD), `get_system_metadata`,
`get_checksum`, `list_objects`, `is_authorized`.

### Node and reference
`get_node_info`, `ping`, `list_query_engines`, `list_views`, `list_object_formats` and `get_object_format`
(from the DataONE CN), `get_server_config`.

### Not exposed
MNStorage (create, update, delete, archive), MNReplication, `/log`, and `/generate` are left out on purpose.
They modify the repository or need a DataONE admin token.

## Development

```bash
uv run pytest            # unit and mocked tests
uv run pytest -m live    # smoke tests against arcticdata.io
```

Layout: `config.py` (settings), `auth.py` (Keycloak token verification), `client.py` (httpx + XML parsing + DataONE
errors), `solr.py` (query building), `downloads.py` (inline, URL, or server delivery), `ratelimit.py` (per-user and upstream limits), and `tools/`
(`search`, `datasets`, `objects`, `node`).

## Deployment (software-dev)

`Dockerfile` builds the image. `.github/workflows/image.yml` builds it on every pull request and pushes it on pushes
to `main` and `v*` tags, to both:

- `hub.ncsa.illinois.edu/pdg/pdg-mcp` (NCSA Harbor, `pdg` project; the chart default). This needs the repository
  secrets `HUB_USERNAME` and `HUB_PASSWORD`, ideally a Harbor robot account with push access to `pdg`. Without them
  the Harbor push is skipped.
- `ghcr.io/permafrostdiscoverygateway/pdg-mcp`.

Tags are `main`/`latest` for `main`, `X.Y.Z` for `vX.Y.Z` tags, and `sha-<short>` for every push. The chart
pulls `image.tag`, which defaults to the chart's `appVersion`, so tag a release (`v0.1.0`) or set `image.tag: main`.
Override `image.repository` to pull from ghcr.io or another registry instead.

The Helm chart is in `charts/pdg-mcp`. It is stateless and needs no secrets besides the image pull secret: all config
is passed as `ARCTICDATA_MCP_*` environment variables, and Keycloak validates tokens through public keys.

software-dev is managed by ArgoCD, with one `Application` per app in `charts/apps/templates/`. Add
`charts/apps/templates/pdg-mcp.yaml` there, following the `open-webui.yaml` pattern but with a git source:

```yaml
spec:
  project: {{ .Values.cluster }}
  destination:
    name: {{ .Values.cluster }}
    namespace: pdg-mcp
  source:
    repoURL: https://github.com/PermafrostDiscoveryGateway/pdg-mcp.git
    path: charts/pdg-mcp
    targetRevision: main
    helm:
      releaseName: pdg-mcp
      values: |
        host: pdg-mcp.software-dev.ncsa.illinois.edu
        imagePullSecrets: [ncsa-hub]
        auth:
          realmUrl: https://keycloak.software-dev.ncsa.illinois.edu/realms/PDG
```

(plus the usual `metadata`, `annotations`, `syncPolicy` and `CreateNamespace=true` block, and a `pdgMcp.enabled`
flag in `values.yaml`). Before the first sync, create the Keycloak realm, client and `mcp-user` role described under
Authentication.

### Image pull secret

The `pdg` Harbor project is private, so the cluster needs a pull secret in the `pdg-mcp` namespace and the chart must
reference it (`imagePullSecrets: [ncsa-hub]`). Use a Harbor robot account with pull access to `pdg`:

```bash
kubectl create secret docker-registry ncsa-hub -n pdg-mcp --dry-run=client -o yaml \
  --docker-server=hub.ncsa.illinois.edu --docker-username='<robot account>' --docker-password='<robot secret>' \
  | kubeseal --format yaml > kubernetes/pdg-mcp.ncsa-hub.sealed.yaml
```

Commit the sealed file to software-dev's `kubernetes/` folder (never the plain Secret).
