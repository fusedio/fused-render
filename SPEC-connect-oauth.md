# SPEC: Connect — one pattern for every tool, its sign-in and its MCP

**Status:** Design recommendation, 2026-10-21 (rev 3: fused-render is the
MCP client). Supersedes the "stateless Fused relay" plan in the
OAuth-for-local-apps research notes.
**Policy it must satisfy:** open source, privacy first. Fused never takes
or stores user data, including tokens, and never sits between the user's
machine and a provider. Every sign-in goes from the user's computer to
the vendor and back; every API and MCP call likewise.
**Requirement added in rev 3:** the MCP access belongs to fused-render,
not to Claude Code. Bots, app pages, scheduled tasks and the chat all
reach a vendor's hosted MCP server through fused-render's own client.

## 0. The one-paragraph version

Adding a popular tool (Slack, Google, GitHub, Notion, Linear, Atlassian,
HubSpot, ...) is **one folder**: a `provider.json` that names how the
tool signs in and where its tools come from, plus, where the vendor has
no hosted MCP server, a starter app that implements the tools. The core
is written once: a sign-in driver with four auth kinds, a Streamable
HTTP MCP client, a credential store that is the OS keychain, a tool
roster that bots and pages already use, and a Connections page that
renders the registry. No Fused server exists in any flow. The research
notes' relay is not built: Slack shipped PKCE for public clients on
2026-03-30, and the MCP authorization spec lets a local client register
itself with a vendor's authorization server.

## 1. Context

**Why no relay.** The notes chose a Fused-hosted relay for Slack because
Slack would only exchange a code against a client secret. Slack PKCE is
GA (2026-03-30): a PKCE app sends `code_verifier` to `oauth.v2.access`
with no secret, may use `localhost` redirects, refreshes with `client_id`
alone. Desktop redirects cannot request bot scopes; user scopes are what
they allow, and the user-only `search:read` slack-connect needs is one.
Refresh tokens of a PKCE app expire after 30 idle days.

**Why fused-render can be the MCP client.** The MCP authorization spec
(2025-11-25) is OAuth 2.1 for public clients: the client discovers the
authorization server from the MCP server's Protected Resource Metadata
(RFC 9728), reads the server's metadata (RFC 8414), registers itself by
Client ID Metadata Document (an HTTPS URL that points at a static JSON
file) or Dynamic Client Registration (RFC 7591), uses PKCE S256, a
`localhost` redirect and the `resource` parameter, and refreshes with
rotating refresh tokens. Nothing in it needs a secret or a server of
ours. Notion, Linear and Atlassian's hosted servers accept DCR today.
GitHub's hosted server does not (it takes a pre-registered OAuth app or
a token as Bearer). Slack's hosted server needs a Slack app of one's own.

**What the repo already has.** `_fused_login.py` (loopback PKCE
callback), `hf_auth.py` (device code on a daemon thread, D402),
`github_login.py` (delegate to `gh`), `botmcp.py` (a hand-written stdio
MCP server, stdlib only), `apptools.py` (the APP TOOLS roster bots call
through the `tool` action, with an approval gate for writes), and
`claude_config/mcp.py` (registering servers with Claude Code, a separate
and optional concern). `httpx` is a core dependency; the `mcp` package is
only in the `[fused]` extra, so the client below is hand-written on
`httpx`, the way `botmcp.py` is hand-written on the stdlib.

## 2. The pattern: a provider pack

```
fused_render/connect/                       # core, written once
├── registry.py        # loads providers/*/provider.json, validates, exposes them
├── oauth.py           # sign-in driver: loopback+PKCE and device-code flows
├── discovery.py       # MCP auth discovery: PRM → AS metadata → CIMD / DCR / pre-registered
├── mcp_client.py      # Streamable HTTP MCP client on httpx: initialize, tools/list, tools/call
├── mcp_tools.py       # remote tools as ToolRecs in the APP TOOLS roster; approval from annotations
├── store.py           # the credential store (OS keychain; §5)
├── routes.py          # /api/connect/* for every provider (§4)
└── providers/
    ├── slack/    provider.json  manifest.json  icon.svg
    ├── google/   provider.json  icon.svg
    ├── github/   provider.json  icon.svg
    ├── notion/   provider.json  icon.svg
    ├── linear/   provider.json  icon.svg
    └── atlassian/ provider.json icon.svg

fused_render/templates/shared/connect_token.py   # stdlib: a starter READS a direct-API token

fused_render/bots/starters/<key>/     # only for tools WITHOUT a hosted MCP server
├── mcp.toml  <key>.py  index.html
└── starter.json       # {"connect": "<provider key>"} replaces setup_tool/ready_key

fused_render/bots/presets/<key>/preset.json    # "apps": ["<key>"] or "connect": ["<key>"]
```

