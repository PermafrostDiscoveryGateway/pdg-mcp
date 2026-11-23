# MCP Apps proof of concept: dataset map

[MCP Apps](https://modelcontextprotocol.io/docs/extensions/apps) let a tool return an interactive
HTML view that the client renders inline in the chat, in a sandboxed iframe. This PoC adds one:
`search_datasets` (and `search_portal_datasets`) show their results on a map.

## What it does

When a client that supports MCP Apps (e.g. Claude, ChatGPT) calls `search_datasets`, it also
renders a Leaflet map that:

- draws the requested search box (dashed grey), if the call had one,
- draws each matching dataset's geographic footprint (blue),
- opens a popup with the title and PID when a footprint is clicked, and
- has an **Ask Claude about this dataset** button that sends a follow-up message into the chat.

Clients without MCP Apps support ignore the UI and get the same JSON result as before.

## How it works

| Piece | Where |
|---|---|
| UI resource `ui://arcticdata/search-map.html` (MIME `text/html;profile=mcp-app`) | `src/arcticdata_mcp/tools/apps.py` |
| The page itself | `src/arcticdata_mcp/ui/search_map.html` |
| Tool → UI link (`_meta.ui.resourceUri`) | `app=AppConfig(...)` on `search_datasets` in `tools/search.py` |

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

## Water timeseries views

Two more tools add MCP Apps views that mirror the Historical view of the
[water-timeseries-v2](https://github.com/PermafrostDiscoveryGateway/water-timeseries-v2) "Lost Lakes" dashboard,
on a small sample of its data (#14). The UI is rewritten for MCP Apps (Leaflet instead of MapLibre/PMTiles), but
the data and charts come from the water_timeseries library itself.

| Tool | View | Matches the dashboard's |
|---|---|---|
| `show_lakes(north?, south?, east?, west?, only_drained?, only_with_timeseries?)` | `ui://water/lake-map.html` | drainage-year coloring (`get_style_pmtiles_drainage_year`), stable lakes in grey, *Hide stable lakes* toggle, hover fields and labels (`map_utils.py`), *Drainage Year* legend, ESRI / Dark Matter / TCVIS base layers, points below zoom 8 and polygons above, click popup with *Select for time series* |
| `get_lake_timeseries(lake_id)` | `ui://water/lake-timeseries.html` | *Time Series Plot of Lake* section: the Dynamic World figure (water, vegetation, bare, breakpoint line, relative-area axis) and the JRC figure, both built by `plot_timeseries_interactive`, plus *Save Interactive Plot (HTML)* buttons (via the host's download) |

*Select for time series* and *Ask Claude to explain this series* send a message into the chat instead of
rerunning a Streamlit page. Polygons and Plotly figures go only in `structuredContent` (for the views); the text
result the model reads holds the lake attributes, break statistics and the masked series.

Not mirrored: the Near Real-Time mode, the monthly drainage statistics heatmap, recent imagery, yearly
thumbnails and timelapses (all Earth Engine), on-demand downloads, shareable links and the tutorial.

### Data

`scripts/export_water_fixtures.py` runs the library on the water-timeseries-v2 test fixtures and writes
`src/arcticdata_mcp/data/water/{lakes.geojson,timeseries.json}`:

- `DWDataset` / `JRCDataset` masking (no-data and snow months dropped) and the dashboard's vegetation grouping;
- `SimpleBreakpoint` breaks (the Historical mode's `break_method=simple`) with pre/post-break medians and change;
- the dashboard's figures, `plot_timeseries_interactive(lake, breakpoints=break_date, plot_variables=["water",
  "vegetation", "bare"])` for DW and the JRC equivalent, saved as Plotly JSON;
- the dashboard's lake name (`geohash_to_human_readable_name`).

The sample is 121 lakes on the Seward Peninsula, 5 with time series, 3 of them drained. The server only reads the
JSON, so it doesn't depend on the library. Point `[water] data_dir` at another export to swap the data. The
library needs its full environment, and water-timeseries-v2's lockfile currently pins numpy<2 with a pyarrow that
needs numpy 2, so run the export in an isolated env:

```bash
uv run --no-project --python 3.12 --with ../water-timeseries-v2 --with "pyarrow<18" \
    python scripts/export_water_fixtures.py ../water-timeseries-v2/tests/data src/arcticdata_mcp/data/water
```

### Try it

> Show me the drained lakes in the water timeseries sample, then the time series of the biggest one.

### Limitations

- Sample data only. Pan-Arctic scale needs PMTiles (as the dashboard uses) rather than GeoJSON in the tool
  result, and a hosted data source instead of bundled files.
- Lakes without a time series in the sample show as stable; in the dashboard every lake has been analysed.
