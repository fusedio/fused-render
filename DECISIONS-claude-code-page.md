# Claude Code page: decisions and open items

- `/claude-code?tab=<tab>` hosts everything that was Preferences' Claude Code section. Tabs: Skills (default), Memory, Plugins, MCP servers, Settings, History. The tab is written with `replaceState`.
- The page is reached from the settings popover (directly above Preferences), not from primary nav, so the sidebar never lights a row and the trigger at once.
- Legacy `/claude-config`, `/claude-md` and `/preferences?tab=claude*` / `?s=claude-*` rewrite to `/claude-code?tab=...` in `router.ts`.
- Preferences keeps its own settings, including "AI & agents"; its Claude model pointer row links to this page.
- Settings tab rows reuse `SettingRow`, whose entry type requires a `GroupId`; they use `group: "ai"`. The group is only read by search crumbs, which this page never runs.
- Page skeleton mirrors Preferences: `.settings-header` (title + filter), `.settings-nav` with the six tabs, `.settings-main` with an h2 + description. History hides the filter and carries the git chip in its head row. Switching tab clears the filter and drops `?skill=`.
- Skills is master-detail: `?tab=skills` lists (one `.settings-card`, a button per skill: name, one muted description line); `?tab=skills&skill=<name>` shows the editor. SkillsSection draws its own head (New skill in the list; back, link pill, Reveal, Delete, Save in the editor). Sibling files are tabs; the textarea is the only child of a card, fixed 60vh minimum, vertically resizable. Saved note fades after 2.5s (no toast); errors stay inline.
- Switching files or going Back with unsaved edits asks Discard / Keep editing. Navigating via the left nav while dirty does not ask.
- The skill install-command copy action was dropped with the per-row icon buttons.
- Memory, Plugins and MCP own their head (shared `Head` in bits.tsx): the live count is the description line, actions sit right of the h2. History keeps `SectionToolbar`.
- Memory files are editable the same way; deleting one commits the deletion.
- `claude plugin uninstall` has no `-y`; uninstall runs at user scope after checking the id is installed.
- `mcp spec` reads `~/.claude.json` read-only to prefill Edit; Edit is remove + add-json (not atomic).
- The filter input sits above the tab's list and matches names/descriptions of that tab's rows; it does not index across tabs. History has no filter.
- The git chip shows on the History tab header only.
- Shared CSS stays in `preferences.css` / `claude-config.css`.
- Plugins has no search box of its own: the page filter drives both lists. The empty state's clear button only resets the marketplace filter. Installed/Discover control and refresh are the head's aside.
- `plural(n, one, many?)` lives in bits.tsx; Memory, MCP and Plugins use it.
- `SectionToolbar`, `.cc-toolbar*` and `.cc-summary` stay: History and Profiles (being removed) still use them; the removal builder deletes them with those.
- Screenshots: v3-memory, v3-mcp (caught in loading state), v3-plugins. No v3-history.

## Unfinished
- No UI check was done; typecheck passes.
- The Skills editor edits files already in the skill folder; adding a new sibling file is not supported.
