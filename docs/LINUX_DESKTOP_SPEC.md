# Linux desktop supervisor — spec + acceptance gates

Status: **in progress.** This document borrows the *shape* of
[`PYTHON_SUPERVISOR_SPEC.md`](PYTHON_SUPERVISOR_SPEC.md) (gates-before-code) but
not its contract: the behavioral contract below is the **requirements, stated
user-first** — not "match Windows". The Windows backend
(`fused_render/supervisor/_win32/`) is a reference point for *what problems
exist*, not an API to port. Where sockets/XDG/namespaces make a problem simpler
than the Win32 idiom, the Linux backend solves it the Linux-native way.

The current desktop architecture and the per-OS backend seam this plugs into
live in [`DESKTOP_SUPERVISOR.md`](DESKTOP_SUPERVISOR.md). Linux support is a new
`fused_render/supervisor/_linux/` package plus one dispatch branch in
`fused_render/supervisor/_backend.py` — **no changes to `core.py`**. The seam
`core.py` consumes is: `Job` (a supervised process tree with `.spawn(...)` and
`.close()` = tree-kill), `instance` (single-instance election + IPC), `startup`
(autostart toggle), `ui` (native dialogs/shell), and `SPAWN_ERRORS`.

## Goal

Ship FusedRender as a single-file Linux desktop app with the same user-visible
guarantees the Windows build ships — single instance, file "Open with", tray,
start-at-login, no orphaned process trees on crash, clean upgrade — packaged as
an **AppImage** (download, `chmod +x`, run; no admin, no package-manager
matrix). rclone is bundled in the payload so remote mounts work with zero user
setup; `fusermount3` stays host-provided (a setuid binary cannot ship in an
AppImage).

## Requirements → Linux mechanism

