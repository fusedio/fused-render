# Linux clipboard: publish text/plain alongside the file targets

## The problem

On macOS, `_darwin.py:56-60` writes `public.file-url` items *and* then
`NSPasteboardTypeString` into the same pasteboard item set, so a file Copy in
the explorer yields a path that pastes into any text field.

On Linux it does not. `_linux.py`'s `write_files` shells out to `wl-copy`/
`xclip`, which take a single `--type` per invocation and replace the selection
on a second call. It therefore picks exactly one target, guessing from
`XDG_CURRENT_DESKTOP` via `_is_kde()`:

- KDE/Plasma -> `text/uri-list`
- everything else -> `x-special/gnome-copied-files`

Two consequences, both reported by the user:

1. **No `text/plain` is ever offered.** Pasting a copied file into a text field
   (a browser textarea) yields nothing; a lenient terminal shows the raw wire
   format, e.g. `copy\nfile:///home/iamsdas/Music`.
2. **The desktop guess mis-fires.** The user is on Hyprland, where
   `XDG_CURRENT_DESKTOP=Hyprland` is neither KDE nor GNOME, so it silently
   falls to the GNOME branch.

## Verified facts — do not re-derive these

Measured on the user's machine (Hyprland / Wayland / `wl-clipboard 2.3.0`):

- `wl-copy` offers exactly ONE type. Passing `-t` twice does not add a second;
  `wl-paste --list-types` shows one. There is no CLI route to multi-target.
- **PyGObject with GTK4 CAN own a multi-target selection.** A `Gdk.ContentProvider.new_union([...])`
  of per-mime providers, handed to `Gdk.Display.get_default().get_clipboard().set_content()`,
  was confirmed to serve all of these simultaneously to an external `wl-paste`:

  | target | payload read back |
  |---|---|
  | `text/plain` | `/home/iamsdas/Music` |
  | `text/plain;charset=utf-8` | `/home/iamsdas/Music` |
  | `x-special/gnome-copied-files` | `copy\nfile:///home/iamsdas/Music` |
  | `text/uri-list` | `file:///home/iamsdas/Music` |

- GTK3 is a dead end: `Gtk.TargetTable` and `Gtk.Clipboard.set_with_data` are
  not exposed through introspection in PyGObject. Use GTK **4**.
- **The app venv cannot import `gi`.** `.venv/bin/python` is 3.12 and has no
  `gi`; `/usr/bin/python3` is 3.14 and imports it fine. The owner process MUST
  therefore be spawned with a *probed* interpreter, not `sys.executable`.

A working prototype is at
`/tmp/claude-1000/-home-iamsdas-Work-fused-render/5b2459f8-2769-43a7-809b-53d517ab8280/scratchpad/gtk4proto.py`.
Read it — it is known-good on this machine.

## What to build

### 1. New module `fused_render/shell/pasteboard/_linux_owner.py`

A standalone resident clipboard owner, executed as a script under a *different*
interpreter than the server's.

- **It must not import anything from `fused_render`** — it runs under system
  Python, which has no access to the app venv.
- The `gi` imports live inside `main()`, not at module top level, so the module
  stays importable (and unit-testable) on a machine with no GTK.
- Reads `{"paths": [...]}` as JSON on stdin.
- Builds the four payloads exactly as the table above:
  - `x-special/gnome-copied-files` = `"copy\n" + "\n".join(uris)`
  - `text/uri-list` = `"\r\n".join(uris) + "\r\n"` (RFC 2483, as the existing
    module docstring already specifies)
  - `text/plain` and `text/plain;charset=utf-8` = `"\n".join(paths)` — the same
    newline-joined form macOS puts in `NSPasteboardTypeString`
- Reuse the URI encoding rule from `_linux.py`'s `path_to_uri` (`quote(path, safe="/")`).
  Since this module cannot import it, copy the function in with a comment
  pointing at the original; keep the two in step.
- **Signals readiness**: print a line to stdout, flush, then `os.dup2` `/dev/null`
  over fd 1. This matters — see the lifecycle note below.
- **Must exit when it loses ownership.** Connect to the clipboard's `changed`
  signal and quit the main loop once `clipboard.is_local()` is false. Without
  this, every copy leaks a resident process forever.

### 2. `_linux.py` — try the owner, keep the current path as fallback

- `write_files` attempts the multi-target owner first. On *any* failure — no
  interpreter with `gi`, no GTK4, spawn error, no readiness within a short
  timeout — fall through to the **existing, unchanged** `wl-copy`/`xclip`
  single-target code. That fallback is still correct for a machine without
  PyGObject, so do not delete it and do not weaken its tests.
- Add an interpreter probe: try `sys.executable`, then `/usr/bin/python3`, then
  `shutil.which("python3")`, testing each with
  `subprocess.run([py, "-c", "import gi"])`. Cache the answer per process;
  expose the cache so tests can reset it.
- **Pipe discipline is critical.** `_linux.py` already carries a long comment
  explaining that a forking clipboard helper inherits the parent's pipes and
  holds them open indefinitely, so `subprocess.run` blocks for the full timeout.
  The same trap applies here. Use `Popen` with `start_new_session=True`, write
  the JSON, close stdin, read the single readiness line with a bounded wait,
  then close the parent's read handle. `stderr` goes to `DEVNULL`. Never wait
  for the child to exit — it is resident by design.
- Once the owner path is in use, `_is_kde()` no longer decides anything (the
  owner offers both file flavors at once). Keep `_is_kde()` for the fallback
  only, and correct the module docstring: the "one real platform limitation"
  paragraph is now the fallback's limitation, not the feature's.

### 3. Tests — `tests/test_pasteboard_linux.py` (340 lines, follow its style)

The existing tests fake `shutil.which` / `subprocess.run` on the module so they
run on any platform. Keep that discipline; **CI has no display, so no test may
start a real GTK loop.**

Cover:
- payload construction for all four mime types, including the CRLF terminator
  on `text/uri-list`, the `copy\n` verb line, and percent-encoding of a path
  with spaces and non-ASCII
- multi-path copies for every target
- the interpreter probe: picks the first candidate that imports `gi`, skips ones
  that fail, and reports none when all fail
- `write_files` spawns the owner when a capable interpreter exists
- `write_files` falls back to `wl-copy`/`xclip` when the probe finds nothing,
  when the spawn raises, and when readiness never arrives
- `_linux_owner` is importable with no GTK present
- every existing test in the file still passes unchanged

## Verification

```
.venv/bin/python -m pytest tests/test_pasteboard_linux.py tests/test_pasteboard.py tests/test_server_clipboard.py -q
```

Scoped only — do not run the full pytest or the frontend suite; the orchestrator
does that. No frontend change is needed: the explorer already mirrors a copy
through `writeOsClipboard`, and this is entirely below that call.

## Out of scope

- Windows and macOS backends.
- The frontend clipboard store, the `Copied` pill, and SPA link handling — all
  already built and committed on this branch; see `DECISIONS-explorer-clipboard.md`.
- Making the owner survive a server restart, or sharing one owner across copies.
  A fresh short-lived owner per copy is correct: the previous one quits when it
  loses ownership.

## Record decisions

Append what you decide, and anything here that turns out wrong, to
`DECISIONS-explorer-clipboard.md`.
