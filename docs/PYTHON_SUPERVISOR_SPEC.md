# Python supervisor — experiment spec

Status: experiment. Goal is to decide whether the Windows desktop supervisor can
be pure Python (removing the Rust toolchain) without losing the guarantees the
Rust version already ships.

The **behavioral contract** is the working Rust supervisor — the Windows desktop
supervisor implementation and its architecture doc, tracked separately and not
yet on `main`. Match its behavior; do not invent new semantics.

## Why this experiment

Most of the supervisor is portable orchestration Python already does on macOS
(`fused_render/app.py`). The Rust exists for two things: the Job Object
process-tree kill, and a windowless owner decoupled from the interpreter it
supervises. This experiment tests whether Python + `pywin32` can hold those two
guarantees. If it can, we drop a toolchain; if it can't (esp. no-orphans on hard
crash), we keep Rust.

## Design (option B — full Python)

- A **thin native launcher** `FusedRender.exe` remains the single Start-Menu /
  Explorer target and AppUserModelID owner. It only execs
  `pythonw.exe -m fused_render.win_supervisor <args>` — no logic. (A few lines
  of Rust/C, or a generated stub. This is the ONLY native piece left.)
- `fused_render/win_supervisor.py` (new) is the surviving process. It owns:
  job object, tray, single-instance, IPC, startup toggle, and spawns the server
  as a Job-assigned child (mirroring the Rust child launch: `pythonw -I -m
  fused_render.cli serve --no-browser --port <p>`).
- Do **not** split responsibilities across processes — tray, single-instance,
  and job ownership share one process, else you add an IPC seam.

## Must-match behaviors (port from the Rust modules)

1. **Job Object** (`job.rs`): `CreateJobObject` + `SetInformationJobObject`
   with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; create the child **suspended**
   (`win32process.CreateProcess`, not `subprocess.Popen` — need the thread
   handle), `AssignProcessToJobObject`, then `ResumeThread`. Strip `PYTHONHOME/
   PYTHONPATH/PYTHONSTARTUP/PYTHONUSERBASE/PYTHONINSPECT` from the child env;
   `CREATE_NO_WINDOW`. Use `pywin32` `win32job`/`win32process`.
2. **Single-instance + IPC** (`instance.rs`): named mutex `Local\FusedRender.
   Supervisor.v1.<sid>`; message-mode named pipe `\\.\pipe\FusedRender.
   Supervisor.v1.<sid>` with SDDL DACL `D:P(A;;GA;;;SY)(A;;GA;;;<sid>)`.
   Commands: `Open(path)`, `OpenHome`, `ShutdownForUpgrade`. Secondary launches
   forward to the primary and exit; on `ShutdownForUpgrade` the secondary waits
   for the primary mutex to release. Use `win32event`, `win32pipe`,
   `win32security`.
3. **Readiness + shutdown** (`supervisor.rs`): ephemeral localhost port; 256-bit
   launch token (`secrets.token_urlsafe`); gate readiness on `/api/config`
   returning the expected instance id + token; graceful shutdown via
   `POST /api/desktop/shutdown` with the token header, Job-close as the backstop.
   These already exist in `app.py` — reuse.
4. **Tray** (`tray.rs`): Open, Open file, status/port, Open logs, Default apps,
   Start at sign in (checkbox), Exit (confirm). Candidate lib: `pystray`
   (evaluate windowless behavior + message loop). File dialog: bundled tkinter
   `filedialog` (tcl/tk is already in the payload).
5. **Startup toggle** (`startup.rs`): HKCU `...\Run` value `FusedRenderDesktop`
   = `"<launcher-exe>" --startup`. Registry failure must NOT crash the
   supervisor (log + revert checkbox). Uninstaller must sweep this on the
   quoted-exe prefix (see the installer.iss fix on the PR branch).
6. **State isolation** (`paths.py` on the PR branch): pass the same
   `FUSED_RENDER_*` env to the child (`HOME/CACHE_DIR/RUNTIME_DIR/TEMP_DIR/
   LOG_DIR`, `DESKTOP_INSTANCE_ID/TOKEN`, OPENFUSED/RCLONE/UV/CLAUDE/DUCKDB).

## Packaging

- Add `pywin32` (+ tray lib) to the bundled env.
- `build_windows_installer.ps1` stops building the Rust supervisor; builds/copies
  the launcher stub instead. Everything else (relocatable CPython + wheel +
  DuckDB extensions + Inno) is unchanged.

