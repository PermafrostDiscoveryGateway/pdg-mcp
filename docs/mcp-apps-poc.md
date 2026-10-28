# MCP Apps proof of concept: dataset map

[MCP Apps](https://modelcontextprotocol.io/docs/extensions/apps) let a tool return an interactive
HTML view that the client renders inline in the chat, in a sandboxed iframe. This PoC adds one:
`search_by_bounding_box` now shows its results on a map.

## What it does

When a client that supports MCP Apps (e.g. Claude, ChatGPT) calls `search_by_bounding_box`, it also
renders a Leaflet map that:

- draws the requested search box (dashed grey),
- draws each matching dataset's geographic footprint (blue),
- opens a popup with the title and PID when a footprint is clicked, and
- has an **Ask Claude about this dataset** button that sends a follow-up message into the chat.

Clients without MCP Apps support ignore the UI and get the same JSON result as before.

## How it works

| Piece | Where |
|---|---|
| UI resource `ui://arcticdata/search-map.html` (MIME `text/html;profile=mcp-app`) | `src/arcticdata_mcp/tools/apps.py` |
| The page itself | `src/arcticdata_mcp/ui/search_map.html` |
| Tool → UI link (`_meta.ui.resourceUri`) | `app=AppConfig(...)` on `search_by_bounding_box` in `tools/search.py` |

1. The tool's metadata points at the `ui://` resource, so the client fetches the page and renders it.
2. The page uses the `App` class from `@modelcontextprotocol/ext-apps` to talk to the host over `postMessage`:
   - `ontoolinput` receives the tool arguments, which are used to draw the search box.
   - `ontoolresult` receives the tool's `structuredContent`. The existing `results[].bbox` field
     is enough, so no server-side data changes were needed.
   - `sendMessage` posts a user message back into the conversation.
3. The resource declares a CSP (`resourceDomains`) allowing `cdn.jsdelivr.net` (Leaflet, ext-apps)
   and `tile.openstreetmap.org` (map tiles). Without it the host blocks those loads and the map is blank.

## Try it

Connect a client as described in the [user guide](user-guide.md), then ask something like:

> Find Arctic Data Center datasets on the North Slope of Alaska (north 71.5, south 68, east -141, west -166).

## Known limitations

- Boxes crossing the antimeridian are drawn incorrectly (the search tool doesn't support them either).
- Point datasets (north = south, east = west) render as tiny rectangles rather than markers.
- Only `search_by_bounding_box` has a UI. Other search tools also return `bbox` and could reuse the same resource.

## Water timeseries views

Two more tools add MCP Apps views that mirror the Historical view of the
[water-timeseries-v2](https://github.com/PermafrostDiscoveryGateway/water-timeseries-v2) "Lost Lakes" dashboard,
on a small sample of its data (#14). The UI is rewritten for MCP Apps (Leaflet instead of MapLibre/PMTiles), but
the data and charts come from the water_timeseries library itself.

| Tool | View | Matches the dashboard's |
|---|---|---|
| `show_lakes(north?, south?, east?, west?, only_drained?, only_with_timeseries?, limit?, offset?)` | `ui://water/lake-map.html` | drainage-year coloring (`get_style_pmtiles_drainage_year`), stable lakes in grey, *Hide stable lakes* toggle, hover fields and labels (`map_utils.py`), *Drainage Year* legend, ESRI / Dark Matter / TCVIS base layers, points below zoom 8 and polygons above, click popup with *Select for time series* |
| `get_lake_timeseries(lake_id)` | `ui://water/lake-timeseries.html` | *Time Series Plot of Lake* section: the Dynamic World chart (water, vegetation, bare, breakpoint line, relative-area axis) and the JRC chart, drawn from the raw series to match `plot_timeseries_interactive`, plus *Save Interactive Plot (HTML)* buttons (via the host's download) |

*Select for time series* and *Ask Claude to explain this series* send a message into the chat instead of
rerunning a Streamlit page. Lake geometry goes only in `structuredContent` (for the view); the text
result the model reads holds the lake attributes. `get_lake_timeseries` returns the same data to both: break statistics
and the masked series.

Not mirrored: the Near Real-Time mode, the monthly drainage statistics heatmap, recent imagery, yearly
thumbnails and timelapses (all Earth Engine), on-demand downloads, shareable links and the tutorial.

### Data

`scripts/export_water_fixtures.py` runs the library on the water-timeseries-v2 test fixtures and writes
`src/arcticdata_mcp/data/water/{lakes.geojson,timeseries.json}`:

- `DWDataset` / `JRCDataset` masking (no-data and snow months dropped) and the dashboard's vegetation grouping;
- `SimpleBreakpoint` breaks (the Historical mode's `break_method=simple`) with pre/post-break medians and change;
- the dashboard's lake name (`geohash_to_human_readable_name`).

The series are plain data: dates and one array per variable (6 decimals, since the chart's hover text divides them),
plus the Dynamic World `normalization_factor`. The view draws the charts itself (below).

The bundled sample is 121 lakes on the Seward Peninsula, 5 with time series, 3 of them drained. The server only reads the
JSON, so it doesn't depend on the library. Point `[water] data_dir` at another export to swap the data. The
library needs its full environment, and water-timeseries-v2's lockfile currently pins numpy<2 with a pyarrow that
needs numpy 2, so run the export in an isolated env:

```bash
uv run --no-project --python 3.12 --with ../water-timeseries-v2 --with "pyarrow<18" \
    python scripts/export_water_fixtures.py ../water-timeseries-v2/tests/data src/arcticdata_mcp/data/water
```

### Time series charts

`lake_timeseries.html` builds each chart from the raw series (`buildFigure`), as a port of
`DWDataset` / `JRCDataset.plot_timeseries_interactive`: the same traces, colors, hover text (`value ha | share %`),
axes, breakpoint line and `plotly_white` styling. Dynamic World shows each class as a share of the lake's
`normalization_factor`. JRC shows it as a share of the three series at that date. A lake with no break has no
breakpoint line. Traces are SVG `scatter`, so a chart takes no WebGL context (#17).

This replaced precomputed Plotly figures, which were about 26 KB per lake against 1 KB of data. Changing the chart
style means editing the view, not re-running the library. `buildFigure` was checked against the library's figures
for all 5 lakes (traces, values, hover shares to 0.01 point, breakpoint lines, axes and template).

### Lakes at scale: the GeoParquet store

`[water] data_dir` (or `ARCTICDATA_MCP_WATER_DATA_DIR`) picks the backend from what the directory holds
(`arcticdata_mcp/waterdata.py`):

- `lakes.parquet`: lakes are queried with [DuckDB](https://duckdb.org), a Python package that runs inside the server
  (nothing to install or run besides `uv sync`). A bounding box reads only the row groups that overlap it, so the
  full layer is never loaded into memory.
- `lakes.geojson`, or an empty `data_dir` (the bundled fixture): the whole file is held in memory and filtered in
  Python. Same results, for tests and small exports.

`scripts/build_water_store.py` writes `lakes.parquet` from the water-timeseries-v2 lake layer, or from the sample cut
by `scripts/make_water_sample.py`. It keeps the columns `show_lakes` returns, adds `west/south/east/north` and
`lon/lat` (so the server needs no DuckDB spatial extension, which downloads at runtime), stores simplified
GeoJSON geometry, and sorts by `id_geohash` (a geohash prefix is a map tile) in 20k-row groups:

```bash
uv run --group ingest python scripts/build_water_store.py \
    ../water-timeseries-v2/data/DW_historicalbp_simple_merged_breaks_with_allgeoms_v4.parquet data/water-store
ARCTICDATA_MCP_WATER_DATA_DIR=data/water-store uv run arcticdata-mcp --transport http --insecure
```

`timeseries.json` is copied next to it and still holds only the 5 test lakes.

What `show_lakes` returns:

- **Ranking.** Drained lakes first, largest water change first, then stable lakes by area. A capped result keeps
  the lakes that matter.
- **Model text.** Counts (`numFound`, `drained` over every match), a `limit`/`offset` page of lake attributes
  (default 50) and `next_offset`.
- **Map geometry follows the zoom the box implies.** With no box, or one wider than about 4 degrees (zoom below 8),
  the view gets one centroid per lake, up to 2000. Zoomed in, it gets simplified polygons, up to 300. Both stay
  around 1 MB. `map_note` says when the map shows fewer lakes than match, and the status line under the title
  shows it.

### Panning and zooming

The map reloads when the view changes. On `moveend` (300 ms debounce) `lake_map.html` calls the app-only
`lakes_in_view(north, south, east, west, zoom, only_drained?, only_with_timeseries?)` through
`app.callServerTool`, then redraws. The tool has `AppConfig(visibility=["app"])`, so a host hides it from the model
(a plain MCP client still lists it, with `_meta.ui.visibility = ["app"]`). It returns only the map payload (features,
counts, `map_note`) and uses the map's real zoom instead of one derived from the box. *Hide stable lakes* is sent as
`only_drained`, so the feature cap is spent on drained lakes. A response that arrives after a newer move is dropped.
The first draw (`show_lakes`) fits the map to its result and does not trigger a reload.

Longitudes: Leaflet repeats the base map across world copies but draws vector layers only at their own
coordinates. The map therefore sends wrapped longitudes (`west > east` when the view crosses 180 degrees, which the
server treats as `[west, 180]` plus `[-180, east]`) and draws each lake once per world copy the view overlaps, with
`worldCopyJump` on. Without that, lakes near the antimeridian (Alaska, Canada, far-eastern Siberia) vanish when the
view sits in another copy.

### Try it

> Show me the drained lakes in the water timeseries sample, then the time series of the biggest one.

### Limitations

- The map is capped at 2000 dots or 300 polygons per view and every move replaces the whole payload (#16, next
  step: vector tiles such as the dashboard's PMTiles).
- Only 5 lakes have a time series: the local data has no per-lake series for the full layer, so a source for them
  is still needed before `get_lake_timeseries` can cover more lakes (#16).
- Lakes without a time series in the sample show as stable; in the dashboard every lake has been analysed.