**Adding a tool is adding `providers/<key>/`**, plus `starters/<key>/`
when the vendor has no hosted MCP server. The registry picks the
provider up by folder name; the Connections page grows a card; the
tools appear in the roster. A test iterates every `provider.json`,
validates it against the schema below, and checks every `connect` key
in a `starter.json` or `preset.json` resolves.

### provider.json

```json
{
  "name": "Notion",
  "auth": [
    {"kind": "mcp-oauth", "resource": "https://mcp.notion.com/mcp",
     "client": ["cimd", "dcr", "prompt"]},
    {"kind": "api-key", "label": "Internal integration token",
     "header": "Authorization: Bearer {value}", "docs": "https://www.notion.so/profile/integrations"}
  ],
  "tools": {"remote": "https://mcp.notion.com/mcp", "app": null},
  "privacy": "Sign-in and every call go straight from this computer to Notion. The token stays in your Keychain. Fused runs no server in this flow."
}
```

```json
{
  "name": "Slack",
  "auth": [
    {"kind": "oauth-pkce", "client_id": "123.456",
     "authorize_url": "https://slack.com/oauth/v2/authorize",
     "token_url": "https://slack.com/api/oauth.v2.access",
     "revoke_url": "https://slack.com/api/auth.revoke",
     "identity_url": "https://slack.com/api/auth.test",
     "scopes": {"user_scope": "channels:history,channels:read,..."},
     "redirect": {"host": "localhost", "ports": [47823, 47824, 47825, 47826, 47827], "path": "/callback"},
     "byo": {"manifest": "manifest.json", "docs": "https://api.slack.com/apps"}}
  ],
  "tools": {"remote": null, "app": "slack"},
  "privacy": "..."
}
```

`auth` is ordered: the first entry is the card's default, the rest sit
under "Advanced". `tools.remote` is a hosted MCP server fused-render
connects to; `tools.app` is a local starter. A tool may have both.

### The four auth kinds

