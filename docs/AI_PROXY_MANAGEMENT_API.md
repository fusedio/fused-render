# CLIProxyAPI Management API — verified contract

Empirically probed against a real **CLIProxyAPI v7.2.90** binary (Homebrew build,
`darwin_aarch64`) on 2026-07-28, by running an isolated instance on port 18317
with a management `secret-key` under our control. Every row below was observed on
the wire, not read from documentation — the project's `docs/MANAGEMENT_API.md` is
a 404 on `main`, so this file is our source of truth for the client we write.

Upstream: <https://github.com/router-for-me/CLIProxyAPI> (Go, MIT).

## Auth

Base path `/v0/management`. Every request needs the management key, **including
from localhost** (`remote-management.allow-remote: false` only blocks non-local
callers; it does not make localhost unauthenticated). Two accepted forms — both
verified 200:

```
Authorization: Bearer <secret-key>
X-Management-Key: <secret-key>
```

A `?key=<secret-key>` query param is **not** accepted (401). Missing key →
`401 {"error":"missing management key"}`. An empty `secret-key` in config
disables the whole management surface (404 on every route).

The config file's `secret-key` is hashed on startup if given in plaintext, so the
plaintext is only knowable to whoever wrote the config — which is why we generate
it ourselves rather than trying to read an existing one.

## Routes

| Method | Path | Purpose | Response |
|---|---|---|---|
| GET | `/v0/management/anthropic-auth-url` | Begin Claude OAuth | `{state, status:"ok", url}` |
| GET | `/v0/management/codex-auth-url` | Begin ChatGPT/Codex OAuth | `{state, status:"ok", url}` |
| POST/GET | `/v0/management/oauth-callback` | Hand back the code | `{status:"ok"}` |
| GET | `/v0/management/get-auth-status?state=` | **Poll the real login result** | `{status:"wait"\|"ok"\|"error", error?}` |
| DELETE | `/v0/management/oauth-session?state=` | Cancel a pending login | `{status:"ok", cancelled}` |
| GET | `/v0/management/auth-files` | List credentials | `{files:[…]}` |
| DELETE | `/v0/management/auth-files?name=<file>` | Remove one credential | `{status:"ok"}` |
| PATCH | `/v0/management/auth-files/status` | Enable/disable a credential | `{status:"ok", disabled}` |
| GET | `/v0/management/config` | Effective config | config object |
| GET | `/v0/management/request-log` | Request-log toggle state | `{request-log:bool}` |

Confirmed **absent** in 7.2.90 (404), so don't build on them: `version`, `usage`,
`login-status`, `auth-status` (the real path is `get-auth-status` — an easy
miss), `<provider>-auth-callback`, `claude-login`.

Version/build info is not a JSON route; every management response carries
`X-CPA-VERSION` / `X-CPA-COMMIT` / `X-CPA-BUILD-DATE` headers. There is no
dedicated health route — a cheap authenticated GET doubles as a reachability
check. No SSE or websocket for management state, so status is poll-only.

Other providers also expose `…-auth-url` (`kimi`, `xai`, `antigravity`); `gemini`
and `qwen` do not. Kimi and xAI return an extra `{flow:"device", expires_in,
user_code}` — a device-code flow. Anthropic and Codex return **no** `flow` field,
i.e. they are ordinary redirect flows.

## The login flow

`GET …/anthropic-auth-url` returns a `state` plus an authorization `url` whose
`redirect_uri` is `http://localhost:54545/callback` (Codex uses `1455`). Probing
with `lsof` showed the server **does not open a listener** on that port when the
URL is issued — so the redirect target is not served by the proxy in this flow.
The caller opens `url` in a browser, the user approves, and the browser lands on
that (non-listening) callback URL carrying `?code=…&state=…`; the `code` is then
handed back to the proxy:

```
POST /v0/management/oauth-callback   {"state": "<from auth-url>", "code": "<from callback>"}
```

Notable and load-bearing: this returns `200 {"status":"ok"}` **even for a
deliberately bogus code**, so a 200 means "the code was recorded", not "logged
in". The exchange is deferred — `oauth-callback` only writes the code to a
`.oauth-<provider>-<state>.oauth` file in the auth dir, and a goroutine spawned
back at `…-auth-url` time picks it up and does the real token exchange. A body
with `state` but no `code` is `400 code or error is required`; an unknown or
expired state is `404`.

