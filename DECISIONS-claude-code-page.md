# Claude Code page: decisions and open items

- `/claude-code?tab=<tab>` hosts everything that was Preferences' Claude Code section. Tabs: Skills (default), Memory, Plugins, MCP servers, Settings. The tab is written with `replaceState`.
- The page is reached from the settings popover (directly above Preferences), not from primary nav, so the sidebar never lights a row and the trigger at once.
- Legacy `/claude-config`, `/claude-md` and `/preferences?tab=claude*` / `?s=claude-*` rewrite to `/claude-code?tab=...` in `router.ts`.
- Preferences keeps its own settings, including "AI & agents"; its Claude model pointer row links to this page.
- Settings tab rows reuse `SettingRow`, whose entry type requires a `GroupId`; they use `group: "ai"`. The group is only read by search crumbs, which this page never runs.
- Page skeleton mirrors Preferences: `.settings-header` (title + filter), `.settings-nav` with the five tabs, `.settings-main` with an h2 + description. The filter shows on every tab. Switching tab clears the filter and drops `?skill=`.
- Skills is master-detail: `?tab=skills` lists (one `.settings-card`, a button per skill: name, one muted description line); `?tab=skills&skill=<name>` shows the editor. SkillsSection draws its own head (New skill in the list; back, link pill, Reveal, Delete, Save in the editor). Sibling files are tabs; the textarea is the only child of a card, fixed 60vh minimum, vertically resizable. Saved note fades after 2.5s (no toast); errors stay inline.
- Switching files or going Back with unsaved edits asks Discard / Keep editing. Navigating via the left nav while dirty does not ask.
- The skill install-command copy action was dropped with the per-row icon buttons.
- Memory, Plugins and MCP own their head (shared `Head` in bits.tsx): the live count is the description line, actions sit right of the h2.
- Memory files are editable the same way; deleting one is final (the confirm says it can't be undone).
- `claude plugin uninstall` has no `-y`; uninstall runs at user scope after checking the id is installed.
- `mcp spec` reads `~/.claude.json` read-only to prefill Edit; Edit is remove + add-json (not atomic).
- The filter input sits above the tab's list and matches names/descriptions of that tab's rows; it does not index across tabs.
- Shared CSS stays in `preferences.css` / `claude-config.css`.
- Plugins has no search box of its own: the page filter drives both lists. The empty state's clear button only resets the marketplace filter. Installed/Discover control and refresh are the head's aside.
- `plural(n, one, many?)` lives in bits.tsx; Memory, MCP and Plugins use it.
- Screenshots: v3-memory, v3-mcp (caught in loading state), v3-plugins. v4-memory is the check after the removal below.

## Removal of git versioning, History and catalog refresh
- The app no longer manages a git repo in `~/.claude`: no commits on edit, no History, drift or Profiles. `git_ops.py`, `profiles.py` and the git half of `lib.py` are deleted, as are `HistorySection`, `ProfilesSection`, the git chip and the `/api/claude-config/{git_ops,profiles}` modules.
- Legacy `cctab=history|profiles` and `claude-history` links fall through to the default tab rather than a dead one.
- `onChanged`, `sectionEpoch` and `badgeEpoch` existed only to refresh the chip and to commit; they are gone from every section and from the store. `useChangePreview` became `useConfirm` (a note and buttons; nothing ever passed it a diff).
- `SectionToolbar`, `Group`, `Row`, `.cc-toolbar*`, `.cc-row*`, `.cc-scalar`, `.cc-gitchip`, `.cc-delta`, `.cc-fstat`, `.cc-pick`, `.cc-row-hint` and the unused icons (edit, eye, clock, undo, info, check) are deleted. `.cc-summary` stays for the Plugins pager. The Plugins marketplace rail sticks at `top: 0` now that no toolbar sits above it.
- Memory `clear` and CLAUDE.md `delete` are plain deletes. Memory's per-project Commit button and change badge are gone.
- The settings-catalog refresh is gone: the Catalog card, `ClaudeCatalogControl`, `refreshCatalog`, the `refresh_catalog` server action and router entry, and the user-dir catalog override (`catalog_override_path`, `catalog_read_path`). `load_catalog` reads the packaged `settings_catalog.json` only.
- `refresh_catalog.py` stays as a development script that rewrites the packaged JSON; its parser tests stay.
- `lib.home_dir` stays (`mcp.py` uses it) and `SUBPROCESS_KWARGS` stays (`claude_cli` needs it).

## Unfinished
- No UI check was done; typecheck passes.
- The Skills editor edits files already in the skill folder; adding a new sibling file is not supported.

## Curated Settings tab
- The Settings tab lists only settings.json keys that change how the Claude sessions fused-render launches behave. 16 keys in three groups: Model & reasoning (model, effortLevel, alwaysThinkingEnabled, fastMode, language), Sessions (autoCompactEnabled, autoMemoryEnabled, fileCheckpointingEnabled, cleanupPeriodDays, disableBundledSkills, includeGitInstructions, attribution.sessionUrl), Permissions (permissions.defaultMode, sandbox.enabled, enableAllProjectMcpServers, permissions.blockReadsOutsideWorkingDirectories). Terminal-UI keys (theme, diffTool, update channel) and worktree keys (fused-render manages worktrees itself) are out. `settings_catalog.json` holds only those, in that order, with the group names as its `group` field.
- `refresh_catalog.CURATED` (key -> group, ordered) is the source of truth: the script emits only those keys in that order and takes each one's label/control/options overlay from the checked-in file. A curated key with no overlay is an error, not invented.

## Skills tab lists plugin skills
- `skills.list` returns the user's skills plus those of every plugin that is true in settings.json `enabledPlugins` (not installed_plugins.json, which only holds `installPath`), read from the current `installPath` only. Plugin skills reuse `plugins._manifest`, `_skills` and `_within`; the walk budget is 5000 entries per plugin.
- A skill is addressed by `source` + `plugin` + `slug` (+ optional `marketplace`). A plugin skill's dir is found by re-listing that plugin, so it always lives under its installPath. `write` and `delete` refuse any non-user source; `read` and `open` (Reveal) work for both.
- The `shareCommand`/`source` (lockfile origin) fields and `SKILL_LOCK_PATH` are gone; `source` now means "user" | "plugin".
- URL: `?skill=omarchy` (user) or `?skill=plugin:<plugin>/<slug>`. List: "Your skills" then "From plugins" (sorted by plugin, slug), each plugin row carries a muted `.cc-pill` with the plugin name. The filter matches name, slug, description and plugin; a group with no match is hidden, but "Your skills" always shows its empty card ("No skills of your own yet.") when unfiltered.
- Plugin detail: pill shows the plugin, read-only textarea, no Save/Delete, note "Comes with the <plugin> plugin. Updates to the plugin replace it." sits in the blurb after the description.
