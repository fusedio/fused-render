# Agents page: decisions and open items

- `/agents?h=<harness>&tab=<tab>` hosts everything that was Preferences' Claude Code section. Harness tabs and per-harness tabs come from `GET /api/agents/harnesses` (capabilities in `fused_render/agent_backends/homes.py`).
- Legacy `/claude-config`, `/claude-md` and `/preferences?tab=claude*` rewrite to `/agents?h=claude&tab=...` in `router.ts`.
- Preferences keeps its own settings, including "AI & agents"; its Claude pointer row links to Agents.
- Settings tab rows reuse `SettingRow`, whose entry type requires a `GroupId`; they use `group: "ai"`. The group is only read by search crumbs, which the Agents page never runs.
- Codex MCP writes are line-level edits of `[mcp_servers.<name>]` tables in `config.toml` with a minimal TOML emitter, validated by `tomllib`. Comments inside edited tables are not preserved.
- `claude plugin uninstall` has no `-y`; uninstall runs at user scope after checking the id is installed.
- `mcp spec` reads `~/.claude.json` read-only to prefill Edit; Edit is remove + add-json (not atomic).
- The filter input is per tab (names/descriptions of the current tab's rows); it does not index across tabs.
- The git chip shows on the History tab header only.
- Shared CSS stays in `preferences.css` / `claude-config.css`.
- Removed: the claude registry entries/groups/nav section, `claude.test.ts` (it tested registry search of those rows), and the registry tests for them.

## Unfinished
- No UI check was done (no dev server was run); typecheck and the router/registry unit tests pass.
- Copilot/Codex "History", plugins and skills are not offered; Codex/Copilot MCP removal uses `window.confirm`.
- The Skills editor edits files already in the skill folder; adding a new sibling file is not supported.
- The harness switcher shows all three harnesses; a disabled one shows a notice linking to Preferences › AI & agents but stays editable.
