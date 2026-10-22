# Claude Code page: decisions and open items

- `/claude-code?tab=<tab>` hosts everything that was Preferences' Claude Code section. Tabs: Skills (default), Memory, Plugins, MCP servers, Settings, History. The tab is written with `replaceState`.
- The page is reached from the settings popover (directly above Preferences), not from primary nav, so the sidebar never lights a row and the trigger at once.
- Legacy `/claude-config`, `/claude-md` and `/preferences?tab=claude*` / `?s=claude-*` rewrite to `/claude-code?tab=...` in `router.ts`.
- Preferences keeps its own settings, including "AI & agents"; its Claude model pointer row links to this page.
- Settings tab rows reuse `SettingRow`, whose entry type requires a `GroupId`; they use `group: "ai"`. The group is only read by search crumbs, which this page never runs.
- Skills: list, filter, create (scaffold), delete, and edit SKILL.md or any sibling file in `FileEditor` (monospace, grows to content, 20 rows minimum, Save disabled while unchanged, inline saved/error note). Mutations commit to the config repo.
- Memory files are editable the same way; deleting one commits the deletion.
- `claude plugin uninstall` has no `-y`; uninstall runs at user scope after checking the id is installed.
- `mcp spec` reads `~/.claude.json` read-only to prefill Edit; Edit is remove + add-json (not atomic).
- The filter input sits above the tab's list and matches names/descriptions of that tab's rows; it does not index across tabs. History has no filter.
- The git chip shows on the History tab header only.
- Shared CSS stays in `preferences.css` / `claude-config.css`.

## Unfinished
- No UI check was done (no dev server was run); typecheck passes.
- The Skills editor edits files already in the skill folder; adding a new sibling file is not supported.
- Switching between a skill's files discards unsaved edits without asking.