| kind | Flow | Fused ships | Who holds the token | Precedent | First wave |
|---|---|---|---|---|---|
| `mcp-oauth` | MCP auth spec: discover, register (CIMD → DCR → ask), loopback + PKCE, `resource`, rotating refresh | nothing, or one static CIMD JSON (§3) | `connect/store.py`; the token never leaves the server process | `_fused_login.py` + new `discovery.py` | Notion, Linear, Atlassian |
| `oauth-pkce` | loopback + PKCE against a Fused-registered public client, local refresh | public `client_id` (+ Google's non-secret desktop secret) | `connect/store.py` | `_fused_login.py` | Slack, Google |
| `oauth-device` | RFC 8628 device code, polled on a daemon thread; or delegated to a vendor CLI | public `client_id` | `connect/store.py`, or the vendor CLI's own store (`gh`, `hf`) | `hf_auth.py`, `github_login.py` | GitHub, Hugging Face |
| `api-key` | paste (API key, internal-integration token, service-account JSON) | nothing | `connect/store.py` | Google Sheets/Docs starters | fallback everywhere |

`byo` is a modifier on `oauth-pkce` or `oauth-device`: same flow, the
user's own `client_id`, created from the shipped manifest.

Rule for the default: **prefer the kind where Fused holds the least and
the tools reach every surface.** `mcp-oauth` first when the vendor hosts
an MCP server that accepts a self-registered client: nothing to ship,
the vendor maintains the tools, bots and pages get them through §4.
`oauth-pkce` plus a starter when there is no hosted server (Slack today,
Google). A vendor CLI that already keeps the token (`gh`, `hf`) wins
over our store (D402).

## 3. The sign-in driver and MCP discovery

**`oauth.py`, one module, two flows, every provider a config entry.**

Loopback + PKCE: generate `state`, `code_verifier`, `code_challenge`
(S256); bind a listener on the redirect host (Google: any free port on
`127.0.0.1`; Slack and MCP servers: the first free port of the
registered list, see Ports); open the authorize URL (`client_id`,
scopes, `redirect_uri`, `state`, `code_challenge`, `S256`; Google adds
`access_type=offline`; MCP adds `resource`); the listener checks
`state`, answers "You can close this tab", stops; POST `code` +
`code_verifier` (+ `resource`) to the token URL; store; probe identity
for the account label. Refresh lazily, locally, on the first call that
finds the token expired; rotate the refresh token when the server
returns a new one; a failed refresh flips the card to "Reconnect".

Device code: `hf_auth.py` parameterised by URLs. Shared mechanics from
the three precedents: 600 s watchdog on a person; success decided by a
fresh probe, never an exit code; one flow per provider; join, do not
restart; cancel by raising from the poll hook; the page polls once a
second while a flow is live.

**`discovery.py`, the MCP authorization spec as the client does it.**

1. GET the MCP endpoint with no token; expect 401. Take
   `resource_metadata` and `scope` from `WWW-Authenticate`; else probe
   `/.well-known/oauth-protected-resource/<path>` then the root.
2. Read `authorization_servers[0]`; fetch its metadata, trying
   `/.well-known/oauth-authorization-server` then
   `/.well-known/openid-configuration`, with path insertion for issuers
   that have a path. Refuse to continue if
   `code_challenge_methods_supported` is absent (the spec's rule).
3. Pick a client identity, in the spec's priority order:
   1. a pre-registered `client_id` from `provider.json`, if any;
   2. **CIMD** when the server says `client_id_metadata_document_supported`:
      the `client_id` is an HTTPS URL to a static JSON document (`client_id`,
      `client_name`, `redirect_uris` with the localhost port list,
      `token_endpoint_auth_method: "none"`). Fused publishes that one
      file from this repo (GitHub Pages or fused.io, built by CI from
      `connect/client-metadata.json`). It is a static document, holds
      nothing, receives nothing but the vendor's fetch, and a self-hoster
      points `FUSED_RENDER_CLIENT_METADATA_URL` at their own copy;
   3. **DCR** when the server has `registration_endpoint`: POST
      `client_name`, `redirect_uris`, `grant_types`, `response_types`,
      `token_endpoint_auth_method: "none"`; store the issued `client_id`
      beside the token;
   4. ask the user for a client id (the vendor's own registration page
      linked from the card).
4. Scopes: the 401's `scope` if given, else `scopes_supported` from the
   resource metadata, else none. Handle 403 `insufficient_scope` at call
   time with one step-up re-authorization, then fail.
5. Hand the discovered endpoints to `oauth.py`'s loopback flow.

**`mcp_client.py`.** Streamable HTTP on `httpx`: POST JSON-RPC with
`Accept: application/json, text/event-stream`, `MCP-Protocol-Version`,
`Authorization: Bearer`; read either a JSON body or an SSE stream until
the response with the request id arrives; keep `Mcp-Session-Id`;
`initialize` → `notifications/initialized` → `tools/list` (cached per
connection, invalidated on `notifications/tools/list_changed`) →
`tools/call`. A 401 mid-session triggers one refresh and one retry.
The legacy SSE transport is out of scope (vendors call it deprecated).
Tokens are sent only to the MCP server they were issued for (the
spec's audience rule), so the store keys them by `resource` URL.

**Paste fallback, every provider, no bounce page.** When the browser is
not on the machine running the app (SSH, container, remote Fused) it
lands on a loopback URL it cannot reach and shows the URL with the code
in it. The card offers "Paste the address your browser ended up on";
the driver parses `code` and `state` and continues at the token step.
gcloud, rclone and the GitHub CLI do the same, and it covers the
busy-port case.

**Ports.** One registered list, `localhost:47823` to `:47827`, path
`/callback`, used by the Slack app, the CIMD document and every DCR
registration, so one listener implementation serves all. Google ignores
the port and gets any free one on `127.0.0.1`.

## 4. Where the tools go: every surface, one roster

`mcp_tools.py` turns each connected `tools.remote` server's `tools/list`
into `ToolRec`s in the same registry `apptools.registry()` returns:
`app` = the provider key, `name` = the MCP tool name, params from the
tool's `inputSchema`. Then, with no further change:

- **Bots** see them under APP TOOLS and call them with the `tool` action;
  `run_tool` dispatches to `mcp_client` instead of a subprocess. The
  approval gate reads MCP tool annotations (`readOnlyHint: true` runs at
  once; `destructiveHint` or no annotation goes through the gate, with
  today's name heuristic as the tie-break). The prompt line forbidding
  "host" MCP tools stays as is: these are roster tools, not host tools.
- **App pages and starters** call `POST /api/connect/<key>/tools/call`
  (same-origin, `X-Fused: 1`) from JS, or from Python through the
  server origin the way `apptools.run_py` does. The token never reaches
  app code.
- **Scheduled tasks and the chat** go through the same roster.
- **Claude Code**, optionally: a button on the card runs
  `claude_config/mcp.py`'s `add` for the same URL. Claude then does its
  own login and keeps its own token; this is a convenience, never the
  integration.

Direct-API starters (Slack, Google) keep making their own vendor calls
and read their token through `templates/shared/connect_token.py`
(stdlib, no `fused_render` import, sibling-import-by-path per the
`adding-a-shared-template-utility` skill); it refreshes through the
provider's token URL and writes back through the same store. Refresh
logic therefore exists in `oauth.py` and there, held equal by one test,
the way `app_entry.py` mirrors `app_listing.py`.

### HTTP surface (`connect/routes.py`)

```
GET  /api/connect                       every provider: key, name, icon, auth kinds, state, account, tools source
POST /api/connect/<key>/login           {kind?, client_id?} starts a flow (joins a live one)
GET  /api/connect/<key>                 {state: disconnected|pending|connected|reconnect, account, kind, url?, code?}
POST /api/connect/<key>/callback        {url} the paste fallback
POST /api/connect/<key>/logout          revoke at the vendor, delete locally; a failed revoke still deletes and says so
POST /api/connect/<key>/key             {value} for api-key kinds
GET  /api/connect/<key>/tools           the cached tools/list (remote) or mcp.toml tools (app)
POST /api/connect/<key>/tools/call      {name, args} → content, isError; 401 → refresh once; 403 → step-up once
```

## 5. The credential store (`connect/store.py`)

The one place this repo's "no credential store" rule bends, and only
where no vendor-owned store exists (D402 could delegate to hf; the
vendors here have nothing to delegate to).

- macOS: login Keychain, service `fused-render.connect.<key>`, via
  `security`, the way `bots/browser.py` keeps the profile key. Linux and
  Windows: `keyring` when a backend exists, else an owner-only (0600)
  file under the app's config dir; the card says which.
- One record per provider: `{kind, resource?, client_id, access, refresh?,
  expires?, account, scopes}`. Today's starter keys in `.fused/data`
  migrate in.
- For `mcp-oauth` the token is read only by the server process; app
  code gets tool results, never the token. For direct-API starters,
  `connect_token.py` reads it.
- Never in `prefs.json`, logs, the diagnostics bundle, crash files or a
  child's environment. Diagnostics redaction gets the service-name
  prefix before this ships. The UI shows the account name, never a
  masked token (D402).

## 6. What the user sees

Preferences → Connections: one card per provider in the registry. Icon,
**Connect**, the vendor's consent page, "Connected as ‹name›", the tool
count ("23 tools from Notion"), **Disconnect**, the provider's
`privacy` line, and an "Advanced" disclosure listing the other auth
kinds (BYO with "Create from manifest" and a `client_id` field; paste a
key; "Add to Claude Code"). A starter's "Needs setup" badge and a bot
preset's missing connection both open the card.

## 7. Per-tool entries, first wave

| Tool | Default kind | Advanced | Tools | Notes |
|---|---|---|---|---|
| Notion | `mcp-oauth` at `mcp.notion.com/mcp`, DCR confirmed | `api-key` internal integration | remote | nothing shipped |
| Linear | `mcp-oauth` at `mcp.linear.app/mcp`, DCR + PKCE public client confirmed via third-party docs | `api-key` | remote | verify against Linear's own docs |
| Atlassian | `mcp-oauth` at `mcp.atlassian.com/v1/mcp/authv2`, DCR | `api-key` (admin-enabled) | remote | Atlassian may allowlist clients; if our registration is refused, the API token path is the default |
| GitHub | `oauth-device` via `gh` (exists); the hosted MCP gets `gh auth token` as Bearer | PAT paste; BYO OAuth app | remote `api.githubcopilot.com/mcp` + `gh` starter | hosted server has no DCR; verify it accepts a `gh` token |
| Slack | `oauth-pkce`, Fused app, 13 user scopes, PKCE on, public distribution, no team lock | `byo` from manifest | starter `slack` (slack-connect); `mcp.slack.com/mcp` as `remote` only if it accepts the PKCE app | |
| Google | `oauth-pkce`, desktop client, minimal read-only scopes per feature | `api-key` service account (today) | starters `google-sheets-tabs`, `google-docs-tabs`, later Gmail/Calendar | publish the project; verification before any restricted scope |
| Hugging Face | `oauth-device` via hf (exists, D402) | `HF_TOKEN` | none | same card UI, no code change |

## 8. Trust and open source

No Fused-hosted service exists in any flow. The only Fused-published
thing is one static client-metadata JSON for CIMD, built from this repo
by CI, holding a name and a redirect list, overridable by a self-hoster;
and the Fused Slack and Google app registrations, whose settings the
shipped manifests reproduce verbatim so a user can diff them against an
app of their own. Relay, bounce page, integration platforms (Composio,
Nango, Pipedream, Paragon, Arcade) and a Fused login are not built; each
stores or proxies the data, or adds an account.

## 9. Migration

1. `connect/` core: `registry`, `oauth` (PKCE + device), `store`,
   `connect_token.py`, the Connections page.
2. `discovery.py` + `mcp_client.py` + `mcp_tools.py`; Notion pack first
   (DCR confirmed), bots calling Notion tools through the roster.
3. Linear and Atlassian packs; CIMD document published from CI.
4. Slack pack: manifest with `pkce_enabled: true`, 13 user scopes, the
   port list; slack-connect becomes starter `slack` reading through
   `connect_token`; HTTPS-localhost cert, fixed single port, the secret
   field and the team lock go. Enable PKCE and public distribution on
   the Fused app; ship its `client_id`.
5. Google pack; the two Google starters gain `connect: google` with the
   service account kept as Advanced.
6. Fold `hf_auth.py` and `github_login.py` behind the same routes and
   card; GitHub hosted MCP via `gh auth token`.

## 10. Acceptance

- Adding a provider is one folder (two with a starter) and zero core
  edits; the registry test validates every `provider.json`.
- A bot calls a Notion tool through APP TOOLS with Fused shipping
  nothing for Notion; an app page calls the same tool through
  `/api/connect/notion/tools/call`; a scheduled task does too.
- A fresh user connects Slack with no Slack app, no Fused account, no
  cert prompt, and no request to any Fused host during the flow (a proxy
  log in the test asserts the host list; the CIMD fetch, when it
  happens, is made by the vendor, not by the user's machine).
- No token in prefs, logs, the diagnostics bundle, a subprocess
  environment, or an app's tool result.
- `grep` finds no relay URL and no Slack secret in the repo; Google's
  desktop secret only in its `provider.json` with its comment.
- Disconnect revokes at the vendor and clears the store; a DCR client id
  is deleted with it.
- The paste fallback completes a connection when the listener is
  unreachable from the browser.
- A 401 mid-session refreshes once and retries; a 403
  `insufficient_scope` steps up once; a second failure surfaces as
  "Reconnect" on the card.

## 11. Implementation contract (first wave: Notion, GitHub, Slack)

This section is the interface each piece is built against. Four pieces
are built in parallel by separate agents; a piece may call another only
through what is written here.

### Ownership

| Piece | Files |
|---|---|
| core-auth | `fused_render/connect/{__init__,registry,store,oauth,discovery}.py`, `fused_render/connect/providers/{notion,github,slack}/provider.json` (+ `slack/manifest.json`, icons), `tests/_fake_vendor.py`, `tests/_fake_provider/fakevendor/provider.json`, `tests/test_connect_{registry,store,oauth,discovery}.py` |
| core-tools | `fused_render/connect/{mcp_client,tools,manager,routes}.py`, `fused_render/connect/providers/slack/tools.py`, `fused_render/server/app.py` (one `include_router`), `tests/test_connect_{mcp_client,tools,manager,routes}.py` |
| frontend | `frontend/src/shell/Preferences.tsx` (a Connections group) + `frontend/src/shell/Connections.tsx` (+ test), `fused_render/static/runtime.js` (`fused.connect`), `fused_render/templates/shared/fused_connect.py`, `tests/test_fused_connect_shared.py` |
| bots | `fused_render/bots/apptools.py`, `fused_render/bots/bot.py` (the `tool` path), `tests/test_bots_connect_tools.py` |

`fused_render/connect/__init__.py` stays empty. Nobody edits another
piece's files; a needed change there is a note in the final report.

### registry.py

```python
@dataclass
class Provider:
    key: str            # folder name
    name: str
    dir: str            # absolute pack folder
    auth: list[dict]    # ordered; first is the default
    tools: dict         # {"remote": url|None, "native": bool}  (native = pack has tools.py)
    privacy: str
    icon: str | None    # absolute path to icon.svg or None

def providers() -> list[Provider]      # built-in packs + every dir in $FUSED_RENDER_CONNECT_PROVIDERS (os.pathsep-separated), sorted by key
def get(key) -> Provider | None
def validate(d: dict, where: str) -> None   # raises ValueError naming the field
```

Auth kinds and their required fields:

- `mcp-oauth`: `resource` (the MCP URL). Endpoints are discovered.
- `oauth-pkce`: `client_id` (may be `""`, see availability), `authorize_url`, `token_url`, `redirect` `{host, ports|null, path}`, optional `scopes` (dict of query params, e.g. `{"user_scope": "..."}` or `{"scope": "..."}`), `identity_url`, `revoke_url`, `client_secret` (Google only), `extra_authorize` (dict), `byo` `{manifest, docs}`.
- `oauth-device`: `client_id`, `device_url`, `token_url`, `scope`, `identity_url`.
- `cli`: `command` (argv list printing a token on stdout), `identity_url`. Available when the command runs and prints a non-empty token.
- `api-key`: `label`, `identity_url`, optional `docs`.

**Availability.** A kind needing `client_id` with `""` in the file reads
`FUSED_RENDER_CONNECT_<KEY>_CLIENT_ID` (key upper-cased); still empty
means the kind is listed as `available: false` with `reason`. The
first available kind is the card's default.

### store.py

```python
def get(key) -> dict | None
def put(key, record: dict) -> None
def delete(key) -> None
def backend() -> str     # "keychain" | "file"
```

Record: `{"kind", "resource"?, "client_id"?, "client_registration"? (DCR
response), "access", "refresh"?, "expires_at"? (epoch seconds, float),
"token_type"?, "scope"?, "account"?, "extra"?}`. Backends: macOS login
Keychain via `security add-generic-password -U` / `find-generic-password -w`
/ `delete-generic-password`, service `fused-render.connect.<key>`,
account `<key>`; otherwise a `0600` file at
`<storage.home_dir()>/connect/<key>.json` (`fused_render.shell.storage.home_dir`).
`FUSED_RENDER_CONNECT_STORE=file` forces the file backend (tests, CI).

### oauth.py

```python
def pkce_pair() -> tuple[str, str]                      # (verifier, S256 challenge)
REDIRECT_HOST, REDIRECT_PORTS, REDIRECT_PATH = "localhost", [47823, 47824, 47825, 47826, 47827], "/callback"

@dataclass
class TokenSet: access: str; refresh: str | None; expires_at: float | None; token_type: str; scope: str | None; raw: dict

class LoopbackFlow:
    def __init__(self, *, authorize_url, token_url, client_id, redirect: dict | None = None,
                 scopes: dict | None = None, extra_authorize: dict | None = None,
                 extra_token: dict | None = None, client_secret: str | None = None, http=None): ...
    def start(self) -> str            # binds the listener (any free port when redirect.ports is None), returns the URL to open
    redirect_uri: str
    def result(self, timeout: float = 600) -> TokenSet    # blocks; raises FlowCancelled / FlowError / FlowTimeout
    def complete_from_url(self, url: str) -> None         # the paste fallback: feeds code+state as if the listener got them
    def cancel(self) -> None

class DeviceFlow:
    def __init__(self, *, device_url, token_url, client_id, scope: str | None = None, http=None): ...
    def start(self) -> dict           # {verification_uri, verification_uri_complete?, user_code, expires_in, interval}
    def result(self, timeout: float = 900) -> TokenSet
    def cancel(self) -> None

def refresh(*, token_url, client_id, refresh_token, client_secret=None, extra: dict | None = None, http=None) -> TokenSet
```

Token responses: standard JSON (`access_token`, `refresh_token`,
`expires_in`, `token_type`, `scope`); Slack's `{ok, error}` envelope
raises `FlowError(error)` on `ok: false`, and a user token under
`authed_user.access_token` (+ `authed_user.refresh_token`,
`authed_user.expires_in`) is what `TokenSet.access` carries when present.
GitHub's device token endpoint needs `Accept: application/json`. The
listener answers every hit with a small HTML page; it checks `state`
and ignores a mismatch (logged, keeps waiting). All HTTP through `httpx`.

### discovery.py

```python
@dataclass
class Discovered:
    resource: str; issuer: str
    authorization_endpoint: str; token_endpoint: str
    registration_endpoint: str | None; revocation_endpoint: str | None
    scopes: list[str]              # the 401's `scope` if given, else PRM scopes_supported, else []
    cimd_supported: bool; pkce_methods: list[str]; raw: dict

def discover(resource_url: str, http=None) -> Discovered      # per MCP auth spec §Authorization Server Discovery; raises DiscoveryError (incl. "no PKCE")
def register_dcr(d: Discovered, *, redirect_uris: list[str], client_name="Fused", http=None) -> dict   # the registration response (client_id, ...)
def client_metadata_url() -> str | None     # $FUSED_RENDER_CLIENT_METADATA_URL, else None (CIMD off)
def choose_client(d: Discovered, *, preregistered: str | None, redirect_uris, http=None) -> tuple[str, dict | None]
    # (client_id, registration|None) in spec order: preregistered → CIMD (if url set and d.cimd_supported) → DCR → raises NeedsClientId
```

### mcp_client.py

```python
class McpClient:
    def __init__(self, url: str, token: str | Callable[[], str], http=None, protocol_version="2025-06-18"): ...
    def initialize(self) -> dict        # server info; stores Mcp-Session-Id
    def list_tools(self) -> list[dict]  # raw MCP tool dicts (name, description, inputSchema, annotations?)
    def call(self, name: str, args: dict) -> dict   # {"content": [...], "isError": bool, "structuredContent"?: ...}
    def close(self) -> None
class McpError(Exception)
class McpAuthError(McpError): www_authenticate: str | None   # 401 (and GitHub's 400 "Authorization header" variant)
class McpScopeError(McpError): scope: str | None             # 403 insufficient_scope
```

Streamable HTTP: POST JSON-RPC, `Accept: application/json, text/event-stream`,
`MCP-Protocol-Version`, `Authorization: Bearer`; a `text/event-stream`
body is read until the message whose `id` matches; `Mcp-Session-Id`
is echoed back once seen; `notifications/initialized` after init.

### tools.py

```python
@dataclass
class ToolSpec: provider: str; name: str; description: str; input_schema: dict; read_only: bool; destructive: bool

def list_tools(key: str) -> list[ToolSpec]       # [] unless connected; cached 60 s per key
def call_tool(key: str, name: str, args: dict) -> dict   # {"ok": bool, "text": str, "content": list, "is_error": bool}
def invalidate(key: str) -> None
```

Sources, in order: `tools.remote` through `McpClient` with
`manager.token(key)` (a 401 refreshes once and retries; a second 401
marks the provider `reconnect`); a native module
`providers/<key>/tools.py` exposing `TOOLS: list[dict]` (`name`,
`description`, `input_schema`, `read_only`, `destructive`) and
`call(ctx, name, args) -> str | dict`, where `ctx.token`, `ctx.http`
(an `httpx.Client`), `ctx.record`. `read_only` comes from MCP
`annotations.readOnlyHint`, `destructive` from `destructiveHint`
(default `True` when annotations are absent, per the MCP spec defaults).

### manager.py

```python
def list() -> list[dict]         # one per provider: {key, name, icon: "/api/connect/<key>/icon"|None, privacy, auth: [{kind, label, available, reason?, byo?}], tools: {remote, native}, ...status(key)}
def status(key) -> dict          # {state: "disconnected"|"pending"|"connected"|"reconnect"|"error", kind?, account?, url?, user_code?, verification_uri?, error?, tools: int, store: backend()}
def login(key, kind: str | None = None, client_id: str | None = None, value: str | None = None) -> dict   # starts a flow on a daemon thread (joins a live one); `value` for api-key; returns status
def callback(key, url: str) -> dict      # paste fallback
def cancel(key) -> dict
def logout(key) -> dict                  # revoke at the vendor when a revoke endpoint is known, then store.delete; never fails on the vendor
def token(key) -> str                    # a valid access token, refreshing when expires_at is within 60 s; raises NotConnected
```

One flow per key at a time; the same lock discipline as `hf_auth.py`
(`_start_lock` around check-and-create, state readable without it).
`mcp-oauth` login: `discovery.discover` → `choose_client` → `LoopbackFlow`
with `extra_authorize={"resource": ...}` and `extra_token={"resource": ...}`
→ identity = `McpClient.initialize()`'s `serverInfo.name`; the record
stores `resource`, `client_id`, `client_registration`. `cli` login:
run the command, probe `identity_url`, store `{"kind": "cli", ...}` with
the token re-read from the command on every `token()` call (nothing
persisted but the kind and account).

### routes.py (`/api/connect`)

```
GET  /api/connect                       manager.list()
GET  /api/connect/{key}                 manager.status(key)
GET  /api/connect/{key}/icon            the pack's icon.svg (404 when none)
POST /api/connect/{key}/login           body {kind?, client_id?, value?} → manager.login
POST /api/connect/{key}/callback        body {url} → manager.callback
POST /api/connect/{key}/cancel          → manager.cancel
POST /api/connect/{key}/logout          → manager.logout
GET  /api/connect/{key}/tools           [{name, description, input_schema, read_only, destructive}]
POST /api/connect/{key}/tools/call      body {name, args} → tools.call_tool; 404 unknown key, 409 not connected
```

Every POST requires the `X-Fused: 1` header (`server.common._require_fused`).
Unknown key → 404 `{error}`. The router is included in `server/app.py`
next to `hf_auth_router`.

### runtime.js and the shared Python helper

```js
fused.connect.list()                      -> Promise<provider[]>          (GET /api/connect)
fused.connect.status(key)                 -> Promise<status>
fused.connect.login(key, {kind?, value?}) -> Promise<status>              (opens status.url in a new tab when present)
fused.connect.logout(key)                 -> Promise<status>
fused.connect.tools(key)                  -> Promise<tool[]>
fused.connect.call(key, name, args)       -> Promise<{ok, text, content, isError}>   (rejects on 409 with {code: "not_connected"})
```

`templates/shared/fused_connect.py` (stdlib only, sibling-import
`appenv` by path): `providers()`, `tools(key)`, `call(key, name, args)`
against `appenv.origin()` with `X-Fused: 1`.

### Bots bridge

`apptools.registry()` appends one `ToolRec` per `tools.list_tools(key)`
of every connected provider: `app = key`, `app_dir = "connect:" + key`,
`name`, `description`, `params` built from `input_schema.properties`
(required → no default), `spec = ToolSpec`, `tools_in_app = n`.
`apptools.available()` stays about local apps; `bot.run_tool` checks for
a connect record first and does not refuse on a missing bundled
module. `run_tool(rec, args)` for a connect record calls
`tools.call_tool` (unknown args dropped as today). `is_write(rec)` for a
connect record is `not spec.read_only`. `_deliver` emits the "Used
‹Provider› › tool" note without an app card when `app_dir` starts with
`connect:`. The APP TOOLS header gains ", and connected services
(Notion, GitHub, Slack...) whose tools run on their servers".

### Test fake vendor (`tests/_fake_vendor.py`)

A stdlib `http.server` on `127.0.0.1:<port>` runnable standalone
(`python tests/_fake_vendor.py --port N`) and as a fixture:
`/.well-known/oauth-protected-resource/mcp`, `/.well-known/oauth-authorization-server`
(with `registration_endpoint`, `code_challenge_methods_supported: ["S256"]`,
`token_endpoint_auth_methods_supported: ["none"]`), `POST /register`,
`GET /authorize` (auto-approves: 302 to `redirect_uri` with `code` and
`state`; `?approve=manual` renders a page with an Allow link instead),
`POST /token` (code grant with PKCE check, refresh grant, 60 s access
tokens so refresh is exercised), `POST /revoke`, `POST /device` +
device polling on `/token` (approves after two polls), `GET /me`
(identity), and `POST /mcp` (Bearer required; tools `echo`
(`readOnlyHint: true`), `write_note` (no annotations); JSON responses,
plus `?sse=1` to answer as `text/event-stream`).
`tests/_fake_provider/fakevendor/provider.json` declares all four
kinds against it (ports from an env var the fixture sets), so one
provider exercises every path.

### Conventions

- Python 3.12, stdlib + `httpx`; no `mcp` package import anywhere in
  `connect/`. Module docstrings in the repo's voice (what, why, what was
  rejected). Tests use `pytest`, `-n 0` is fine for one file.
- No token in any log line (`logger.debug` the key and the state, never
  the record). Diagnostics redaction: add `fused-render.connect.` to
  the redaction list if one exists in `diagnostics.py`.
- Nothing commits; the integrator commits.
