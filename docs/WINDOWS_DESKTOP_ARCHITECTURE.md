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