## Acceptance gates (same matrix already run for Rust — the go/no-go)

- Build installer end-to-end; install silently (per-user, no admin).
- App serves; port does not clash with a dev server on 1777.
- **No-orphans on hard crash:** `taskkill /F` the supervisor → the entire
  pythonw + daemon tree must die (this is the Job Object's whole point and the
  riskiest thing to reproduce in Python — if it fails here, keep Rust).
- Clean tray Exit and `--shutdown-for-upgrade`: no orphan processes, port freed.
- Uninstall sweeps app dir + cache/runtime/temp/logs + ProgIDs + the Run value;
  preserves `state`; leaves wheel ProgIDs untouched.
- No visible console for any subprocess path.

## Decision

If all gates pass (especially no-orphans-on-crash), open a migration PR to
replace the Rust supervisor. If any fail, record the failure here and keep the
Rust supervisor as the shipping path.

## Software updates (auto-check + auto-download, user-approved install)

Goal: an installed build checks for a newer version on its own, downloads and
verifies it silently in the background, and then prompts the user — the
**install is always one click**, never silent. No surprise restarts: the app
is only ever replaced after the user says yes.

The genuinely hard parts already ship in `scripts/windows/installer.iss`: it
stops the running app (`PrepareToInstall` → `ShutdownSupervisor` execs the
installed exe with `--shutdown-for-upgrade`), does the atomic payload swap
(`ActivatePayload`: `next` → `payload`, keep `previous` for rollback), and
refuses a downgrade (`InitializeSetup`). So an "update" is just: fetch the new
`setup.exe` and run it. Reuse it as the update vehicle; do not build a second
install path.

**Approach: roll-your-own manifest check, not WinSparkle.** Even with
background checking, WinSparkle's value (its cadence/skip-version state and
dialog stack) doesn't outweigh its cost — a native DLL, ctypes callback
lifetime hazards, appcast XML, and a shutdown-ownership negotiation with our
installer. The homegrown path is ~150 lines reusing idioms already in
`supervisor.py` (off-thread `MessageBoxW` per `_confirm_exit`, the `_spawn_*`
worker-thread pattern) plus one daemon timer thread.

1. **Client flow** (`fused_render/win_supervisor/update.py`). Two entry points,
   one shared `_check_lock` (one check at a time, whichever fires):
   - **Automatic** — `start_auto_checks(paths)`, called once from
     `supervisor.run()` after the tray starts, spawns a daemon thread that
     waits `_STARTUP_DELAY_S` (so it never competes with launch) then loops
     every `_CHECK_INTERVAL_S` (6 h). Each tick is **silent** on "up to date"
     and on transient network errors (logged only, never a dialog), so it can't
     nag. `FUSED_RENDER_NO_AUTO_UPDATE` disables it.
   - **Manual** — the `TrayAction.CHECK_UPDATES` tray item runs the same flow
     on demand, but reports its result either way ("up to date" / an error
     dialog).
   - The ed25519 signature is verified in `_fetch_manifest`, **before** the
     `version` is trusted for the "up to date"/prompt decision — a CDN/bucket
     compromise can't forge a version to suppress or fake an update. When
     `manifest.version` > `fused_render.__version__`: download the installer to
     `%TEMP%` (**not** `Desktop\temp` — the installer's `[InstallDelete]` wipes
     that), streaming it under a size cap while confirming its SHA-256 equals
     the signed `manifest.sha256` (mismatch → refuse, report/log, never run an
     unverified exe), then `MessageBoxW` MB_YESNO "FusedRender X.Y.Z is ready to
     install…". On Yes, **re-hash the staged file one last time** (it sat in
     `%TEMP%` while the prompt was open) and only then `os.startfile(installer)`
     and return; the installer takes over. A declined version is remembered in
     `_prompted_versions` so the background loop won't re-prompt it this session
     (a restart or manual check offers it again); an accepted-but-failed launch
     is not remembered, so it retries.
