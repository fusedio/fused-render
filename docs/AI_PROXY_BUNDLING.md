# Bundling the AI proxy + managing AI accounts — design

Follows on from `fused.ai()` (SPEC RH-11), which relays to an OpenAI-compatible
proxy the **user** installs and runs. That leaves the feature inert until someone
has independently found CLIProxyAPI, installed it, run a login per provider, and
kept it running. This design ships the proxy inside the app, supervises it, and
turns "connect a Claude or ChatGPT account" into a button in Preferences.

The wire contract we build against is in
[`AI_PROXY_MANAGEMENT_API.md`](AI_PROXY_MANAGEMENT_API.md) — established by
probing a real v7.2.90 binary, because upstream publishes no doc for it.

## Why this is a small change

CLIProxyAPI is a single static Go binary under MIT — the same shape as **rclone**,
which this repo already bundles for mounts. So nearly every piece has an in-repo
precedent to copy rather than invent:

| Need | Existing precedent |
|---|---|
| Download + checksum-verify a pinned binary at build time | `scripts/build_dmg.sh:146-184` |
| Stage it into the .app and smoke-test it | `scripts/build_dmg.sh:449-498` |
| Same for Linux / Windows payloads | `scripts/build_linux_appimage.sh:90-146`, `supervisor/paths.py:217-222` |
| Resolve binary at runtime (env → bundled → PATH) | `shell/mounts.py:956-978` `rclone_bin()` |
| Spawn a local daemon on a random port, auth it, health-poll, persist state | `shell/mounts.py:1038-1108` `ensure_rcd()` |
| Tear it down killing only a pid proven to be ours | `shell/mounts.py:1117-1199` |
| Sign a bundled Mach-O for notarization | `scripts/build_dmg.sh:599-620` (enumerates every nested Mach-O) |
| A Preferences section | `frontend/src/views/Preferences.tsx` `prefs-section` idiom |
| Listing external connected things with a destructive action | `frontend/src/views/Account.tsx` environments + Revoke |

## Runtime: `fused_render/shell/ai_proxy.py`

A new module mirroring the rcd half of `shell/mounts.py`.

**Binary resolution** — `ai_proxy_bin()`, three tiers exactly like `rclone_bin()`:
`FUSED_RENDER_AI_PROXY_BIN` (set by the supervisor in packaged Windows/Linux
builds) → the macOS bundle path under `Contents/Resources/bin/` when
`sys.frozen == "macosx_app"` → `shutil.which`. The PATH tier is what keeps an
existing user-run install working untouched.

**Lifecycle** — `ensure_ai_proxy()` spawns lazily on the first `fused.ai()` call
rather than at app launch: AI calls are occasional, and an idle 20 MB Go process
in every session is rent we don't need to pay. Under a `threading.Lock` like
`_rcd_lock`, since the relay is async and two concurrent calls would otherwise
both spawn. It generates a config under the app state dir with:

- an ephemeral port we pick ourselves (bind-0 then close, as `ensure_rcd` does)
- `host: "127.0.0.1"` — loopback only, never the upstream default of all interfaces
- one random `api-keys` entry, so the relay authenticates and nothing else local can drive it
- a random `remote-management.secret-key` with `allow-remote: false`
- `auth-dir` under the app state dir, holding the OAuth tokens
- `disable-control-panel: true` — we drive the API ourselves and don't want it fetching a web panel from GitHub

Then health-polls `/v1/models` to a deadline before reporting ready, writes
`{port, pid, spawner_pid, keys, log}` to a state file, and rotates its log.
Teardown reuses the `stop_local_rcd()` shape: SIGTERM→SIGKILL, refuse to signal
a pid not confirmed ours, respect a spawner that is still alive.

**Relay change** — `server.py:_ai_relay` currently posts to
`ai_base_url()/v1/chat/completions` with no credentials. It gains: call
`ensure_ai_proxy()` first, and send `Authorization: Bearer <generated key>`. The
`fused.ai()` JS surface and its error `type`s do not change, so RH-11's authored
contract is untouched. When `ai_base_url` is overridden to a user's own proxy we
skip supervision entirely and behave exactly as today.

