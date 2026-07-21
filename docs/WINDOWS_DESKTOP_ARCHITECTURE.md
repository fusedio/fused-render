# Windows desktop architecture

Status: local installer prototype. This is not a release artifact.

## Product contract

- Windows 10/11 x64, per-user install, no administrator requirement.
- `FusedRender-<version>-setup.exe` installs or upgrades in place and launches the app.
- One tray/controller instance owns one private Python server and its complete process tree.
- The existing wheel installation, its processes, registry entries, and state remain untouched.
- Explorer integration adds FusedRender to Open With. It does not silently replace defaults.
- The first release opens the UI in the default browser.

## Packaging decision

Use Inno Setup around a private relocatable CPython and a small Rust supervisor. The Python
tree is required because rendering executes arbitrary Python files and invokes
`sys.executable`; freezing with PyInstaller, Nuitka, or a web-shell packager would not remove
that requirement and is brittle for the native dependency stack.

The installed layout will be:

```text
%LOCALAPPDATA%\Programs\FusedRender\
  payload\
    FusedRender.exe
    python\
      python.exe
      pythonw.exe
      uv.exe
      Lib\site-packages\...
    assets\icons\...
  unins000.exe
```

The supervisor source is in `windows/supervisor`. It is a Windows GUI-subsystem binary, so it
does not allocate a console.

## Why a native supervisor (and what stays Python)

Most of the supervisor is portable orchestration — ephemeral-port pick, `/api/config`
readiness probe, launch-token generation, browser open, graceful-shutdown call — and `app.py`
already implements all of it in Python for macOS. That part does not need a new language.

Two responsibilities carry the native binary:

- **Job Object process-tree kill.** `KILL_ON_JOB_CLOSE` plus the suspended-create → assign →
  resume sequence guarantees the entire private-Python tree (server, tile daemons, compile
  workers) dies when the supervisor exits — even on a hard crash — with no orphans. This is
  the "closes properly / no orphans" contract above.
- **A windowless owner decoupled from the interpreter it supervises.** A static native `.exe`
  can still show an error and clean up when the bundled Python is broken; a `pythonw.exe`
  supervisor could not.

Single-instance election and Open-With forwarding are *not* free the way they are on macOS,
where LaunchServices routes every open to the one running `.app` and AppKit re-delivers it.
Windows spawns a fresh process per Open-With, so the mutex + named-pipe IPC is net-new work in
any language — it is not macOS Python that was skipped.

A full-Python alternative is viable (thin launcher + `pywin32` for the job object / pipe /
mutex, `pystray` for the tray) and would remove the second toolchain. It is being prototyped
on a branch off `main` and validated against the same lifecycle gates below before any switch;
the Rust supervisor stays the shipping path until that proves out. A half-native/half-Python
split is deliberately avoided: the tray, single-instance election, and job ownership must live
in one surviving process, so splitting them only adds an IPC seam.

### macOS (`app.py`) vs Windows supervisor

| Aspect | macOS (Python) | Windows (this PR, Rust) |
|---|---|---|
| Server process | uvicorn in-process, daemon thread | separate private `pythonw.exe` child |
| Process cleanup | idle-exit + `_quit_tile_daemons` | Job Object `KILL_ON_JOB_CLOSE` |
| Single-instance | pidfile + portfile + HTTP probe | named mutex |
| Open-file forwarding | AppKit `openFiles:`/`openURLs:` | named-pipe IPC |
| Readiness / security | untokened `/api/config` probe | token + instance-id-gated `/api/config` |
| State isolation | `branch_dir`/`branch_port` | `FUSED_RENDER_*` env via `paths.py` |
| In-app UI | tray + WKWebView popover | tray → default browser |

These divergences are intentional for now (macOS gets OS-level routing Windows lacks); if
`paths.py` becomes the shared isolation system, macOS should eventually route through it too.

## Lifecycle

`FusedRender.exe` is the only Start Menu and Explorer command target.

Its tray menu provides Open FusedRender, Open file, status/port, Open logs, Default apps,
Start at sign in, and confirmed Exit. Double-clicking the tray icon opens the browser.

1. A per-user named mutex elects one supervisor.
2. Later launches forward `Open(path)`, `OpenHome`, or `ShutdownForUpgrade` over a secured
   message-mode named pipe and exit.