| Concern | Linux mechanism (`_linux`) |
| --- | --- |
| Single instance | `flock(LOCK_EX\|LOCK_NB)` on `$XDG_RUNTIME_DIR/fused-render/supervisor.lock`. The kernel releases the lock on **any** death including `SIGKILL` — the same "abandoned mutex" semantics the Windows named mutex has. |
| IPC (secondary → primary) | Unix stream socket (`supervisor.sock`) in the same `0700` runtime dir, socket mode `0600`; carries the **identical** `protocol.py` frames with a 4-byte status reply. |
| Process-tree kill | **baseline**: `start_new_session` (setsid) + `PR_SET_PDEATHSIG(SIGKILL)` via `prctl` in a `preexec_fn`, with `killpg` for deliberate teardown. **strong**: wrap the server in an unprivileged user+pid namespace (`unshare --user --map-root-user --pid --fork --kill-child`) so the kernel reaps every descendant when ns-init dies. Selected by `FUSED_RENDER_LINUX_TREE_KILL` (`pgroup` default, `namespace` opt-in); the gate decides which ships by default. |
| Tray | `pystray` (AppIndicator backend where a StatusNotifier host exists; X11 fallback via python-xlib). Absence on stock GNOME is an accepted degraded mode (gate (c)). |
| Start at login | `~/.config/autostart/fused-render.desktop` (`$XDG_CONFIG_HOME` honored). |
| Dialogs / shell-open | `_linux/ui.py`: first available of `zenity` → `kdialog` → bundled tkinter; `xdg-open` for open_path/open_uri/open_url (same pattern as `server.py`'s reveal-in-file-manager). |
| Paths | XDG: state `$XDG_DATA_HOME/fused-render/desktop`, cache `$XDG_CACHE_HOME/fused-render/desktop`, runtime `$XDG_RUNTIME_DIR/fused-render`, with documented fallbacks (`~/.local/share`, `~/.cache`, and a `0700` dir under cache when `XDG_RUNTIME_DIR` is unset). |
| Installer / upgrade | Replace the `.AppImage` file; old instance shuts down via the same `--shutdown-for-upgrade` command over the socket. |

The `.desktop` `Exec` uses `%f` (a **single** file field code), not `%F`, to
match the Windows registry command's `%1` and keep `protocol.parse_args`
identical across platforms (it accepts exactly one path argument). A multi-file
"Open with" therefore launches one instance per file; the second and later
instances forward their open to the primary over the socket and exit, exactly
as a second manual launch does — the desktop environment coalesces, the
supervisor never sees more than one path per process.

## Acceptance gates (go / no-go)

- **(a) No orphans on `SIGKILL` of the supervisor.** Server + template daemons +
  rclone mounts all die. This is the riskiest guarantee; it is encoded as a CI
  test (`tests/test_supervisor_linux_tree.py`) so it is enforced forever, not
  just measured once.
- **(b) Port does not clash** with a running dev server (ephemeral port; the
  run loop already retries 3 ports).
- **(c) Tray** works on KDE and a GNOME with an AppIndicator extension, and its
  *absence* on stock GNOME still leaves the app fully usable — browser reachable
  and a second launch forwards (opens home) instead of double-serving.
- **(d) Open-with** association works after user-level AppImage integration.
- **(e) Upgrade** = replace the file while running; the old instance shuts down
  via `--shutdown-for-upgrade`.
- **(f) Runs on the two oldest supported targets** — Ubuntu 22.04 + Debian 12 —
  with **no FUSE2 assumption** (type-2 / static-runtime AppImage).

### Which gates run where

| Gate | Where verified |
| --- | --- |
| (a) no-orphans | `tests/test_supervisor_linux_tree.py` in CI (Linux runner) + manual `kill -9` walk on a VM |
| (b) port clash | unit + manual |
| (c) tray / degraded mode | **manual** — headless CI cannot see a tray; KDE + stock-GNOME VMs |
| (d) open-with | **manual** — needs a desktop session + AppImage integration |
| (e) upgrade | **manual** — replace-file-while-running walk |
| (f) 22.04 / Debian 12 | AppImage build smoke in CI + **manual** VM walk |

Gates (c), (d), (e), (f)-manual are a desktop-session checklist that headless CI
cannot exercise; they are signed off on a VM before release and recorded in the
Decision section below.

## Non-goals / accepted trade-offs

- **Tray fragmentation.** Stock GNOME has no StatusNotifier host → no icon.
  Accepted: everything is reachable via the browser and a re-launch. The tray's
  "Default apps..." item opens an OS "default apps" settings page on Windows
  (`ms-settings:defaultapps`); there is no cross-desktop Linux equivalent, so
  `_linux/ui.open_default_apps` raises `OSError` and the action is a logged
  no-op (the run loop's `_safe_call` swallows it). Cosmetic, not a launch
  blocker.
- **Template daemons that outlive the server on purpose** (rclone rcd serves,
  tile daemons started with `start_new_session`): under either tree-kill they die
  with the app, matching Windows Job semantics but differing from dev/macOS.
  Same accepted trade-off as Windows.
- **glibc floor.** python-build-standalone needs glibc ≥ 2.17; `[bundled]`
  manylinux wheels (duckdb, rasterio…) have their own floors. Ubuntu 22.04 /
  Debian 12 clear all of them. musl/Alpine is out of scope.
- **x86_64 only** first. aarch64 is a follow-up once the pipeline exists.
- **fusermount3 host-side.** rclone ships in the payload; `fusermount3` is
  setuid and cannot. A host without FUSE gets the existing mount-error surface.

## Tree-kill mechanism — measurement & decision

Two candidates, both implemented thin behind `_linux/tree.py`, selected by
`FUSED_RENDER_LINUX_TREE_KILL`:

- **`pgroup` (baseline, default).** `PR_SET_PDEATHSIG(SIGKILL)` kills the direct
  child when the supervisor dies; `killpg` on the session handles deliberate
  teardown. **Known gap:** a grandchild that calls `setsid` itself escapes the
  process group — those are not guaranteed reaped on supervisor crash.
- **`namespace` (strong, opt-in).** `unshare --user --map-root-user --pid --fork
  --kill-child` makes the server pid-1 of a private pid namespace; the kernel
  tears down the whole namespace when pid-1 dies, and `--kill-child` propagates a
  supervisor kill down. Airtight, but depends on unprivileged userns being
  enabled (default on Ubuntu/Fedora/Debian ≥ 12, sometimes disabled by
  hardening).

**Decision (default committed, pending the VM walk):** `pgroup` ships as the
default (`FUSED_RENDER_LINUX_TREE_KILL` unset → `pgroup`); `namespace` is
opt-in. Rationale: `pgroup` has no host prerequisite, so it works on every
target including hosts that disable unprivileged userns by hardening;
`namespace`'s airtight escaped-grandchild guarantee is available for operators
who want it and whose hosts allow userns. This is revisited to make `namespace`
the default only if the VM walk shows userns is reliably available across the
target matrix AND the escaped-grandchild case matters in practice (the escaping
daemons the app spawns — rclone rcd, tile daemons — are the same ones the spec
already accepts dying with the app). `tests/test_supervisor_linux_tree.py`
measures both mechanisms wherever userns is available.

## Decision record (gate results)

- **(a) no-orphans — partially enforced in CI, full guarantee pending VM.**
  `tests/test_supervisor_linux_tree.py` runs in the `linux-desktop` CI job. On a
  stock GitHub runner unprivileged userns is usually unavailable, so only the
  `pgroup` parametrization runs there — it enforces "the direct child dies when
  the supervisor is SIGKILLed" and "job.close() reaps the same-session tree" on
  every push. The `namespace` parametrization (and thus the escaped-grandchild
  guarantee) runs only where userns is enabled: a self-hosted/privileged runner
  or the manual VM walk. **Action for the VM walk:** confirm on Ubuntu 22.04 +
  Debian 12 that `kill -9` of the supervisor leaves zero `fused`/server/rclone
  processes under each mechanism the host supports.
- **(b)–(f):** pending the manual VM walk (see the "Which gates run where"
  table). The AppImage build + its import/`_child.py`/rclone smoke tests run in
  the CI job (a slice of (f)); tray/dialogs/open-with/upgrade are the manual
  desktop-session checklist.

_The manual VM sign-off (KDE + stock-GNOME, Ubuntu 22.04 + Debian 12) is
recorded here at release time._