**Config precedence**, most specific first: an explicit `FUSED_RENDER_AI_BASE_URL`
or `ai_base_url` pref means "the user is pointing us somewhere deliberate" — use
it, supervise nothing. Otherwise use the bundled proxy. A build with no bundled
binary and no override falls back to probing the default port, so a dev checkout
against a homebrew install keeps working.

## Accounts: `/api/ai/accounts`

Three shell routes, mutations behind the existing `_require_fused` header guard
(`prefs.py:54`) so a previewed page can never touch credentials:

| Route | Purpose |
|---|---|
| `GET /api/ai/accounts` | List connected accounts + proxy status |
| `POST /api/ai/accounts/connect` | Begin a login; returns the authorization URL |
| `DELETE /api/ai/accounts/{name}` | Disconnect one account |

`GET` maps the proxy's `auth-files` listing down to what the UI needs —
`{provider, email, disabled, expired}` — filtered to `claude` and `codex`, plus
whether the proxy is running at all. It must never return token material.

### The login flow

The probe established that OAuth is fully drivable over HTTP, and that the fixed
callback ports (`54545` for Claude, `1455` for Codex) are unoccupied — so the app
binds one for the duration of a login and reads the `code` straight out of the
browser redirect. No copy-paste, no CLI subcommand.

1. `POST …/connect {provider}` → we call `<provider>-auth-url`, get `{state, url}`
2. we bind the provider's callback port (loopback only) with a one-shot handler
3. we return `url` to the frontend, which opens it — **the browser is always the
   client's job**; the backend only ever returns a URL string, matching how
   `account.py` handles Fused sign-in
4. the user approves; the browser hits our handler; we capture `code` and serve a
   short "return to FusedRender" page
5. we `POST oauth-callback {provider, state, code}`
6. we poll `GET get-auth-status?state=` until `ok` or `error`

