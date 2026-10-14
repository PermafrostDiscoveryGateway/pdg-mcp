# Connecting to the PDG Arctic Data MCP Server

## Step 1: Register and get approved (do this first)

**You cannot use the server until the NCSA team approves your account.** Registering alone is not enough: sign-in will succeed, but every search fails until an admin grants you access. Approval is manual, so request it before you set up any app.

1. Open the [PDG sign-in page](https://keycloak.software-dev.ncsa.illinois.edu/realms/PDG/account) and click **Register**.
2. Fill in your name, email and a password, then verify your email if asked.
3. Message the NCSA PDG team on Slack and ask for access. Include the email or username you registered with, for example: "Please approve my PDG-MCP account: jane.doe@example.edu."
4. Wait for the team to confirm. Then continue with the app you use below.

You also need one of: a Claude plan (Free allows one custom connector), a paid ChatGPT plan (Plus, Pro, Business, Enterprise or Edu), or Claude Code.

## Step 2: Values you will enter

Every app uses the same five values. **Copy them exactly**.

| Field | Value |
| --- | --- |
| Server URL | `https://pdg-mcp.software-dev.ncsa.illinois.edu/mcp` |
| Authentication | OAuth |
| OAuth client ID | `pdg-mcp` |
| OAuth client secret | Leave blank |
| Token endpoint auth method | `none` (not `client_secret_post`) |

Each user can make 2 tool calls per second, with bursts of up to 20.

## Step 3a: Claude (web and desktop)

1. Click your name (bottom left) → **Settings → Connectors**.
   - On Team or Enterprise plans, an Owner must first add the connector under **Organization settings → Connectors**. Members then connect it from their own Connectors page.
2. Click **Add custom connector**.
3. Enter **Name** `PDG Arctic Data` and **Remote MCP server URL** `https://pdg-mcp.software-dev.ncsa.illinois.edu/mcp`.
4. Open **Advanced settings**. Set **OAuth Client ID** to `pdg-mcp` and leave **OAuth Client Secret** blank.
5. Click **Add**, then **Connect**, and sign in with your PDG account.
6. In a chat, click **+** → **Connectors** and turn on **PDG Arctic Data**.

If you skip step 4, Claude shows "Couldn't register with pdg-mcp's sign-in service".

## Step 3b: ChatGPT (web)

ChatGPT adds MCP servers as plugins. The one setting people get wrong is **Token endpoint auth method**: it defaults to `client_secret_post` and must be changed to `none`.

1. Open the **Plugins** page and click **Add** (top right) → **Add custom MCP server**.
2. In the left half of the dialog, fill in:
   - **Name:** `PDG Arctic Data`
   - **Description:** `Search and download NSF Arctic Data Center datasets`
   - **Connection:** choose **Server URL**, then enter `https://pdg-mcp.software-dev.ncsa.illinois.edu/mcp`
   - **Authentication:** OAuth
3. Expand **Advanced OAuth settings**. In the right half, under **Client registration**, set:
   - **Registration method:** User-Defined OAuth Client
   - **OAuth client ID:** `pdg-mcp`
   - **OAuth client secret:** leave blank
   - **Token endpoint auth method:** change `client_secret_post` to **`none`**
   - **Scopes:** leave the defaults
   - Ignore the "CIMD is unavailable" notice and the Callback URL; neither needs action.
4. Tick **I understand and want to continue**, then click **Create as a plugin**.
5. Sign in with your PDG account when the sign-in page opens.
6. In a new chat, mention or select **PDG Arctic Data** so ChatGPT uses it.

If **Add custom MCP server** is missing, your workspace may require Developer mode: **Settings → Apps → Advanced settings → Developer mode**. On Business, Enterprise and Edu, a workspace admin must allow it first.

## Step 3c: Claude Code (CLI)

Run this once. The client ID and callback port `33418` must match exactly.

```bash
claude mcp add --transport http --scope user --client-id pdg-mcp --callback-port 33418 pdg-mcp https://pdg-mcp.software-dev.ncsa.illinois.edu/mcp
```

1. Start `claude`, type `/mcp`, select **pdg-mcp** and choose **Authenticate**.
2. Sign in with your PDG account in the browser that opens.
3. `/mcp` now lists pdg-mcp as connected.

To remove it later: `claude mcp remove pdg-mcp --scope user`.

## Step 4: Check that it works

Start a new chat with the server turned on and ask:

> Search the Arctic Data Center for permafrost temperature datasets.

A working setup returns a list of dataset titles with DOIs (for example `doi:10.18739/A2...`). If the assistant says it couldn't reach the server or offers only general advice, see Troubleshooting.

## Troubleshooting

Most failures come from a missing approval or the wrong token auth method. For anything not listed, send the PDG team the URL from your browser's address bar.

| What you see | Fix |
| --- | --- |
| Sign-in works, but every search fails or returns 401 | Your account isn't approved yet. Request access (Step 1). |
| ChatGPT: "I couldn't access the PDG Arctic Data connector's search actions" | Check you are approved, then open the plugin and confirm **Token endpoint auth method** is `none`. Reconnect and start a new chat. |
| "Invalid client credentials" or `unauthorized_client` | Set token auth method to `none` and remove any client secret. |
| Claude: "Couldn't register with pdg-mcp's sign-in service" | Edit the connector: **Advanced settings → OAuth Client ID** = `pdg-mcp`. |
| Sign-in page: "Invalid parameter: redirect\_uri" or "Invalid scopes" | Server-side setting. Send the address-bar URL to the PDG team. |
| Claude Code: connection refused after sign-in | Remove and re-add the server with `--callback-port 33418`. |
| Tool error "retry in N s" | You hit 2 calls per second (bursts of 20). Wait and retry. |
