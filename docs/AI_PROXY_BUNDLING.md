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
2. we bind the provider's callback port with a tiny one-shot handler
3. we hand `url` back to the frontend, which opens it in the user's browser
4. the user approves; the browser hits our handler; we capture `code` and serve a "return to FusedRender" page
5. we `POST oauth-callback {state, code}`
6. **we poll `auth-files` for a new entry** — because `oauth-callback` answers 200 even for a bogus code, so its status proves nothing

Step 6 is the part that must not be shortcut. Success is a credential appearing;
anything else within the timeout is a failure the UI reports.

Failure modes the UI has to name rather than hang on: callback port already held
(a concurrent login, or the user's own proxy mid-login), user abandons the browser
(timeout, release the port), token exchange rejected (no credential appears).
Only one login may be in flight at a time — the fixed ports make that structural,
so the endpoint should reject a second concurrent attempt outright.

## Preferences UI

A new **"AI accounts"** section following the house idiom exactly — a
`<section className="prefs-section">` with an `<h2>`, a `.deploy-muted` explainer,
per-section `busy`/`error` state, and an `ErrorBanner` (`Preferences.tsx`). It
lists connected accounts with provider and email, a Connect button per provider,
and Disconnect per account behind a `window.confirm` like Account.tsx's Forget.

Scope is **Claude and ChatGPT only** for now, though the proxy also speaks Gemini,
Kimi, xAI and Antigravity — the two that matter are the two most users already
subscribe to, and each provider added is another OAuth flow to verify.

Whether this lives as a section in the render tab or its own tab depends on
whether it should appear before any account is connected; a section is the
smaller change and is the starting assumption.

## Build

Per platform: pin a version, download the release archive, verify against the
published `checksums.txt`, extract, stage next to `rclone`, and smoke-test the
staged binary (`--help`) before it ships — the rclone step's fail-loud discipline.
Assets are ~19–21 MB per platform, immaterial against a bundle that already
carries numpy/pandas/pyarrow.

macOS notarization needs no new work: the signing loop enumerates every nested
Mach-O by magic bytes and signs each with the hardened runtime, so a new binary
under `Contents/Resources/bin/` is picked up automatically. Worth an explicit
check that a Go binary accepts the existing entitlements, since Go binaries have
historically needed care with hardened runtime.

## Open questions

- **Terms of service.** CLIProxyAPI authenticates as a CLI client against Claude /
  ChatGPT using subscription OAuth, not sanctioned API keys. A user choosing to run
  that themselves is one risk posture; an app shipping it bundled and inviting the
  user to log in is a different and larger one. This is a judgement call for the
  project owner, not an engineering task, and it gates the whole branch.
- **Version pinning vs. drift.** The management surface is undocumented upstream
  and was verified against exactly one build. Pin hard, and degrade gracefully:
  an unexpected 404 should read as "this proxy build can't manage accounts from
  the app", not a crash.
- **Reusing an existing auth dir.** A user with a homebrew install already has
  authed accounts in `~/.cli-proxy-api`. Pointing at it would inherit their logins
  for free but also let the app mutate a directory it doesn't own. Defaulting to
  our own state dir is the safer starting position.
