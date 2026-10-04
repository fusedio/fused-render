# Linux auto-update (AppImage)

Bring the macOS in-app updater to the Linux AppImage build. Linux ships an
AppImage today (`scripts/build_linux_appimage.sh`, published to
`s3://fused-render/fused-render-linux/`) with no manifest and no updater —
`release.yml` says so at the "Publish AppImage" step.

The end state is the same user experience macOS already has: a sidebar badge
that says an update is available, an Update button that downloads and swaps,
and a "Restart fused-render" button that actually restarts.

## What already exists (do not rebuild)

- `fused_render/update/common.py` — signed-manifest fetch/verify
  (`fetch_manifest`), `is_newer`, `download_verified` (HTTPS-only incl.
  redirects, streams to a temp file, hashes, compares against the signed
  sha256, `progress` + `should_abort` callbacks, `UpdateCancelled`). Platform
  neutral already. **Do not change its security semantics.**
- `scripts/windows/generate_update_manifest.py` — signs `latest.json`. Used by
  both the mac and Windows release jobs. Reuse as-is.
- `fused_render/update/mac.py` — the `UpdateManager` state machine, the check
  loop, the throttle, the Activity-dock job mirroring, the cancel flag, and
  the DMG install.
- `fused_render/supervisor/_linux/startup.py:68` — `appimage_path() -> Path |
  None`, already resolves `$APPIMAGE` and validates it exists. Reuse it.
- Frontend: `UpdateBadge.tsx` + `update-status.ts` are entirely driven by
  `/api/config`'s `update` field. **They need no changes** — if the Linux
  manager reports the same status shape, the badge just works.

## Task 1 — Extract the shared state machine

Create `fused_render/update/manager.py` holding the platform-neutral half of
today's `mac.UpdateManager`:

- all the state (`_state`, `_latest`, `_error`, `_progress`,
  `_progress_total`, `_phase`, `_check_error`, `_last_check_at`, `_job_id`,
  `_cancel`, `_job_broken`, `_check_only`) and the `RLock`;
- `status()`, `check()`, `install()`, `_job_report()`, `_job_forget()`,
  `_job_clear_cancel()`, `_beat_installing()`, `_cancel_requested()`,
  `_updates_dir()`, `_sweep_stale_downloads()`, `_check_disk_space()`,
  `start_auto_checks()`;
- the constants that are not mac-specific (`JOB_PREFIX`, `PHASE_DOWNLOADING`,
  `PHASE_INSTALLING`, `INSTALL_HEARTBEAT_S`, `CANCELLED_MESSAGE`,
  `MIN_CHECK_GAP_S`, `FAILED_CHECK_GAP_S`, `DEV_MANAGER_ENV`,
  `_DISK_SPACE_FACTOR`). There is no success message: a clean install removes
  its row rather than finishing it (D885), on both platforms, because the
  removal lives in this shared `_install`.

Subclass hooks (abstract or NotImplementedError on the base):

- `method(self) -> str` — informational word on the wire.
- `_disk_version(self) -> str | None` — the version currently on disk.
- `_install_artifact(self, manifest: dict) -> None` — download + swap.
- class attrs `_DOWNLOAD_PREFIX`, `_DOWNLOAD_SUFFIX`, `_STARTUP_DELAY_S`.

Then `mac.py` becomes `MacUpdateManager(UpdateManager)` keeping `bundle_path`,
`find_brew`, `detect_method`, `_install_dmg`, `_attach`, `_find_app`,
`_verify_app_version`, `_discard_old_bundle`, `MANIFEST_URL`, `CASK_NAME`,
`MAC_STARTUP_DELAY_S`.

**Hard constraint: `tests/test_mac_update.py` must pass unmodified.** If a test
patches `mac.<name>`, that name must still resolve on `mac`. Re-export from
`manager` where needed (the `_win32/update.py` module already demonstrates the
re-export-for-patchability pattern). Mac behaviour must not change at all —
this is a pure move. Do not reword or delete the existing comments; they carry
decisions and attributions. Move them with the code they explain.

## Task 2 — `fused_render/update/linux.py`

`LinuxUpdateManager(UpdateManager)`:

- `MANIFEST_URL = os.environ.get("FUSED_RENDER_UPDATE_MANIFEST_URL",
  "https://d2ic19jpchjovp.cloudfront.net/fused-render-linux/latest.json")`
  — same override-for-staging rationale as mac; the signature still pins it.
- Target = `startup.appimage_path()`. `start()` returns `None` when that is
  `None` (dev run / not an AppImage), unless `DEV_MANAGER_ENV` is set, exactly
  like mac's bundle check.
- `method()` returns `"appimage"` when running from one, else `"none"`.
- `_disk_version()` — see Task 3; read the stamp file.
- `_install_artifact(manifest)`:
  1. Refuse early with a clear message if the AppImage's **parent directory**
     is not writable (`os.access(parent, os.W_OK)`) — same shape as mac's
     "cannot write to {parent}" error. This is the common Linux case (an
     AppImage in `/opt` or a read-only mount).
  2. Disk-space check against that parent directory (`_DISK_SPACE_FACTOR`).
  3. `common.download_verified(manifest, dir=<parent of the AppImage>,
     prefix="FusedRender-", suffix=".AppImage", progress=…,
     should_abort=self._cancel_requested)` — **download next to the current
     file**, deliberately: `os.replace` is only atomic within one filesystem,
     and the parent directory is the one place guaranteed to be on the same
     one as the target.
  4. `os.chmod(downloaded, 0o755)`.
  5. `os.replace(downloaded, appimage)` — atomic; the running process keeps
     its open inode, so the swap under a live process is safe (same guarantee
     the mac bundle rename relies on).
  6. Write the version stamp (Task 3), then report the job done.
  - Honour the same post-download cancel check mac does after
    `download_verified` returns (a ✕ that arrived with the final chunk), and
    discard the partial via `common.discard` on any failure.
  - Keep the path `$APPIMAGE` stable — `_linux/integration.py` writes
    `.desktop` `Exec=` lines and an autostart entry pointing at it, and
    renaming the file would strand both.