`POST` also accepts a whole `redirect_url` instead of a picked-apart `code` — the
server parses its query string to fill `code`/`state`/`error`. (That field is
POST-JSON only; the GET form doesn't read it.) Posting `error` instead of `code`
aborts the flow with a specific message.

### Confirming a login: `get-auth-status`

The result channel is **`GET /v0/management/get-auth-status?state=<state>`**,
polled after handing back the code:

| Response | Meaning |
|---|---|
| `{"status":"wait"}` | Exchange still in flight |
| `{"status":"ok"}` | Credential saved — it is now in `auth-files` |
| `{"status":"error","error":"…"}` | Exchange or save failed, with a readable reason |

Verified: a deliberately bogus code yields `{"status":"error","error":"Failed to
exchange authorization code for tokens"}` within about a second. This is strictly
better than inferring failure from a timeout on the credential listing, which is
what an earlier draft of this design did — poll **this**, not `auth-files`.

A state is effectively single-use: completing it shrinks its TTL to a minute, and
replaying `oauth-callback` on a used state is a `409`. Pending states live in an
in-memory map with a 30-minute TTL, purged lazily on access — so a state does not
survive a proxy restart, and an abandoned login expires on its own. A pending
login can be cancelled outright with `DELETE …/oauth-session?state=`.

### `?is_webui=1` — deliberately not used

Passing `is_webui=1` to an auth-url route makes the proxy bind the callback port
itself and 302 onward to its bundled control-panel SPA. Verified working, but
rejected for us on two counts: it binds `*:54545` (**all interfaces**, not
loopback), and it forwards into a control panel we switch off with
`disable-control-panel`. Our own loopback-only listener is both tighter and
fewer moving parts.

### Callback ports are fixed — and free for us to bind

The `redirect_uri` is hardcoded per provider and **cannot be moved**: passing
`port`, `callback_port`, `oauth_callback_port`, `redirect_uri`, or `no_browser`
as query params to the auth-url route leaves it unchanged.

| Provider | Redirect URI |
|---|---|
| Claude (`anthropic`) | `http://localhost:54545/callback` |
| ChatGPT (`codex`) | `http://localhost:1455/auth/callback` |

Since the proxy does not itself listen there, **our app can bind those ports for
the duration of a login** and capture the `code` out of the browser's redirect
directly, then hand it to `oauth-callback`. That is what makes a one-click
"Connect account" possible: without it, the browser would land on a dead port
and the user would have to copy a code out of a failed page's URL bar.

The ports being fixed also means two logins cannot run concurrently, and a login
cannot proceed if something else already holds the port (notably the user's own
separately-installed proxy mid-login). Both are states the UI has to report
rather than hang on.

Requested scopes, for the record: Claude asks for `user:profile user:inference
user:sessions:claude_code …`; Codex asks for `openid email profile
offline_access`.

## Credential listing shape

`GET /v0/management/auth-files` → `{"files": [...]}`, one entry per credential:

```json
{
  "name": "claude-user@example.com.json",
  "id": "claude-user@example.com.json",
  "path": "/…/auths/claude-user@example.com.json",
  "provider": "claude",
  "account": "user@example.com",
  "account_type": "oauth",
  "email": "user@example.com",
  "label": "user@example.com",
  "disabled": false,
  "priority": 0,
  "failed": 0,
  "auth_index": "72390572acff0657",
  "created_at": "2026-07-28T19:19:14.91903+05:30",
  "modtime": "2026-07-28T19:19:14.918860782+05:30",
  "recent_requests": [{"time": "16:00-16:10", "success": 0, "failed": 0}]
}
```

`provider` is the discriminator we filter the preferences list on (`claude`,
`codex`). `name` is the handle `DELETE` takes. Deleting with a missing or
unknown name is a `400 {"error":"invalid name"}`.

The proxy runs a file watcher over `auth-dir`, so a credential dropped in by any
means is picked up within ~1–2 s without a restart; that watcher latency is why
the post-login poll needs a couple of seconds of tolerance.

## On-disk credential files

Written by the proxy into `auth-dir`, one JSON per account, named
`<provider>-<email>.json`. Fields (Claude): `type`, `email`, `access_token`,
`refresh_token`, `id_token`, `expired`, `last_refresh`, `disabled`, `priority`.
Codex adds `account_id`. These hold live OAuth tokens for the user's own
subscription — treat the directory as secret material: never log its contents,
never copy it into a bundle, and keep it under the app's state dir with the same
care as other credentials.

## Version skew

Probed only against 7.2.90. The route surface is not covered by any published
doc, so a client written against it should degrade gracefully rather than assume:
an unexpected 404 on a management route should surface as "this proxy build
doesn't support account management from the app", not a crash.
