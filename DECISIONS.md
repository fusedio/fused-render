# Linux auto-update — build log

Read in full before resuming. Append, don't rewrite.

## Task 1 — extract manager.py

- Base class in `fused_render/update/manager.py`, `MacUpdateManager(UpdateManager)`
  in `mac.py`. `tests/test_mac_update.py` patches `mac.<name>` for a long list of
  names (`__version__`, `MAC_STARTUP_DELAY_S`, `MIN_CHECK_GAP_S`,
  `FAILED_CHECK_GAP_S`, `time.sleep`/`time.monotonic` via the `time` module
  object, `INSTALL_HEARTBEAT_S`, `PHASE_DOWNLOADING`, `PHASE_INSTALLING`,
  `DEV_MANAGER_ENV`, `_manager`, `bundle_path`, `UpdateManager` class attrs like
  `start_auto_checks`) — all of these are re-exported/aliased on `mac` so
  `monkeypatch.setattr(mac, "X", ...)` still resolves and is actually read by
  the code path under test. Mirrors `_win32/update.py`'s
  `_PUBLIC_KEY = _common.PUBLIC_KEY` pattern: module-level aliases, read at
  call time through `mac.<name>` inside methods that live in `mac.py`
  (`start_auto_checks`, `check`, is overridden or calls through self so the
  patched *instance*'s class (`mac.UpdateManager` = `MacUpdateManager`) is what
  gets exercised).
- Decision: `mac.UpdateManager` stays the name tests import and construct
  (`mac.UpdateManager(bundle=..., method=...)`) — so `mac.py` does
  `UpdateManager = MacUpdateManager` is wrong (shadows the base import); instead
  `MacUpdateManager` IS what test code calls `mac.UpdateManager` — i.e. in
  `mac.py`, name the subclass `UpdateManager` directly (not aliased), since
  nothing outside mac.py needs the base class name from mac's namespace.
- `time` module: mac.py imports `time` (stdlib) directly; tests patch
  `mac.time.sleep`/`mac.time.monotonic`. Since `time` is a shared stdlib module
  object, if `manager.py` also does `import time` and mac.py re-exports its own
  `import time`, patching `mac.time.sleep` patches the *same* module object
  manager.py's `time.sleep` calls read from — no re-export needed for `time`
  itself, only for the mac-specific constants/functions defined in mac.py or
  moved to manager.py that tests reach through `mac.<name>`.
- THE REAL PROBLEM, found only once I actually wrote manager.py: re-exporting
  a constant on `mac` (`mac.PHASE_DOWNLOADING = manager.PHASE_DOWNLOADING`,
  the `_win32/update.py` idiom) is NOT enough by itself once the CODE that
  reads the constant has also moved to manager.py. `_win32/update.py`'s
  aliases work because the functions reading them are defined in that same
  file — a bare module-global lookup resolves in that module's own
  namespace. Here, `check()`/`install()`/`_beat_installing()` etc. live in
  manager.py, so a bare `MIN_CHECK_GAP_S` read there is manager.py's OWN
  global, immune to `monkeypatch.setattr(mac, "MIN_CHECK_GAP_S", ...)` no
  matter how faithfully mac.py re-exports the name.
  Fix: `UpdateManager._const(self, name)` in manager.py — looks `name` up on
  `sys.modules[type(self).__module__]` first (i.e. on whichever concrete
  module — mac.py or linux.py — actually defined the running instance's
  class), falling back to manager.py's own module global only if the
  subclass's module doesn't define that name at all. Every constant a test
  patches through `mac.<name>` (`MIN_CHECK_GAP_S`, `FAILED_CHECK_GAP_S`,
  `INSTALL_HEARTBEAT_S`, `PHASE_DOWNLOADING`, `PHASE_INSTALLING`,
  `JOB_PREFIX`, `DONE_MESSAGE`, `CANCELLED_MESSAGE`) is read through
  `self._const("NAME")` inside manager.py instead of a bare global. `time`
  needed no such treatment (see above — same module object either way), and
  neither did `DEV_MANAGER_ENV` (only read inside mac.py's own `start()`,
  which never moved) nor `MAC_STARTUP_DELAY_S` (read only via the
  `_STARTUP_DELAY_S` class-attr hook below).
- `_STARTUP_DELAY_S` (the class-attr hook `start_auto_checks()` reads) is
  overridden in mac.py's `UpdateManager` as a `@property` returning the
  module global `MAC_STARTUP_DELAY_S`, rather than a plain class attribute
  set once at class-body time — a plain assignment would freeze whatever
  `MAC_STARTUP_DELAY_S` was at import time, and `test_mac_update.py` patches
  `mac.MAC_STARTUP_DELAY_S` at runtime expecting `start_auto_checks()` (which
  lives in manager.py) to see the patched value on its very next read.
- Naming: the mac subclass really is named `UpdateManager` inside `mac.py`'s
  own namespace (matches `mac.UpdateManager(bundle=..., method=...)` in every
  test). The shared base class is imported as `from fused_render.update
  import manager as _base` and subclassed `class UpdateManager(_base.
  UpdateManager)`. The module-level singleton (`mac._manager`,
  `mac.manager()`, `mac.start()`) is untouched by any of this — it is a
  separate name (`_manager`) from the `_base` import alias, so there is no
  collision between "the shared manager module" and "this module's
  singleton instance", which an earlier draft of this file conflated.
- `_updates_dir()` / `_sweep_stale_downloads()`: moved to manager.py
  VERBATIM, including their macOS-flavoured wording ("`~/Library/Application
  Support/fused-render/updates`", "DMGs and staged bundles") — Task 1 is a
  pure move for mac's own behaviour, and the spec lists `_updates_dir()` as a
  concrete (non-hook) base method. `LinuxUpdateManager` (Task 2) overrides
  `_updates_dir()` with a Linux-appropriate path rather than inheriting the
  macOS one; see the Task 2 section below for where that directory lives.
- Task 1 done, committed. `tests/test_mac_update.py` — all 63 tests green,
  unmodified.