Step 6 is the part an earlier draft got wrong. `oauth-callback` answers 200 even
for a bogus code — it only records the code, and the exchange happens in a
goroutine — so its status proves nothing. `get-auth-status` reports
`wait`/`ok`/`error` with a real message ("Failed to exchange authorization code
for tokens"), which means a failed login can be reported as a failure instead of
being inferred from a timeout.

Failure modes the UI names rather than hangs on: callback port already held (a
concurrent login, or the user's own proxy mid-login), user abandons the browser
(timeout; release the port and `DELETE oauth-session`), exchange rejected
(`get-auth-status` says so). Only one login at a time — the fixed ports make that
structural, so a second concurrent attempt is rejected outright rather than
queued. Cancellation is a real operation, not just closing the tab, since the
proxy holds pending state for 30 minutes.

Because the whole surface is poll-only (no SSE/websocket), the frontend follows
the existing `useFusedLogin` cadence in `lib/account.ts`: open the URL, poll every
couple of seconds, and offer Cancel while waiting.

One deliberate asymmetry with `account.py`: `/connect/cancel` clears the tracked
attempt unconditionally, including one that settled a moment earlier, so
`/connect/status` afterwards reports `idle` rather than what actually happened.
It is therefore useless as a post-cancel reconciliation read — unlike
`getAccountStatus`, which can still answer "did it finish?". Verified that this
only forgets the *outcome*: a credential written in the race window survives
untouched, so **the account listing is the reconciliation source of truth after a
cancel**, and that is what the frontend re-reads. Keeping cancel unconditional is
the simpler contract (it always means "stop tracking this"), and the listing
answers the only question that matters afterwards — is there a new account or
not.

## Preferences UI

Follows the house idiom exactly — `<section className="prefs-section">` with an
`<h2>`, a `.deploy-muted` explainer, per-control `busy`/`error` state, and
`ErrorBanner` on failure. There is no shared Section/Row/Toggle component
library: every section in `Preferences.tsx` is a bespoke function component
sharing only CSS classes, so a new one matches by following that shape rather
than by importing anything. Styles are plain global CSS in `frontend/src/shell.css`
(prefs classes from ~:3594) — no CSS modules, no Tailwind.

The connect flow reuses an existing hook rather than inventing one: `lib/account.ts`'s
`useFusedLogin` is already exactly this shape (begin → `window.open` the URL →
poll every 2s → surface "waiting for the browser" with a Cancel, and reconcile a
completion that races the cancel). The AI version is that pattern pointed at our
routes. `views/Account.tsx`'s environments table is the model for the account list
with a per-row destructive action, including the `window.confirm` before removal.

Note `PUT /api/prefs` returns the **whole fresh `Prefs`** and each control bubbles
it up via `onChange`, so there is no client-side merge; and every mutating call
must send `X-Fused: 1` or the server 403s (`mutateJson` in `lib/api.ts` does this).
Since accounts are an external OAuth surface rather than a simple stored toggle,
they get their own router module in the shape of `account.py` instead of new keys
on `/api/prefs` — `prefs.py`'s `ai_base_url()` stays the read-only plumbing it is.

Scope is **Claude and ChatGPT only** for now, though the proxy also speaks Gemini,
Kimi, xAI and Antigravity — the two that matter are the two most users already
subscribe to, and each provider added is another OAuth flow to verify.

**Placement: its own tab** (decided), a third beside "Render preferences" and
"Fused account", following how `AccountPanel` occupies a whole tab. Accounts need
more room than a settings row: per-account provider, email, expiry and disabled
state, plus a login that involves an external browser round-trip. Unlike the
Fused account tab, this one is always offered — there is no enabling pref to gate
it on, and a user with no accounts yet is exactly who the tab is for.

## Build

Per platform: pin a version, download the release archive, verify against the
published `checksums.txt`, extract, stage next to `rclone`, and smoke-test the
staged binary (`--help`) before it ships — the rclone step's fail-loud discipline.
Assets are ~19–21 MB per platform, immaterial against a bundle that already
carries numpy/pandas/pyarrow.

macOS notarization needs no new work: the signing loop enumerates every nested
Mach-O by magic bytes and signs each with the hardened runtime, so a new binary
under `Contents/Resources/bin/` is picked up automatically. Still worth
confirming on the first signed build that a Go binary is happy with the existing
entitlements — Go binaries have historically needed care under hardened runtime.

Windows needs no `installer.iss` change: the whole `python\` tree is staged by a
recursive wildcard, which is already how `rclone.exe` arrives.

Known gap, deliberately not closed here: `ActivatePayload`'s "payload is
incomplete" check (`installer.iss:247-255`) verifies `python.exe`, `uv.exe`,
`win32job.pyd` and friends but **not** `rclone.exe`, and so not
`cli-proxy-api.exe` either. A truncated build would therefore install and only
fail later, at the point of use. Widening that check is worth doing for both
binaries at once rather than adding ours alone, so it belongs in its own change.

## Open questions

- **Terms of service — known and accepted (2026-07-28).** CLIProxyAPI
  authenticates as a CLI client against Claude / ChatGPT using subscription
  OAuth, not sanctioned API keys, and the proxy even carries a "cloak" feature
  that disguises requests as Claude Code. Shipping that bundled, with a button
  inviting the user to log in, is plausibly contrary to those providers' terms —
  a materially larger exposure than a user independently choosing to run the same
  tool. The owner reviewed this and chose to proceed; it is recorded here so it
  is not later rediscovered as an oversight. Revisit before any public launch or
  distribution beyond current users.
- **Version pinning vs. drift.** The management surface is undocumented upstream
  and was verified against exactly one build. Pin hard, and degrade gracefully:
  an unexpected 404 should read as "this proxy build can't manage accounts from
  the app", not a crash.
- **Reusing an existing auth dir.** A user with a homebrew install already has
  authed accounts in `~/.cli-proxy-api`. Pointing at it would inherit their logins
  for free but also let the app mutate a directory it doesn't own. Defaulting to
  our own state dir is the safer starting position.