- No equivalent of mac's `_verify_app_version`: there is nothing cheap to read
  out of an AppImage, and the manifest signature already binds version to
  bytes. Say so in a comment.

## Task 3 — Installed-version stamp + `installed.py`

`installed.py:installed_version()` is mac-only (`sys.frozen == "macosx_app"`)
and is what drives `ServerStatusBanner`'s restart card.

On Linux, after a successful swap the manager writes a stamp JSON next to the
app's state dir (not next to the AppImage — that directory may be shared or
synced):

```json
{"path": "/abs/path/to/FusedRender.AppImage", "size": 123, "mtime": 1.0, "version": "0.5.53"}
```

`installed_version()` on Linux returns the stamped version only when the
current `$APPIMAGE` path, size and mtime all still match the stamp; otherwise
`None` (the honest "no restart signal available" the module docstring already
describes). That way a user who replaced the AppImage by hand, or moved it,
does not get a stale banner.

Pick the stamp location from what the app already uses for state — follow
whatever `DesktopPaths` / the existing state-dir helper provides rather than
inventing a new directory.

## Task 4 — Platform dispatch in the routers

`routers/config.py:109` and `routers/update.py:10` both `import
fused_render.update.mac` directly.

Add to `fused_render/update/__init__.py`:

- `manager() -> UpdateManager | None` — dispatch on `sys.platform`
  (`darwin` → mac, `linux*` → linux, else `None`).
- `start() -> UpdateManager | None` — same dispatch.

Update both routers to call those. `routers/update.py`'s docstring currently
says Linux updates "through the supervisor's own path" — fix that; it is no
longer true. Also start the Linux manager from wherever the Linux server
bootstraps (mirror `app.py:1170`'s mac call site; on Linux the server is a
child of the supervisor, so `server/app.py` is the natural place — follow the
existing `DEV_MANAGER_ENV` block there at `server/app.py:906`).

## Task 5 — Linux relaunch

`fused-render://relaunch` reaches the supervisor as a forwarded `Open` command
and is currently dropped at `core.py:440` ("the quit-and-respawn is macOS-only
machinery"). Make it real:

- Add `_ExitReason.RELAUNCH`.
- When `_open_command` (or the request routing around it) sees a relaunch deep
  link on a platform that can relaunch, end the event loop with that reason
  instead of opening a browser tab. The routing runs on a worker thread
  (`_spawn_open`), so signal the loop the way the other exit paths do —
  through a queue the `_event_loop` already polls — rather than tearing down
  from the worker.
- Treat `RELAUNCH` exactly like `TRAY_EXIT` inside `_teardown` (graceful
  shutdown, `job.close()`), then, back in `run()` and only after teardown
  returns cleanly, respawn: `subprocess.Popen([str(appimage)],
  start_new_session=True, close_fds=True)` and return normally. If
  `appimage_path()` is `None`, do not respawn — just exit, which is the
  honest outcome for a dev run.
- `?reason=fda` (`is_fda_relaunch_url`) is macOS-only; leave it alone.

## Task 6 — Publish the signed Linux manifest

In `release.yml`'s `build-linux-release` job, after the S3 upload of the
AppImage, add a "Publish signed Linux update manifest" step mirroring the
macOS one at line 216:

```yaml
env:
  FUSED_RENDER_UPDATE_SIGNING_KEY: ${{ secrets.FUSED_RENDER_UPDATE_SIGNING_KEY }}
run: |
  uv run --no-project --with cryptography \
    scripts/windows/generate_update_manifest.py \
    "$VERSION" "$APPIMAGE" "https://${CDN_DOMAIN}/${LINUX_PREFIX}" \
    dist/linux-latest.json
  aws s3 cp dist/linux-latest.json "s3://${S3_BUCKET}/${LINUX_PREFIX}/latest.json" \
    --cache-control no-cache
```

Replace the "No latest.json manifest: Linux has no auto-updater yet" comment
with what is now true. The CloudFront invalidation for `/${LINUX_PREFIX}/*`
already covers the manifest path.

## Tests

- `tests/test_linux_update.py` — the Linux manager, mirroring the shape of
  `tests/test_mac_update.py`: manifest check states, the writability refusal,
  the swap (`os.replace` onto a temp "AppImage"), cancel during download,
  a checksum mismatch leaving the original file untouched, the stamp write,
  and `_disk_version` reading it back.
- Stamp tests for `installed.py` on Linux: match, moved file, mtime drift.
- A supervisor test for the RELAUNCH exit reason (follow
  `tests/test_win_supervisor_update.py` / existing supervisor core tests for
  how the loop is driven in tests).
- `tests/test_mac_update.py` unchanged and green.

## Out of scope

- aarch64 AppImages (the build script is x86_64-only today).
- Any .deb/.rpm/flatpak packaging.
- Changing the Windows supervisor updater.
- Any frontend change — the badge is already platform-neutral.

## Verification

Scoped, during the build:

```bash
.venv/bin/python -m pytest -q tests/test_linux_update.py tests/test_mac_update.py \
  tests/test_installed.py tests/test_win_supervisor_update.py
bash -n .github/workflows/release.yml  # or actionlint if available
```

The full suite is the orchestrator's job afterwards — do not run it per commit.
