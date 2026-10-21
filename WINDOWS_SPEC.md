# Windows CLI support — build spec

Goal: `fused-render serve` and `fused-render open <path>` start and work on native Windows
(no WSL). Features that can't work there are disabled cleanly (feature reports unsupported /
no-op), never crash startup. macOS/Linux behaviour must not change.

Delete this file in the final commit; move anything a reviewer needs into code or the PR.

## Must do

1. **Remove the gate** at `fused_render/cli.py:~770` (`if sys.platform == "win32": sys.exit(...)`)
   and its comment. Replace `tests/test_cli_platform.py` with a test asserting win32 reaches the
   normal serve dispatch.

2. **Shared platform helper** `fused_render/_platform.py` (stdlib only, never raises at import):
   - `IS_WINDOWS`
   - `flock(fh, exclusive=True, blocking=True) -> bool` / `funlock(fh)`: `fcntl.flock` on POSIX;
     on Windows `msvcrt.locking` on byte 0 (seek to 0 first; LK_NBLCK for non-blocking, retry
     loop of LK_NBLCK for blocking since LK_LOCK gives up after 10 tries). Return False when a
     non-blocking lock is held elsewhere — match whatever exception/return contract the call
     sites currently rely on (they catch `BlockingIOError`/`OSError`; keep that working).
   - `kill_tree(pid, sig=None)`: POSIX keeps current `os.killpg(os.getpgid(pid), SIGKILL/sig)`
     semantics; Windows uses `taskkill /T /F /PID` (or `os.kill(pid, SIGTERM)` fallback).
     Swallow ProcessLookupError/OSError like existing sites.
   - `detach_kwargs()` for Popen: POSIX `{"start_new_session": True}`; Windows
     `{"creationflags": CREATE_NEW_PROCESS_GROUP}`. Only use where children are already
     `start_new_session=True`; don't change POSIX behaviour.

3. **Module-level `import fcntl` on the serve import path** → use the helper:
   `tasks_store.py:128`, `drafts.py:75`, `community.py:37`, `index/store.py:15`,
   `claude_config/lib.py:18`, `server/routers/claude_sessions.py:32`, `ai/hub_catalog.py:27`.
   Call sites are flock-only (see report lines: tasks_store 269,385,405; drafts 605; community
   117,127; index/store 145,150,167; claude_config/lib 122; claude_sessions 1489,1525,1555;
   hub_catalog 73,77). `tasks_store` lease (`try_acquire_lease`, `acquire_lease_blocking`) must
   keep real mutual exclusion on Windows via msvcrt — it gates queue_manager / index owner.
   Already-guarded modules (`shell/notifications.py`, `marketplace.py`, `canvas_sync_state.py`)
   may stay as they are.
   `sysmon/common.py:4` `import pwd` → lazy inside the function, fallback `str(uid)`.

4. **Verify the import path**: add a test that, with `fcntl`, `pwd`, `termios`, `pty`, `resource`
   made unimportable (e.g. `sys.modules[name] = None` in a subprocess) and `sys.platform`
   patched to `win32` where feasible, `import fused_render.server` and `fused_render.cli`
   succeed. A subprocess test is fine. Iterate until it passes — the report may have missed
   modules; fix any others it surfaces the same way.

5. **Runtime breakers on serve/open startup + shutdown**:
   - `server/engine_host.py:249-261` `_kill_tree` → `_platform.kill_tree`. Same for
     `ai/supervisor.py:712-719`, `envinstall.py:1176-1187`, `share_file.py:494`,
     `sysmon/sampler.py:550`, `claude_cmd_log.py:378`, `bots/agent_engine.py:626,642`,
     `bots/browser.py:1285,1402-1410`. Leave `pty_session.py`, `headsup.py` (already branched).
   - `os.setsid()` unguarded: `copilot_host.py:1717`, `session_host.py:311`,
     `_env_install_worker.py:2153` → `hasattr` guard. `os.sysconf("SC_OPEN_MAX")` at
     `session_host.py:293`, `copilot_host.py:219` → guard.
   - `os.geteuid` at `claude_cmd_log.py:40`, `diagnostics.py:435`, `claude_agent/agent.py:137`
     → guard with `hasattr`.
   - `os.statvfs` at `index/store.py:77`, `ai/benchmark.py:275`, `update/_manager.py:775` →
     use `shutil.disk_usage` where it gives the same number, else guard.
   - `os.nice` at `index/watcher.py:474` (and check `index/worker.py:68-100`) → guard.
   - `_env_install_worker.py:85,89,96` top-level `fcntl`/`pty`/`termios` → guarded; on Windows
     run uv with plain pipes (no pty) at `:1035`. Project venv builds must work on Windows.
   - `claude_config/lib.py:723` `open`/`xdg-open` → `os.startfile` on Windows.
   - `/bin/bash` fallbacks in `claude_health.py:222`, `copilot_health.py:163`, login-shell
     probes: on Windows skip the login-shell probe and use `shutil.which` on the plain PATH.

6. **Disable on Windows** (explicit `IS_WINDOWS` gate, logged once, feature reports unsupported
   via the existing patterns — capture-style `Unsupported`/409, or skip the startup handler):
   - bots (`_startup_bots`, server/app.py:~1163) — skip.
   - pty terminal (`pty_session.py`) — the terminal endpoint returns a clear unsupported error.
   - Claude remote control / `claude_agent/agent.py` top-level `fcntl` (line 78): make the
     import safe via the helper so chat/tasks load; if a specific sub-feature is POSIX-only,
     gate it rather than the whole agent.
   - `legacy_mounts_cleanup` already gated — confirm.
   Don't disable anything that the helper makes work.

7. **CI**: add `windows-latest` to the `minimal-install` matrix in `.github/workflows/test.yml`
   (~line 359). Read that job first and make its steps run on Windows (shell: bash is available
   on windows runners; adjust paths/venv activation `Scripts/` vs `bin/`). Keep it a smoke test:
   install the wheel with `[cli]`, `python -c "import fused_render.server"`, start
   `fused-render serve --no-browser --port <p>` in background, curl it, stop.

## Conventions
- Use `bun`/`bunx` not npm/npx for any JS tooling.
- Comments describe the code as it is, no history ("previously", "used to"). Match surrounding
  comment density.
- Don't leave dead code; delete what you replace.
- Scoped pytest only (`uv run pytest tests/test_x.py -k ...`). `/tmp` quota is small — don't run
  the full suite. Read the `setting-up-dev-env` project skill notes in `.claude/skills/` if you
  need the dev env.
- Commit per logical unit. Record decisions/dead ends in `DECISIONS.md` in the worktree root
  (delete it with this spec in the last commit, after moving anything important into the PR
  notes in your final report).