3. The supervisor chooses an ephemeral localhost port and generates a 256-bit launch token.
4. It creates a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
5. It creates private `pythonw.exe` suspended, assigns it to the Job, then resumes it. This
   closes the race in which Python could create an unowned child before assignment.
6. Readiness requires `/api/config` to return the expected desktop instance ID and launch
   token. A wheel server or another launch cannot satisfy this check.
7. Shutdown calls a token-protected local endpoint and waits for Uvicorn to exit. Closing the
   final Job handle remains the backstop that terminates Python descendants.

The current Job Object test creates a parent and grandchild process, confirms both belong to
the Job, closes the Job, and confirms both terminate.

## State isolation

The supervisor passes these roots to Python before any package modules import:

| Variable | Desktop location | Uninstall policy |
|---|---|---|
| `FUSED_RENDER_HOME` | `%LOCALAPPDATA%\FusedRender\Desktop\state` | Preserve |
| `FUSED_RENDER_CACHE_DIR` | `%LOCALAPPDATA%\FusedRender\Desktop\cache` | Remove |
| `FUSED_RENDER_RUNTIME_DIR` | `%LOCALAPPDATA%\FusedRender\Desktop\runtime` | Remove |
| `FUSED_RENDER_TEMP_DIR`, `TEMP`, `TMP` | `%LOCALAPPDATA%\FusedRender\Desktop\temp` | Remove |
| `FUSED_RENDER_LOG_DIR` | `%LOCALAPPDATA%\FusedRender\Desktop\logs` | Remove |
| `OPENFUSED_*` stores | `state\openfused` | Preserve |
| `RCLONE_CONFIG` | `state\rclone\rclone.conf` | Preserve |
| `RCLONE_CACHE_DIR` | `cache\rclone` | Remove |
| `UV_CACHE_DIR` | `cache\uv` | Remove |
| Claude config/transcripts | `state\claude` | Preserve |
| DuckDB extensions/spill | `cache\duckdb` | Remove |

`FUSED_RENDER_DIR` is deliberately not overridden. User documents and projects remain normal
files and may be opened by either the wheel or desktop app. App-managed preferences,
templates, credentials, caches, daemons, logs, ports, and IPC identities are not shared.

When these overrides are absent, wheel paths retain their existing values. This is required
for installed wheel users and persisted data.

## Explorer registration

The installer registers desktop-specific identities such as
`FusedRender.Desktop.csv` and `Applications\FusedRender.exe`. It must not call the wheel's
`fused_render.winopen --register` or remove wheel ProgIDs during uninstall.

Open With registration includes category icons for spreadsheet/data, PDF/document, LaTeX,
image, geospatial, archive, media, database, and 3D formats. Modern Windows controls default
application selection; the tray should link to Default Apps settings rather than modifying
`UserChoice`.

## Upgrade and uninstall

An upgrade must:

1. Send `--shutdown-for-upgrade` to the installed supervisor.
2. Wait for the supervisor mutex and all Job-owned processes to exit.
3. Replace the complete private runtime, never overlay `site-packages`.
4. Preserve `state`, remove old regenerable directories, register the new executable, and
   launch one new supervisor.

A normal uninstall removes installed binaries, desktop-only registry entries, cache, runtime,
temp, and logs. It preserves `state` and user documents. A later explicit cleanup option may
remove preserved state; it must never delete wheel paths.

## Verification gates

The installer is not release-ready until automated checks cover:

- install, running-app upgrade, downgrade rejection, repair, and uninstall;
- wheel registration/state snapshots before and after the complete lifecycle;
- occupied ports and rapid concurrent file opens;
- parent, child, grandchild, compiler, and daemon cleanup;
- no visible console for every Windows subprocess path;
- Excel CSV/XLSX/PDF/Parquet exports from `examples/excel_editor/imports/reports.xlsx`;
- PDF PDF/PNG/JPG/text exports from
  `examples/pdf_studio/projects/pdf-studio-demo/welcome.pdf`;
- cold and warm LaTeX compile plus PDF/HTML/DOCX/Markdown exports from
  `examples/latex_studio/demo/main.tex`;
- offline warm operation and uninstaller residue checks in a clean Windows VM.

Known product gaps are clean-VM and interactive lifecycle coverage, bundled Pandoc verification,
code signing, release jobs, Windows toast delivery, and rclone mounts. Windows mount support is
deferred until the installer can include WinFsp and Windows-specific unmount handling.
