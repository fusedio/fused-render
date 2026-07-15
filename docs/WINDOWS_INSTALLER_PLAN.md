# Windows installer (`FusedRender-Setup.exe`) — implementation plan

Status: **implemented** on `feat/windows-installer` (2026-07-15), unsigned. The
Windows analog of the macOS DMG: one exe a customer double-clicks, which
installs the app, a private Python runtime, every required package, and the
Explorer "Open with" associations — no Python/uv/terminal prerequisites. Stacked
on PR #132 (winopen.py in the wheel).

Built & verified end-to-end on Windows: `scripts/build_windows_installer.ps1`
produces `dist/FusedRender-0.2.6-setup.exe` (201 MB; ~0.96 GB installed). E2E
(silent install → associations point at the bundle's `pythonw -m
fused_render.winopen` → double-click boots a bundle server answering
`/api/config` → silent uninstall removes everything): 16/16 checks passed.
Deferred by owner: code signing (unsigned pilots first), per-category icons
(single icon for now), tray icon.

## UX contract

- Download `FusedRender-<version>-setup.exe` → double-click → next/next/finish.
- Per-user install (HKCU, **no admin prompt**) into `%LOCALAPPDATA%\Programs\FusedRender`.
- File associations registered during install; Start Menu shortcut launches the
  server + browser; uninstall removes associations and files cleanly.
- Double-clicking a `.parquet`/`.csv`/… in Explorer afterwards opens it in
  fused-render with no console windows, ever.

## Decision: bundle a real Python env; wrap with Inno Setup

Two independent lines of evidence rule out freezing (PyInstaller/Nuitka):

1. **Architecture (already decided — D29):** the executor (`_child.py`), the
   deploy shim (`_fused_cli.py`), and `winopen._spawn` all re-invoke
   `sys.executable` as a general-purpose interpreter. A frozen exe is not one.
   py2app on macOS works precisely because it bundles a *real* interpreter.
2. **Dependency stack:** rasterio/GDAL, pyproj (proj.db), usd-core (pxr
   `plugInfo.json` plugin registry), and pymupdf are all recurring PyInstaller
   casualties; there is **no PyInstaller hook for usd-core at all**. These
   wheels are self-relocating in a normal site-packages layout — only freezing
   breaks them.

So: ship the Windows equivalent of the .app — a relocatable CPython + a
site-packages tree — and let an installer lay it down.

- **Runtime:** python-build-standalone 3.12 via `uv python install 3.12
  --install-dir build/win-runtime` (Astral-maintained, designed relocatable; no
  `._pth`/get-pip surgery that the python.org embeddable zip needs). 3.12
  matches the mac pipeline and satisfies the `fused` extra's `>=3.11` marker.
- **Packages:** build the wheel once (same as `build_dmg.sh`), then
  `uv pip install --python <bundle python> "<wheel>[bundled,fused]"`.
  **Never `[app]`** (rumps/pyobjc, macOS-only). **No rclone** (mounts are
  unsupported on Windows).
- **Installer:** Inno Setup (`iscc`), `PrivilegesRequired=lowest`, LZMA2 solid.
  NOT preinstalled on windows-2025 GH runners (nor is NSIS) —
  `choco install innosetup -y` in CI.

Rejected: **PyInstaller+Inno** (above), **pynsist** (dormant project, would
inherit it), **Briefcase/WiX** (GUI-app-shaped, large-MSI failures, custom WiX
for `--register`), **conda constructor** (deps are pip wheels; poor fit),
**MSIX** (mandatory signing + manifest-declared associations conflict with the
existing `--register` mechanism). **WinGet** is a follow-up distribution
channel (accepts Inno installers), not an alternative.

## Bundle layout

```
%LOCALAPPDATA%\Programs\FusedRender\
  python\                 # python-build-standalone 3.12 (python.exe, pythonw.exe, Lib\, DLLs\)
  python\Lib\site-packages\fused_render\...   # wheel + [bundled,fused] installed in
  unins000.exe            # Inno uninstaller
```

Short root path on purpose (MAX_PATH: pxr plugins + matplotlib data are deep;
test on a VM without `LongPathsEnabled`). Prune before packaging:
`__pycache__`, `Scripts\*.exe` (see pitfall #1), tests/ dirs of big deps.

## Required code changes (small, in `fused_render/winopen.py`)

1. **Do not reference `Scripts\*.exe` in the bundle.** pip-generated launcher
   exes hardcode the absolute build-machine python path (pip #13162) — dead
   after relocation. Pruning them makes `_build_command`'s existing fallback
   (`sys.executable -m fused_render.winopen`) kick in. Fix the fallback to
   prefer the `pythonw.exe` sibling of `sys.executable` when present, so
   double-click never flashes a console.
2. **Silent registration:** `--register` currently `_report`s via MessageBox
   when stdout is None. Installer runs it via `python.exe` (console handle,
   prints instead of popping) or a new `--quiet` flag — pick one, keep it tiny.
3. (Flag, decide later) ProgIDs are not branch-suffixed; a branch-stamped
   installer would overwrite the baseline's associations.

## Build script: `scripts/build_windows_installer.ps1`

Mirror of `build_dmg.sh`, same env-var contracts:

1. Resolve `REF`/`SUFFIX` via `python -m fused_render._branch ref|suffix`;
   `VERSION` from pyproject.toml. Artifact:
   `dist/FusedRender<SUFFIX>-<VERSION>-setup.exe`.
2. `$env:FUSED_RENDER_BRANCH=$REF` → build wheel once (`python -m build
   --wheel`; needs Node 22 for the hatch shell build). Force-reinstall the
   wheel if the bundle env is reused (stale `_baked_branch.py`, same trick as
   `build_dmg.sh:136`).
3. `uv python install 3.12` into the bundle dir; `uv pip install
   "<wheel>[bundled,fused]"` into it; generate per-ext icons
   (`scripts/windows/gen_file_icons.py`) if we want them; prune.
4. **Smoke tests through the bundle's own python.exe** (port of
   `build_dmg.sh:285-384`): duckdb import via `fused_render/_child.py` (the
   exact user-code runtime path) and `fused` CLI `--help`/`env list` via
   `_fused_cli.py`.
5. Sign payload exes + (after compile) the installer — credential-driven with
   unsigned fallback, mirroring the mac script's posture.
6. `iscc scripts\windows\installer.iss` → setup exe in `dist/`.

## Installer definition: `scripts/windows/installer.iss`

- `PrivilegesRequired=lowest`, `DefaultDirName={localappdata}\Programs\FusedRender`.
- `[InstallDelete]`: wipe `python\Lib\site-packages` (or the whole `python\`)
  before copying on upgrade — Inno overlays and never deletes, stale
  `.dist-info` otherwise accumulates.
- `[Run]` post-install: `"{app}\python\python.exe" -m fused_render.winopen --register`.
- `[UninstallRun]`: `... --unregister` (runs before file removal — python still
  present, ordering is safe).
- `[UninstallDelete]`: known runtime dirs (`%LOCALAPPDATA%\fused-render` logs/
  pidfiles) — Inno's log doesn't know runtime-created files.
- `[Icons]`: Start Menu → `"{app}\python\pythonw.exe" -m fused_render.winopen`
  (no file arg = opens home), icon `fused_render\assets\fused-render.ico`.

## CI: new job in `.github/workflows/release.yml`

- Sibling job `build-windows-installer` on `windows-latest`, same `v*` tag
  trigger; `needs:` the mac job (release creation currently lives inside the
  mac job — attach with `gh release upload`, or restructure into build jobs +
  one release job later).
- Steps: checkout → setup Node 22 + uv → `choco install innosetup -y` →
  `pwsh scripts/build_windows_installer.ps1` → OIDC AWS creds (same role) →
  upload exe + sha256 to `s3://fused-magic/fused-render-windows/` → attach exe
  to the GitHub release. Do **not** re-upload the wheel (mac job owns it).

## Code signing

**Azure Trusted Signing** (a.k.a. Artifact Signing): ~$10/mo Basic, 5k
signatures, first-class GH Actions action, short-lived MS-issued certs with
immediate SmartScreen reputation — strictly better than OV certs ($200-400/yr
+ slow reputation grind); EV no longer worth it. Eligibility: org with 3+
years verifiable history — **start identity validation early, it's the slow
part**. Unsigned fallback works for hand-held pilots (blue SmartScreen
interstitial, "More info → Run anyway") but corporate SmartScreen-block
policies and AV quarantine of a ~350 MB unsigned exe make it a real adoption
tax. Sign both the payload and the installer.

## Size expectations

~1.0–1.3 GB installed (scipy/pyarrow/duckdb/polars/two GDALs/usd-core/pymupdf
dominate); ~300–400 MB setup exe after LZMA2 — same ballpark as the DMG.
Freezing would not be smaller; the payload is binary wheels either way.

## Open decisions (owner)

1. Sign from day one (needs Azure onboarding started now) or ship unsigned
   pilots first?
2. Ship per-extension icons (`gen_file_icons.py` output) or keep single-icon
   until the rendering issue is fixed?
3. Start Menu launch = opener with no file (server + browser at home). Good
   enough, or is a tray icon wanted later (pystray — new dependency, new PR)?
4. WinGet submission once signed?

## Suggested PR slicing

1. **PR A:** `winopen.py` bundle-mode tweaks (pythonw fallback preference,
   silent register) + tests. Tiny, mergeable independently.
2. **PR B:** `scripts/build_windows_installer.ps1` + `installer.iss` +
   `docs` — buildable locally on this machine end-to-end before CI.
3. **PR C:** `release.yml` windows job + S3/release wiring.
4. **PR D (later):** signing integration once Azure onboarding completes;
   WinGet manifest.
