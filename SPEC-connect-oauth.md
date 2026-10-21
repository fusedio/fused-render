# SPEC: Connect — one pattern for every tool, its sign-in and its MCP

**Status:** Design recommendation, 2026-10-21. Supersedes the
"stateless Fused relay" plan in the OAuth-for-local-apps research notes.
**Policy it must satisfy:** open source, privacy first. Fused never takes
or stores user data, including tokens, and never sits between the user's
machine and a provider. Every sign-in goes from the user's computer to
the vendor and back; every API call likewise.

## 0. The one-paragraph version

Adding a popular tool (Slack, Google, GitHub, Notion, Linear, Atlassian,
HubSpot, ...) is **one folder**: a `provider.json` that names how the
tool signs in and where its MCP tools come from, plus the starter app
that implements the tools. Nothing in the core changes per tool. The
core is three pieces that exist once: a sign-in driver that knows four
auth kinds, a credential store that is the OS keychain, and a Connections
page that renders whatever the registry holds. The research notes'
relay is not built: Slack shipped PKCE for public clients on 2026-03-30,
so Slack joins Google on the loopback + PKCE path and no Fused server is
needed anywhere.

## 1. Context

The notes chose a Fused-hosted relay for Slack because Slack would only
exchange a code against a client secret. That is no longer so:

- Slack PKCE is GA (2026-03-30). A PKCE app sends `code_verifier` to
  `oauth.v2.access` with no `client_secret`, may use `localhost`
  redirects, and refreshes with `client_id` alone. Desktop redirects
  cannot request bot scopes; user scopes are what they allow, and the
  user-only `search:read` slack-connect needs is one of them. Refresh
  tokens of a PKCE app expire after 30 idle days.
- Google desktop clients are public by Google's own definition; the
  desktop `client_secret` may ship in the repo. Loopback
  `http://127.0.0.1:<any port>`, PKCE S256, refresh tokens always
  issued. A project still in Testing issues 7-day refresh tokens, so the
  project must be published; Gmail/Drive scopes bring verification.
- GitHub and Hugging Face issue tokens by **device code** to a public
  client id with no secret. This repo already does both: `hf_auth.py`
  drives hf's device flow (D402), `github_login.py` delegates to `gh`.
- Notion's public integrations still require a client secret at the
  code exchange (no PKCE). Notion also offers an internal-integration
  token the user pastes, and a hosted MCP server that does its own
  OAuth from inside the MCP client.
- Several vendors now host their own MCP server with OAuth 2.1 inside
  the MCP handshake (Notion, Linear, Atlassian, GitHub). The MCP client
  (Claude Code) performs that login and stores the token itself; Fused
  registers nothing and holds nothing. `claude_config/mcp.py` already
  drives it (`login` → detached OAuth).

So there is not one auth story but four, and the structure has to carry
all four without a per-tool special case in the core.

## 2. The pattern: a provider pack

```
fused_render/connect/                       # core, written once
├── registry.py        # loads providers/*/provider.json, validates, exposes them
├── oauth.py           # the sign-in driver: loopback+PKCE and device-code flows
├── store.py           # the credential store (OS keychain; see §5)
├── routes.py          # /api/connect/* (list, login, status, logout) for every provider
└── providers/
    ├── slack/    provider.json  manifest.json  icon.svg
    ├── google/   provider.json  icon.svg
    ├── github/   provider.json  icon.svg
    ├── notion/   provider.json  icon.svg
    └── linear/   provider.json  icon.svg

fused_render/templates/shared/connect_token.py   # stdlib: how an app READS a token

fused_render/bots/starters/<key>/              # the tool's local MCP app (exists today)
├── mcp.toml           # the tools, one [[tool]] per function
├── <key>.py           # the API client; gets its token via connect_token
├── index.html         # the app page; its Setup button opens the Connections card
└── starter.json       # {"connect": "<provider key>"}  ← replaces setup_tool/ready_key

fused_render/bots/presets/<key>/preset.json    # "apps": ["<key>"]  (exists today)
```

**Adding a tool is adding `providers/<key>/` and `starters/<key>/`.**
The registry picks the provider up by folder name. The Connections page
grows a card. The starter's tools reach bots (APP TOOLS) and Claude
(`fused app serve`) exactly as the Google Sheets starter's do today. A
test iterates every `provider.json`, validates it against the schema
below, and checks every `starter.json` `connect` key resolves.

### provider.json

```json
{
  "name": "Slack",
  "auth": [
    {"kind": "oauth-pkce",
     "client_id": "123.456",
     "authorize_url": "https://slack.com/oauth/v2/authorize",
     "token_url": "https://slack.com/api/oauth.v2.access",
     "revoke_url": "https://slack.com/api/auth.revoke",
     "identity_url": "https://slack.com/api/auth.test",
     "scopes": {"user_scope": "channels:history,channels:read,..."},
     "redirect": {"host": "localhost", "ports": [47823, 47824, 47825, 47826, 47827], "path": "/callback"},
     "byo": {"manifest": "manifest.json", "docs": "https://api.slack.com/apps"}},
    {"kind": "byo", "of": "oauth-pkce"}
  ],
  "mcp": {"app": "slack", "remote": null},
  "privacy": "Sign-in goes straight from this computer to Slack. The token stays in your Keychain. Fused runs no server in this flow."
}
```

