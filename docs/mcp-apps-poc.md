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

Two more tools add MCP Apps views on top of a sample of the
[water-timeseries-v2](https://github.com/PermafrostDiscoveryGateway/water-timeseries-v2) lake data (#14):

| Tool | View | Resource |
|---|---|---|
| `show_lakes(north?, south?, east?, west?, only_with_timeseries?)` | Leaflet map on Esri imagery; lakes colored by net area change, lakes with a time series marked yellow. A popup button asks the model to call `get_lake_timeseries`. | `ui://water/lake-map.html` |
| `get_lake_timeseries(lake_id)` | Plotly chart of monthly Dynamic World water area (ha); other land-cover classes can be toggled from the legend. A button asks the model to explain the series. | `ui://water/lake-timeseries.html` |

`show_lakes` puts the polygons only in `structuredContent` (for the map); the text result the model reads holds
just the lake attributes.

### Data

The tools read `lakes.geojson` and `timeseries.json`, by default the copies bundled in
`src/arcticdata_mcp/data/water/` (121 lakes on the Seward Peninsula, 5 with time series), exported from the
water-timeseries-v2 test fixtures so this server does not need geopandas or xarray. To regenerate them, or to
export another set and point `[water] data_dir` at it:

```bash
uv run --no-project --python 3.12 --with geopandas --with pyarrow --with "xarray<2026" --with netcdf4 \
    python scripts/export_water_fixtures.py ../water-timeseries-v2/tests/data src/arcticdata_mcp/data/water
```

### Try it

> Show me the lakes in the water timeseries sample, then the time series of one that drained.

### Limitations

- Sample data only. Pan-Arctic scale needs PMTiles (as the water-timeseries dashboard uses) rather than GeoJSON
  in the tool result, and a hosted data source instead of bundled files.
- No NRT drainage, breakpoints or Earth Engine downloads.
