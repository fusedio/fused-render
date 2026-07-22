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