2. **Manifest** — `latest.json` published next to the installer on the CDN
   (`https://d2ic19jpchjovp.cloudfront.net/fused-render-windows/latest.json`):
   ```json
   { "schema": 1, "version": "0.3.7",
     "url": "https://…/fused-render-windows/FusedRenderPy-0.3.7-setup.exe",
     "sha256": "<hex>", "signature": "<base64 ed25519>" }
   ```
   The signature covers a domain-separated message
   `"fused-render-update\n<version>\n<sha256>\n"`, so a CDN/bucket compromise
   cannot forge a manifest that points the updater at a different installer —
   the sha256 in the manifest is signed and re-checked against the actual
   download. Signing lives in `scripts/windows/generate_update_manifest.py`
   (`uv run`, private key from `$FUSED_RENDER_UPDATE_SIGNING_KEY`).
   **Pinned public key** (client verifies against this constant):
   `u4eiDvccdWmsVCN0nifCEXqmU+xVGIDPe8LP5KRlDns=`. Verification uses
   `cryptography`, added to the `windows-desktop` extra (the only new runtime
   dependency this feature needs; the loop itself is stdlib threading/time).
3. **Shutdown ownership — no conflict, no new IPC.** The updater just launches
   the installer and exits its worker; the installer's `PrepareToInstall`
   execs `--shutdown-for-upgrade`, which forwards over the named pipe to the
   still-running primary → `_event_loop` returns `_ExitReason.UPGRADE` → the
   one `_teardown` path runs (job close, port freed). The installer process is
   spawned by (but not inside) the supervisor's Job, so it outlives the
   teardown. This is exactly the existing upgrade path — nothing new to wire.
4. **Run the installer with its (near-empty) UI, not `/VERYSILENT`.** The
   `[Run]` relaunch entry is `skipifsilent` and nothing else restarts the app,
   so a silent install would leave FusedRender closed; the normal wizard
   (`DisableDirPage` + `DisableProgramGroupPage` make it nearly page-free) ends
   on a finish page that relaunches. `SetupMutex=FusedRenderPySetup` serializes
   installer runs, so an auto-update prompt launching setup can't race a manual
   reinstall through `ActivatePayload`'s rename dance. Keep Windows release
   versions strictly numeric — the client's version compare (and the `.iss`
   `CompareVersions`' `StrToIntDef`) treats a PEP 440 suffix like `0.5.0rc1` as
   non-numeric (the client refuses it and logs; keeps the loop alive).
5. **Release/CI** (done — `.github/workflows/release.yml`, `build-windows-
   release` job, mirrors the DMG contract). On a `v*` tag, after the DMG job:
   build via `build_windows_installer.ps1` (Inno installed with choco — it was
   dropped from the windows-2025 image), sign the manifest, upload the
   installer + `latest.json` to `s3://fused-render/fused-render-windows/`
   behind CloudFront, invalidate `/fused-render-windows/*` (the fixed-name
   `latest.json` is also served `no-cache`), and `gh release upload` the
   installer onto the release the DMG job created (`needs:` it, so no
   release-create race).
6. **One-time setup required:** add the ed25519 private key as the repo secret
   `FUSED_RENDER_UPDATE_SIGNING_KEY` (base64 raw 32-byte seed; matching the
   pinned public key above). Until it exists the `build-windows-release` job
   fails at the signing step — the DMG release still publishes.

**Deferred (not in v1):**
- **In-app (web-shell) "Check for updates" button.** It would need a new
  named-pipe opcode, but `protocol.py` is byte-identical to the Rust
  supervisor for cross-implementation migration; adding an opcode there is a
  contract change. Tray-only for v1; add the opcode (trigger-and-forget, since
  a full check/download blows past `_serve_pipe`'s 20s response window) only
  once the Rust side is retired or mirrors it.
- **Authenticode signing** of `setup.exe`/`FusedRenderPy.exe`. Post-2023 CA/B
  rules require an HSM (Azure Trusted Signing / SSL.com eSigner — USB tokens
  don't work on hosted runners), i.e. org verification + pipeline work. It
  mainly buys first-install SmartScreen trust, which the update path largely
  dodges (the file is written directly, no Mark-of-the-Web). Fast-follow.
- **Fully silent auto-install** (no install prompt) and **WinSparkle** — out of
  scope by design. The install step stays user-approved.

## Acceptance gates — updates

- Background check on a current build → silent: nothing downloaded, no dialog.
- Background check when a newer version exists → downloads + verifies silently,
  then prompts; **decline** leaves the app untouched and isn't re-prompted this
  session; **accept** launches the installer, the app updates in place and
  relaunches, no orphan processes, port freed.
- Manual "Check for updates…" with no newer version → "up to date" dialog.
- Tampered manifest or binary (bad signature or sha256 mismatch) → refused, no
  install (background: silent + logged; manual: error dialog).
- Transient network failure on a background tick → silent (logged), no dialog.
- A release actually publishes `setup.exe` + `latest.json` to the CDN and the
  manifest is not served stale after invalidation.
