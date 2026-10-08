# arcticdata-mcp

A [FastMCP](https://gofastmcp.com) server that exposes the [NSF Arctic Data Center](https://arcticdata.io)
DataONE REST API (`https://arcticdata.io/metacat/d1/mn/v2`) to MCP clients. Agents can search the catalog,
inspect datasets, and download raw data files. The server is protected by a shared password.

All tools are **read-only** and only see **public** content.

## Quick start

```bash
uv sync
cp config.example.toml config.toml   # then set [auth] password
uv run arcticdata-mcp                 # serves http://127.0.0.1:8000/mcp
```

Connect Claude Code:

```bash
claude mcp add --transport http arcticdata http://127.0.0.1:8000/mcp --header "Authorization: Bearer <password>"
```

Or from Python:

```python
from fastmcp import Client
async with Client("http://127.0.0.1:8000/mcp", auth="<password>") as c:
    await c.call_tool("search_datasets", {"query": "permafrost"})
```

## Authentication

In HTTP mode every request must include `Authorization: Bearer <password>`, where the password comes from
`[auth] password` in `config.toml` or from `ARCTICDATA_MCP_AUTH_PASSWORD`. The comparison is constant-time.
Requests without a valid password get `401`. The server refuses to start in HTTP mode without a password
unless you pass `--insecure`. `--transport stdio` runs locally for a single client and skips auth.

## Configuration

See [config.example.toml](config.example.toml). Any key can be overridden with
`ARCTICDATA_MCP_<SECTION>_<KEY>`.

| Key | Default | Meaning |
|-----|---------|---------|
| `auth.password` | — | Shared bearer password (required for HTTP) |
| `server.host` / `port` / `path` | `127.0.0.1` / `8000` / `/mcp` | HTTP listener |
| `downloads.max_inline_bytes` | 10 MiB | Largest file returned inline to the agent |
| `downloads.allow_server_save` | `false` | Allow `delivery="server"` to write files on the server host |
| `downloads.download_dir` | `./downloads` | Where server-side saves go |

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

Layout: `config.py` (settings), `auth.py` (bearer password), `client.py` (httpx + XML parsing + DataONE
errors), `solr.py` (query building), `downloads.py` (inline, URL, or server delivery), and `tools/`
(`search`, `datasets`, `objects`, `node`).