`auth` is an ordered list. The first entry is the default the card
offers; the rest sit under "Advanced". `mcp.app` names the starter;
`mcp.remote` is a vendor-hosted MCP URL when one exists. A tool may have
both (Notion: remote MCP for Claude, local starter for bots).

### The four auth kinds

| kind | Flow | Fused holds | Who stores the token | Precedent in repo | Tools |
|---|---|---|---|---|---|
| `oauth-pkce` | loopback redirect + PKCE, local refresh | public `client_id` (and Google's non-secret desktop secret) | `connect/store.py` | `_fused_login.py` | Slack, Google, Linear (verify PKCE) |
| `oauth-device` | RFC 8628 device code, polled on a daemon thread | public `client_id` | `connect/store.py`, or the vendor CLI's own store when one exists | `hf_auth.py`, `github_login.py` | GitHub, Hugging Face |
| `mcp-native` | `claude mcp add --transport http <url>` then the MCP client's own OAuth 2.1 login | nothing | the MCP client | `claude_config/mcp.py` | Notion, Linear, Atlassian, GitHub remote MCP |
| `api-key` | paste (API key, internal-integration token, service-account JSON) | nothing | `connect/store.py` | Google Sheets/Docs starters | Notion internal integration, anything without OAuth |

`byo` is a modifier on `oauth-pkce` or `oauth-device`: same flow, the
user's own `client_id`, created from the shipped manifest.

Rule for choosing the default: **prefer the kind where Fused holds the
least.** A vendor CLI that already keeps the token (`gh`, `hf`) wins
over our store (D402's reasoning). `mcp-native` wins for Claude-only
use. `oauth-pkce` is the default when a tool's token must be usable by a
local starter app (bots cannot call host MCP servers; `steps_engine.py`
forbids it, so bots need a local app, which needs a token it can read).

### Two things `mcp-native` cannot do, and why the starter still exists

A token the MCP client stores is the MCP client's: a bot, a scheduled
task, or the app page cannot read it, and they should not. So a tool
that bots must drive ships a starter with `oauth-pkce`, `oauth-device`
or `api-key`, and the remote MCP entry is an extra for Claude Code
users, not the integration itself.

## 3. The sign-in driver (`connect/oauth.py`), written once

One module, two flows, every provider is a config entry.

**Loopback + PKCE**

1. Generate `state`, `code_verifier` (43 to 128 chars),
   `code_challenge = base64url(sha256(verifier))`.
2. Bind a listener on the provider's redirect host. Google: any free
   port on `127.0.0.1`. Slack: the first free port in the registered
   list (Slack's docs do not say the port is ignored for `localhost`;
   assume exact match, verify once, collapse the list if it is ignored).
3. Open the authorize URL in the default browser (`client_id`, scopes,
   `redirect_uri`, `state`, `code_challenge`, `S256`; Google adds
   `access_type=offline`).
4. The listener checks `state`, answers "You can close this tab", stops.
5. POST `code` + `code_verifier` (+ Google's desktop secret) to the
   token URL. Store. Call `identity_url` for the account label.
6. Refresh lazily, locally, on the first call that finds the access token
   expired. A failed refresh (Slack 30-day idle, Google revoke) flips the
   card to "Reconnect", never to a silently broken tool.

**Device code**: POST to the device endpoint, show the URL and short
code, poll on a daemon thread, join an in-flight login instead of
starting a second one, cancel by raising from the poll hook. That is
`hf_auth.py` verbatim, parameterised by URLs.

**Shared mechanics** (from the three precedents): a 600 s watchdog on a
person, never on a machine; success decided by a fresh identity probe,
never by an exit code; one flow per provider at a time; the page polls
`GET /api/connect/<key>` once a second while a flow is live.

**Paste fallback**, every provider, no bounce page: when the browser is
not on the machine running the app (SSH, container, remote Fused) it
lands on a loopback URL it cannot reach and shows the URL with the code
in it. The card offers "Paste the address your browser ended up on"; the
driver parses `code` and `state` and continues at step 5. gcloud, rclone
and the GitHub CLI do the same, and it covers the busy-port case.

## 4. HTTP surface (`connect/routes.py`)

```
GET  /api/connect                  every provider: key, name, icon, auth kinds, state, account, mcp
POST /api/connect/<key>/login      {kind?, client_id?} → starts a flow (joins a live one)
GET  /api/connect/<key>            {state: disconnected|pending|connected|reconnect, account, kind, url?, code?}
POST /api/connect/<key>/callback   {url} the paste fallback
POST /api/connect/<key>/logout     revoke at the vendor, delete locally; a failed revoke still deletes and says so
POST /api/connect/<key>/key        {value} for api-key kinds
```

`mcp-native` providers map `login`/`logout` onto `claude_config/mcp.py`'s
existing `add`/`login`/`logout`, so the card looks the same.

## 5. The credential store (`connect/store.py`)

The one place this repo's "no credential store" rule bends, and only
where no vendor-owned store exists (D402 could delegate to hf; Slack
and Google have nothing to delegate to).

- macOS: login Keychain, service `fused-render.connect.<key>`, via
  `security`, the way `bots/browser.py` keeps the profile key. Linux and
  Windows: `keyring` when a backend exists, else an owner-only (0600)
  file under the app's config dir; the card says which.
- One record per provider: `{kind, client_id, access, refresh?, expires?,
  account, scopes}`. The starter apps' `.fused/data` keys (today's
  service-account JSON) migrate into it.
- The token never enters `prefs.json`, logs, the diagnostics bundle,
  crash files or a child's environment. Diagnostics redaction gets the
  service-name prefix added before this ships.
- The UI shows the account name, never a masked token (D402).

**How an app reads it:** `templates/shared/connect_token.py`, stdlib
only, no `fused_render` import (SPEC PY-15), sibling-import-by-path
(`adding-a-shared-template-utility` skill). `connect_token.get("slack")`
returns a live access token, refreshing through the provider's token URL
when expired, writing the new one back through the same store. The
starter's `<key>.py` does `import connect_token` and nothing else about
auth. Refresh logic therefore lives twice, in `oauth.py` and here, held
equal by a shared test, for the same reason `app_entry.py` is a copy of
`app_listing.py`.

## 6. What the user sees

A Connections page (Preferences → Connections) with one card per
provider in the registry: icon, **Connect**, vendor consent page,
"Connected as ‹name›", **Disconnect**, the provider's `privacy` line,
and an "Advanced" disclosure listing the other auth kinds (BYO with a
"Create from manifest" link and a `client_id` field; paste a key). A
starter's "Needs setup" badge and its page's Setup button both derive
from `starter.json`'s `connect` key and open that card.

## 7. Per-tool entries, first wave

| Tool | Default kind | Advanced | MCP | Notes |
|---|---|---|---|---|
| Slack | `oauth-pkce`, Fused app, 13 user scopes, PKCE on, public distribution, no team lock | `byo` from manifest | starter `slack` (slack-connect) | hosted `mcp.slack.com/mcp` needs a confidential app today; add as `remote` only if it accepts the PKCE app |
| Google | `oauth-pkce`, desktop client, minimal read-only scopes per feature | `api-key` service account (today's starters) | starters `google-sheets-tabs`, `google-docs-tabs`, later Gmail/Calendar | publish the project; schedule verification before any restricted scope |
| GitHub | `oauth-device` delegated to `gh` (exists) | PAT paste | remote `api.githubcopilot.com/mcp` + starter via `gh` | nothing stored by Fused |
| Hugging Face | `oauth-device` via hf (exists, D402) | `HF_TOKEN` | none | moves under the same card UI, no code change |
| Notion | `api-key` internal integration | `mcp-native` `mcp.notion.com/mcp` | starter `notion` | public OAuth needs a secret: not offered |
| Linear | `oauth-pkce` if Linear accepts public clients (verify), else `api-key` | `mcp-native` `mcp.linear.app` | starter `linear` | |

## 8. Trust and open source

There is no Fused-hosted component to deploy, version or audit. The
reviewable surface is `fused_render/connect/` plus the Fused app
registrations, whose settings the shipped manifests reproduce verbatim,
so a user can diff Fused's Slack app against one they create themselves.
Relay, bounce page, integration platforms (Composio, Nango, Pipedream,
Paragon, Arcade) and a Fused login are not built; each either stores or
proxies the data, or adds an account.

## 9. Migration

1. `connect/` core with `oauth-pkce` and `oauth-device`; `store.py`;
   `connect_token.py`; the Connections page rendering the registry.
2. Slack pack: manifest with `pkce_enabled: true`, 13 user scopes, the
   localhost port list; slack-connect becomes starter `slack` reading
   through `connect_token`; HTTPS-localhost cert, fixed single port, the
   secret field and the team lock go.
3. Google pack; the two Google starters gain `connect: google` with the
   service account kept as Advanced.
4. Fold `hf_auth.py` and `github_login.py` behind the same routes and
   card (they keep their own storage).
5. `mcp-native` kind wired to `claude_config/mcp.py`; Notion and Linear
   packs.
6. Enable PKCE and public distribution on the Fused Slack app; ship its
   `client_id`.

## 10. Acceptance

- Adding a provider is two folders and zero core edits; the registry
  test validates every `provider.json`.
- A fresh user connects Slack with no Slack app, no Fused account, no
  cert prompt, and no request to any Fused host during the flow (a proxy
  log in the test asserts the host list).
- No token in prefs, logs, the diagnostics bundle or a subprocess
  environment (connect, build a bundle, assert).
- `grep` finds no relay URL and no Slack secret in the repo; Google's
  desktop secret only in its `provider.json` with its comment.
- Disconnect revokes at the vendor and clears the store.
- The paste fallback completes a connection when the listener is
  unreachable from the browser.
- A bot's APP TOOLS call on a `slack` starter tool succeeds using only
  `connect_token`; a Claude Code session using the Notion remote MCP
  succeeds with Fused storing nothing.
