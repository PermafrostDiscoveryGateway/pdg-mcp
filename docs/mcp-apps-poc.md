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
