# Plan: In-App Fused Login & Setup ("Fused account", M18)

**Status:** implemented. M18a (login/logout core), M18b (env setup +
management), and M18c (docs/spec/polish) all shipped; the normative contract
now lives in **SPEC.md §27 (AC-1…AC-10)** with decisions **D111/D112** — this
file remains as the design rationale and the flow/fused-CLI research record.
One follow-up outstanding: a manual DMG smoke of the login round-trip on
macOS (the packaged-app path can't be exercised from CI/Linux).
**Goal:** a user can sign in to Fused, complete first-time environment setup, and
sign out **without ever copying a CLI command out of the app**. Today the Deploy
modal tells the user to run `fused cloud setup` / `fused cloud login` /
`fused env create` in a terminal (SPEC DP-2b, DP-16); this plan replaces those
copy-command dead-ends with in-app actions.

Prior art: the Flow app (`fusedio/flow`) already ships exactly this experience on
top of the same `fused` CLI (`app/src/server/cloud.ts`, `routes/cloud.ts`,
`ui/pages/AccountPage.tsx`, spec `spec/app/connect-fused.md`). This plan ports
its proven mechanics into fused-render's existing seams.

---

## 1. What we build on (current state)

### fused-render today

- **CLI seam already exists** — `fused_render/deploy.py`:
  - `fused_cli()` resolves the CLI from exactly two sources: `FUSED_RENDER_FUSED_BIN`
    (external, env-scrubbed) or the `fused` package importable in the server's own
    interpreter, run via the `_fused_cli.py` shim (DP-3/DP-3a).
  - `fused_cloud_logged_in()` — presence-only check of
    `~/.openfused/fused-cloud-credentials.json` (DP-2b).
  - `eligible_envs()` — reads `~/.openfused/envs.json` directly; hosted backends
    only (`fused`, `aws`) for deploy targets (DP-5).
  - `_setup_cli_hint()` — the terminal command string surfaced in the UI (plain
    `fused`, or the packaged app's bundled wrapper path).
  - One-click wheel install: `POST /api/deploy/install` + `PINNED_FUSED_REQUIREMENT`
    (currently `fused-2.9.3.post1`).
- **The copy-command UI to replace** — `frontend/src/components/DeployModal.tsx`:
  - "No hosted environments" block: *"Create one in a terminal with
    `<setup_cli> cloud setup` …"*.
  - "Not signed in" note: *"…will fail until you run `<setup_cli> cloud login`
    in a terminal"*.
- **Screen pattern to follow** — sentinel views: `/view/_prefs`, `/view/_mounts`,
  `/view/_templates` (dispatch in `frontend/src/App.tsx`; entries in the sidebar
  footer, `frontend/src/components/Sidebar.tsx`; backend feature routers as
  `APIRouter`s included in `server.py:create_app`).
- **Packaged macOS app** ships the fused package in-bundle plus a terminal wrapper
  (`FusedRender.app/Contents/Resources/bin/fused`) *specifically because* the
  one-time interactive setup couldn't be done in a modal (SPEC DP-16). This plan
  removes that limitation; the wrapper stays for power users.

### The fused CLI contract (verified against the pinned wheel generation)

All of the below is in the `fused` repo (`src/fused/agent_core/cli.py`,
`backends/fused_cloud/onboarding.py`, `pkce.py`) and present in the 2.9.3.x wheel
that fused-render pins:

- `fused cloud login --no-browser` — Auth0 Authorization-Code + PKCE. Prints the
  authorize URL to stdout, then blocks up to **300 s** on a one-shot loopback
  callback server (first free port in `localhost:3000-3099`). On success writes
  `~/.openfused/fused-cloud-credentials.json` (JWT + refresh token; the CLI
  transparently refreshes on later reads).
- **`OPENFUSED_LOGIN_RETURN_URL`** (added in fused PR #294, 2026-06-30, explicitly
  for embedding apps): if set to a loopback URL, the post-login callback page
  302-redirects the browser back to that URL — i.e. straight back into
  fused-render instead of a "return to your terminal" page.
- `fused cloud orgs` — the login/status probe. JSON:
  `{"admitted": bool, "orgs": [{"org","env","provision_state","role"}, …]}`;
  exit 1 when not logged in. (No separate `whoami` at the agent_core level.)
- `fused cloud setup [--org O --env E] --env-name NAME --no-browser [--tier T]` —
  the one-shot managed-env flow: ensure JWT → discover org/env (self-creates a
  personal org for an admitted org-less account) → poll until
  `provision_state == "ready"` → mint a data-plane API key → store it in the
  local secrets store (OS keyring) → write the `fused`-backend env into
  `envs.json`. Idempotent (re-run updates in place). Human-text output only.
- `fused cloud logout --no-browser [--env NAME]` — deletes the JWT; with `--env`
  also deletes that env's stored data-plane key.
- `fused env list|show|create|update|delete|default` — local env-store CRUD
  (`show` emits JSON; `list` is text-only).
- `--json` exists on `cloud key create`, `cloud invite create`, `cloud env delete`
  — but **not** on `login`/`setup`, so those two are driven by stdout capture +
  status polling (Flow's proven pattern).
- **Caveat:** the "local secrets store" is the OS keyring. macOS (incl. the
  packaged app) is fine (Keychain); a headless-Linux source install needs the
  `keyrings.alt` fallback (`pip install 'fused[local]'`) or setup's key-store
  step fails. Surface the CLI's error verbatim (existing DP-2b error convention).

### How Flow does it (mechanics we port)

- **Login**: spawn `fused cloud login --no-browser` with `PYTHONUNBUFFERED=1`
  (load-bearing — Python block-buffers piped stdout, without it the URL never
  arrives) and `OPENFUSED_LOGIN_RETURN_URL=<the app URL the client sent>`
  (validated loopback-only). Scrape the first `https?://…` from stdout, return it
  to the client immediately; the **client** opens it via `window.open`. The child
  keeps running, holding the callback server. One login at a time (concurrent
  starts share the in-flight one); a cancel endpoint kills the child.
- **Completion detection**: no push channel — the client polls the status
  endpoint (Flow: every 2 s while connecting) until `connected` flips.
- **Setup**: long-running (provision wait) → run as a tracked background job;
  `POST` returns `{env_name, job_id}` (202), `GET` reports
  `{state: idle|running|done|failed, job_id, detail}`; client polls and matches
  `job_id` so a stale job's `done` can't complete a newer attempt. Managed env
  naming: `fused` for the default env, `fused-<env>` otherwise.
- **Logout**: kill any in-flight login child *first* (a late browser callback
  must not silently re-write a JWT), then `fused cloud logout --no-browser`.
- The app **never touches the JWT or API key** — the CLI owns all credential
  storage; the app only reads *status*.

---

## 2. Design

### 2.1 Scope decisions (proposed)

1. **v1 targets the managed `fused` backend happy path**: sign in →
   `cloud setup` → deploy-ready. Self-hosted AWS provisioning
   (`fused env create --backend aws`, `fused infra serve`) stays a
   terminal/advanced flow — the env list shows AWS envs read-only with the
   existing guidance. (Flow draws the same line.) Follow-up if demand appears.
2. **Subprocess, not in-process Python API.** The CLI stays the single authority
   over credentials, storage format, and token refresh; the seam already
   supports an external CLI (`FUSED_RENDER_FUSED_BIN`) where in-process import
   is impossible; and it matches both deploy.py and Flow. Cost: stdout URL
   scraping + polling instead of structured returns — accepted, proven.
3. **No fused-render user accounts.** This feature manages the *fused CLI's*
   credentials on the user's own machine, for deploy targets only. The app
   itself remains unauthenticated and local-only (D3 unchanged). The
   DECISIONS.md non-goal ("authentication/user accounts") gets a clarifying
   note, not a reversal.
4. **Login is never forced.** No first-run gate; the app is fully usable signed
   out. Entry points are the Account page and the Deploy modal (where the need
   actually arises). This mirrors Flow's "a Fused account is optional" stance.

### 2.2 Backend — new `fused_render/account.py` router

New `APIRouter` mounted in `create_app` (pattern: `deploy.py`), all mutating
POSTs behind the same duplicated `X-Fused: 1` guard. Shared helpers
(`fused_cli()`, `_child_env`, `_setup_cli_hint()`, `eligible_envs()`) either
imported from `deploy.py` or — better — lifted into a small
`fused_render/fusedcli.py` module both routers import (keeps deploy ↔ account
acyclic).

| Endpoint | Behavior |
|---|---|
| `GET /api/account/status` | Composite: `{cli: {available, source, setup_cli}, logged_in, admitted, orgs: […], tier, envs: […], default_env, envs_file}`. `logged_in` starts from the cheap credentials-file presence check; when present, an optional deeper probe (`?probe=1`) shells `fused cloud orgs` (short timeout, ~10 s) for `admitted`/`orgs`/roles and to catch stale credentials. Envs come from the env store read (all backends, not just hosted, with a `hosted` flag per env). |
| `POST /api/account/login` | Body `{return_url}`. Validates `return_url` is loopback; single-flight (a second call while one is live returns the same `{authorize_url}`); spawns `fused cloud login --no-browser` with `PYTHONUNBUFFERED=1` + `OPENFUSED_LOGIN_RETURN_URL`; captures the first URL from stdout (≤10 s or 502 with stderr tail); returns `{authorize_url}`. Child is reaped on exit; its 300 s redirect timeout is the natural upper bound. |
| `POST /api/account/login/cancel` | Kills the in-flight login child, if any. |
| `POST /api/account/logout` | Kills any in-flight login first, then `fused cloud logout --no-browser`. Optional body `{env}` forwards `--env NAME` (also drop that env's data-plane key). Returns fresh status. |
| `POST /api/account/setup` | Body `{org?, env?, env_name}`. 409 if not logged in (login stays its own step so the URL flow lives in one place) or if a setup job is already running. Spawns `fused cloud setup --no-browser [--org O --env E] --env-name NAME` as a tracked background job (thread + `subprocess`, same discipline as deploy's long operations); returns `{job_id, env_name}` (202). Env-name default follows Flow: `fused` / `fused-<env>`. |
| `GET /api/account/setup` | `{state: idle\|running\|done\|failed, job_id, env_name, detail}` — `detail` carries the CLI's last output lines (progress goes to stderr) so failures surface verbatim (keyring errors, provisioning failures, …). |
| `POST /api/account/envs/default` | Body `{name}` → `fused env default NAME`. |
| `POST /api/account/envs/delete` | Body `{name}` → `fused env delete NAME --yes`. Local-pointer removal only — the UI copy must say cloud resources are not torn down. (Could slip to v1.1.) |

Implementation notes:

- **Env hygiene**: reuse deploy.py's external-CLI `PYTHONHOME`/`PYTHONPATH`
  scrubbing. Do not set `OPENFUSED_ENV` for account calls.
- **Loopback constraint is satisfied by design**: the CLI's callback server binds
  `localhost:3000-3099` on the same machine as fused-render (which is itself
  loopback-only), and the user's browser is local too. Remote/port-forwarded
  setups are out of scope, same as the rest of the app.
- **State**: nothing new persisted by fused-render. Login state lives in the
  CLI's files; the only in-memory state is the active login child and the setup
  job record (module-level, lock-guarded — same shape as Flow's
  `activeLoginStart`/setup job).

### 2.3 Frontend — new `/view/_account` sentinel + Deploy modal integration

**Account page** (`frontend/src/views/Account.tsx`, breadcrumb "Fused account",
sidebar-footer entry next to Mounts/Preferences — icon shows a signed-in accent
dot, same affordance style as the deploy dot):

State machine (top to bottom, each state renders one primary action):

1. **CLI missing** → reuse the Deploy modal's install block (one-click
   `POST /api/deploy/install` or the `FUSED_RENDER_FUSED_BIN` /
   `pip install "fused-render[fused]"` hints). Factor that block out of
   `DeployModal.tsx` into a shared component so it renders in both places.
2. **Signed out** → "Sign in to Fused" button →
   `POST /api/account/login {return_url: location.href}` →
   `window.open(authorize_url)` → "Waiting for browser sign-in…" + Cancel;
   poll `GET /api/account/status` every 2 s until `logged_in`. Because of
   `OPENFUSED_LOGIN_RETURN_URL`, the browser lands back on `/view/_account`
   after Auth0 — the page shows the signed-in state on arrival.
3. **Signed in, no managed env** → "Set up hosted environment" panel: org/env
   picker when `orgs` has multiple entries (else invisible), env-name field
   prefilled with the Flow-convention default → `POST /api/account/setup` →
   poll `GET /api/account/setup` (1.5–2 s, match `job_id`) with the CLI's
   progress `detail` streamed into the panel → success state links to Deploy.
   An admitted account with zero orgs gets the "create your workspace" variant
   (setup with no `--org/--env` self-creates a personal org).
4. **Signed in + envs** → account summary (orgs, roles, tier), environment list
   (name, backend badge, default marker, hosted/deploy-eligible flag; actions:
   set default, delete-local with confirm), and "Sign out" (with an optional
   "also remove this env's key" checkbox when a managed env exists →
   `logout {env}`).

**Deploy modal** (`DeployModal.tsx`) — replace the two copy-command blocks:

- "No hosted environments" → keep the explanation, replace the terminal
  instruction with **[Set up hosted environment]** navigating to
  `/view/_account` (modal state survives via the existing re-check on focus), or
  — stretch — run the same setup panel inline.
- "Not signed in" note → **[Sign in to Fused]** button running the exact login
  flow in place (shared `useFusedLogin()` hook: start → window.open → poll →
  refresh config). The existing `fused_logged_in` field in
  `GET /api/deploy/config` keeps working unchanged.

**Preferences → Deployments** gains a "Manage Fused account" link to
`/view/_account`.

Status freshness follows the app's existing posture: refetch on
`focus`/`visibilitychange` + while a login/setup is in flight, poll.

### 2.4 Packaged app & platform notes

- macOS DMG: the bundled fused package is the autodetected CLI source, keyring →
  Keychain — the whole flow works with zero setup. Update README/DP-16 wording:
  the terminal wrapper is no longer the *only* path for `cloud setup`/`login`.
- Source installs on Linux: if the keyring backend is unavailable, `cloud setup`
  fails at the key-store step — the job's `detail` shows the CLI's error, which
  names the `fused[local]` remedy. Add the hint to the setup panel's error copy.
- The wheel pin (2.9.3.post1) already contains everything required; no pin bump
  needed to start. Any later CLI additions (e.g. `--json` on `setup`) are
  upstream follow-ups, not blockers.

### 2.5 Docs/spec obligations (repo convention)

- **SPEC.md**: new §27 "Fused Account — In-App Login & Setup (M18)" with
  requirement IDs (AC-1…): endpoint table, the single-flight login rule, the
  return-URL contract, the setup job model, the signed-out-is-fine invariant.
  Amend **DP-2b** (guidance strings → action buttons; the config field stays)
  and **DP-16** (wrapper demoted from "the way" to "a power-user escape hatch").
- **DECISIONS.md**: new D-entries — (a) in-app Fused account management &
  scope line (managed backend only; AWS stays terminal), (b) subprocess-over-
  in-process rationale, (c) clarification of the "no authentication/user
  accounts" non-goal (unchanged for the app itself; Fused-cloud sign-in is a
  deploy-target concern).
- **README.md**: rewrite the "Deploy to a hosted URL" bullet about the terminal
  wrapper; document the Account page.

---

## 3. Implementation order

Three PR-sized milestones, each shippable:

1. **M18a — login/logout core.** `fusedcli.py` extraction; `account.py` router
   with `status`/`login`/`login/cancel`/`logout`; Account page states 1, 2 and a
   minimal signed-in summary; Deploy modal "Sign in" button; sidebar entry.
2. **M18b — environment setup & management.** Setup job endpoints + panel
   (state 3); env list with set-default/delete-local (state 4); Deploy modal
   "Set up hosted environment" replacement; org picker + self-serve-org variant.
3. **M18c — polish & docs.** SPEC §27 + DP-2b/DP-16 amendments, D-entries,
   README; packaged-app smoke test (DMG login round-trip); keyring error copy;
   signed-in dot on the sidebar icon.

## 4. Testing

- **Backend (pytest)**: drive everything through a **stub CLI** via
  `FUSED_RENDER_FUSED_BIN` (existing pattern in `tests/test_deploy.py`). Stub
  behaviors: `cloud login --no-browser` prints a URL, waits, then writes a fake
  credentials file (honoring `OPENFUSED_FUSED_CLOUD_CREDENTIALS` so tests stay
  hermetic); `cloud orgs` emits canned JSON / exit 1; `cloud setup` sleeps then
  writes an env into a temp `OPENFUSED_ENVS_FILE`; `cloud logout` deletes the
  credentials file. Cover: URL capture + timeout, single-flight login, cancel,
  logout-kills-login, setup job lifecycle incl. failure `detail`, 409s,
  loopback validation of `return_url`, status composition with/without probe.
- **Frontend**: `npm run typecheck` gate as usual; component logic kept thin
  (the state machine keys off the status payload). Flow's `cloud.test.ts` /
  `AccountPage.test.tsx` serve as reference vectors.
- **Manual checklist** (per README's verification style): source venv on macOS
  and Linux, packaged DMG; sign in → browser round-trip lands back on the
  Account page; setup to a fresh managed env; deploy from the modal end-to-end;
  sign out; sign in again (idempotent setup).

## 5. Risks & edge cases

| Risk | Mitigation |
|---|---|
| Stdout buffering swallows the authorize URL | `PYTHONUNBUFFERED=1` on the child (Flow's hard-won fix) |
| User abandons browser sign-in | CLI child self-terminates at 300 s; UI offers Cancel; status poll simply never flips |
| Ports 3000–3099 all busy | CLI errors out; stderr tail surfaces in the modal/page verbatim |
| Late callback after logout re-writes a JWT | Logout kills the in-flight login child first |
| Stale/expired credentials file reads as "signed in" | Presence check is optimistic by design; `?probe=1` (`cloud orgs`) is authoritative; deploy-time CLI errors remain the final authority (existing DP-2b posture) |
| Headless-Linux keyring failure during setup | CLI error passthrough + `fused[local]` hint in the panel copy |
| Two fused-render instances both start logins | Harmless — last completed login wins; both read the same credentials file |
| `cloud orgs` probe hangs offline | Short timeout; degrade to presence-only status |
