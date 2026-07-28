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
| POST/GET | `/v0/management/oauth-callback` | Finish a login | `{status:"ok"}` |
| GET | `/v0/management/auth-files` | List credentials | `{files:[…]}` |
| DELETE | `/v0/management/auth-files?name=<file>` | Remove one credential | `{status:"ok"}` |
| GET | `/v0/management/config` | Effective config | config object |
| GET | `/v0/management/request-log` | Request-log toggle state | `{request-log:bool}` |

Confirmed **absent** in 7.2.90 (404), so don't build on them: `version`, `usage`,
`login-status`, `auth-status`, `<provider>-auth-callback`, `claude-login`.

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

Notable and load-bearing for our design: this returns `200 {"status":"ok"}`
**even for a deliberately bogus code**, so a 200 means "accepted for processing",
not "logged in". Token exchange is deferred. An unknown or expired `state` is the
one thing it does reject up front (`404 unknown or expired state`), and a body
with `state` but no `code` is a `400 code or error is required`.

Consequence: **success must be confirmed by observing `auth-files` grow a new
entry**, not by the callback's status code. Our client polls the listing.

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
