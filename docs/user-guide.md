# Connecting to the PDG Arctic Data MCP Server

## Overview

The PDG MCP server lets Claude, ChatGPT and Claude Code search and download public datasets from the NSF Arctic Data Center. You register and sign in with a PDG-MCP Keycloak account (Note: all three apps use the same Keycloak client `pdg-mcp`).

| What you'll enter | Value |
| --- | --- |
| MCP server URL | `https://pdg-mcp.software-dev.ncsa.illinois.edu/mcp` |
| OAuth Client ID | `pdg-mcp` |
| OAuth Client Secret | leave blank |

Before you start, you need:

- A PDG-MCP Keycloak account that an admin has approved. Register at [keycloak.software-dev.ncsa.illinois.edu/realms/PDG/account](https://keycloak.software-dev.ncsa.illinois.edu/realms/PDG/account) (click **Register** on the sign-in page). Then message the NCSA PDG team with your username or email and ask them to approve your account. Until they do, sign-in works but every tool call fails.
- One of these: a Claude plan (Free allows one custom connector), a ChatGPT plan with Developer mode (Plus, Pro, Business, Enterprise or Edu), or Claude Code.

Each user is limited to 2 tool calls per second, with bursts of up to 20.

## Claude (web and desktop)

Add the server as a **custom connector**, and enter `pdg-mcp` as the OAuth Client ID. If you leave the Client ID blank, Claude shows "Couldn't register with pdg-mcp's sign-in service".

1. In Claude, click your name (bottom left), then **Settings → Connectors**. On Team or Enterprise, an Owner adds the connector under **Organization settings → Connectors** first; members then connect it from their own Connectors page.
2. Click **Add custom connector**.
3. Fill in:
   - **Name:** `PDG Arctic Data`
   - **Remote MCP server URL:** `https://pdg-mcp.software-dev.ncsa.illinois.edu/mcp`
4. Open **Advanced settings** and set **OAuth Client ID** to `pdg-mcp`. Leave **OAuth Client Secret** blank.
5. Click **Add**, then **Connect**. Sign in on the Keycloak page that opens.
6. In a chat, click **+** (or the tools icon) → **Connectors**, and turn on **PDG Arctic Data**.
7. Test it by asking: "Search the Arctic Data Center for permafrost temperature datasets."

## ChatGPT (web)

Custom MCP servers only show up in ChatGPT after you turn on Developer mode. That requires Plus, Pro, Business, Enterprise or Edu. On Business, Enterprise and Edu, a workspace admin must allow Developer mode first.

1. Click your name (bottom left) → **Settings → Apps** (called **Apps & Connectors** or **Connectors** in some versions).
2. Scroll to **Advanced settings** and turn on **Developer mode**.
3. Click **Add** (top right) → **Add custom MCP server**. Older versions show a **Create** or **Create app** button instead.
4. Fill in:
   - **Name:** `PDG Arctic Data`
   - **Description:** `Search and download NSF Arctic Data Center datasets`
   - **MCP Server URL:** `https://pdg-mcp.software-dev.ncsa.illinois.edu/mcp`
   - **Authentication:** OAuth
5. Open the OAuth advanced fields and set **Client ID** to `pdg-mcp`. Leave **Client secret** blank, and choose token auth method **none** if asked.
6. Check **I trust this application**, then click **Create**. Sign in on the Keycloak page.
7. In a chat, click **+** → **More** → **Developer mode**, and turn on **PDG Arctic Data**.

ChatGPT renames these menus often. If something isn't where this guide says, search Settings for "Developer mode".

## Claude Code (CLI)

Run the command below exactly as written. The client ID and callback port `33418` must match the server's sign-in settings.

1. Add the server once for your user account:

```bash
claude mcp add --transport http --scope user --client-id pdg-mcp --callback-port 33418 pdg-mcp https://pdg-mcp.software-dev.ncsa.illinois.edu/mcp
```

2. Start `claude`, type `/mcp`, select **pdg-mcp**, and choose **Authenticate**. Your browser opens the Keycloak sign-in page.
3. After you sign in, the browser shows a success page and `/mcp` lists pdg-mcp as connected.
4. To remove the server: `claude mcp remove pdg-mcp --scope user`.

## Troubleshooting

If an error isn't listed here, copy the URL from your browser's address bar and send it with a screenshot to the PDG team.

| What you see | What to do |
| --- | --- |
| Claude: "Couldn't register with pdg-mcp's sign-in service" | Edit the connector: **Advanced settings → OAuth Client ID** = `pdg-mcp` |
| "Invalid client credentials" or unauthorized\_client | Remove the client secret from the connector; leave it blank |
| Sign-in works, but every tool call fails or returns 401 | Your account isn't approved yet; ask the PDG team |
| Keycloak: "Invalid parameter: redirect\_uri" or "Invalid scopes" | A server setting issue; send the address-bar URL to the PDG team |
| Claude Code: connection refused after sign-in | Remove and re-add the server with `--callback-port 33418` |
| Tool error "retry in N s" | You hit the limit of 2 calls per second (bursts of 20); wait and retry |
