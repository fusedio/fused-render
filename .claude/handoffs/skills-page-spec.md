# Skills page + Claude Code page split — build spec

Approved mockup (open it, it is the visual source of truth):
`/Users/iamsdas/Work/fused-render/.claude/handoffs/claude-config-redesign.html`
Screens: `?screen=menu|skills|skills-q|new|new-draft|plugins|plugins-q|mcp`.

Branch `skills-page`, worktree `/Users/iamsdas/Work/fused-render/.worktrees/skills-page`.
Commit `e5d432a77` (cherry-picked) already adds `skills.py` actions `search`, `remote_preview`,
`remote_add`, `preview` + `tests/test_skills_remote.py`. It was never verified — run that test
file first and fix it if red (it errored once in another worktree, possibly an env issue).

## Hard rules (from the user)
- NO popularity anywhere: never show skills.sh `installs`, no counts-based ranking, no "popular".
- Skills search = installed skills + skills.sh ONLY. It must not search plugin marketplaces.
  Remove the old Discover view that listed marketplace plugin skills (`skills.available` /
  `skill-discover.tsx` grouped rows) from the UI. Backend actions may stay.
- Plugins stay in Claude Code with their own search (installed + `plugins.available`).
- Icon for "Claude Code": reuse `ClaudeMark` from `frontend/src/platform/ui/ClaudeMark.tsx`, the
  same way `frontend/src/shell/Preferences.tsx:70-74` does (`ClaudeNavIcon`, `scale-[0.85]`).
  Do NOT add a new SVG asset.
- Use `bun`/`bunx`, never npm/npx. Tailwind runs without preflight (spell out `m-0` on h1/h2/p).
- Do NOT start or restart `scripts/dev.sh` and do not start any server; the user runs it.
- Do NOT run the full pytest or full `bun test` suite. Scoped tests only (see Testing).
- `DECISIONS.md` at repo root is append-only.

## 1. Settings menu (`frontend/src/shell/GlobalSidebar.tsx`)
- `menuEntries` (~line 706-737): order Templates, Canvases, **Skills** (`/skills`, lucide
  `Sparkles` 16px), **Claude Code** (`/claude-config`, label "Claude Code", icon ClaudeMark at
  16px/scale 0.85), Preferences, separator, Help.
- Replace `CLAUDE_CODE_ICON` (line ~118, lucide Terminal) and its comment; drop the `Terminal`
  import if unused.
- `prefsActive` must also light for `/skills`.

## 2. Routing
- New shell page `/skills` (lazy, like `ClaudeConfig` in `frontend/src/shell/App.tsx:129,737,810,
  839,969-976`): chrome-free settings-style page, same treatment as `isClaudeConfig`.
- `frontend/src/platform/lib/router.ts`: `/claude-config?tab=skills` (and the legacy
  `/claude-md?cctab=skills`, `/preferences?...` paths that resolve to tab `skills`) now resolve to
  `/skills`, keeping `?skill=` and other params. Update `router.test.ts` accordingly.
- Grep for every link that builds `/claude-config?tab=skills` or opens the skills tab (e.g.
  `frontend/src/apps/bots/dialogs/*`, `apps/explorer/lib/fs-actions.ts`, server-side
  notifications in `fused_render/`) and point it at `/skills`.
- Page/tab title for `/skills`: "Skills". Rename user-visible "Claude Config"/"Claude setup"
  labels for `/claude-config` to "Claude Code" (grep `originRoutes` / route labels).

## 3. Claude Code page (`frontend/src/shell/ClaudeConfig.tsx`, `shell/claude_config/tabs.tsx`)
- TABS = plugins (default), mcp, memory. Remove the skills tab. Header icon = ClaudeMark.
- Under the nav, a faint line: "Skills moved to their own page. Open Skills" linking `/skills`.
- Plugins tab: replace the Installed | Discover segmented switch with ONE search box. Empty query
  → Installed list (as today) + a dashed "Your N marketplaces list M more plugins. Type to search
  them." row with a "Browse all" button (shows all available). Typed query → "Installed" matches,
  then "Available in your marketplaces" matches from `plugins.available`, each with Install. Keep
  existing preview/install/update/toggle behaviour and the Marketplaces button.
- MCP tab: unchanged except (if not already there) a health label per server from the data the
  backend already returns. Don't add new backend for MCP health if none exists — skip it then.

## 4. Skills page (new, e.g. `frontend/src/shell/Skills.tsx` reusing
`frontend/src/apps/claude_config/sections/SkillsSection.tsx` pieces)
- Header: Sparkles + "Skills", search box (placeholder "Search your skills and skills.sh",
  `/` focuses it), primary "New skill" button.
- Empty query: blurb "Each skill teaches your coding CLIs how to do one job.", then
  "Your skills" (user skills; flat two-line rows: name + CLI reader tags on line 1, description
  on line 2, location on the right; no truncation of names), then "From Claude Code plugins"
  (read-only, "managed in Claude Code → Plugins"), then a dashed hint "Type 2+ letters to search
  skills.sh too."
- Typed (>=2 chars): "Installed" matches (client-side filter on name+description), then
  "Add from skills.sh" from `skills.search` (debounced ~250ms; show name + `owner/repo`; an
  "Add" button or "Added" state). Selecting a skills.sh row opens a right-hand preview using
  `skills.remote_preview` (SKILL.md body without frontmatter, file list, "Add skill", "View on
  GitHub ↗", "Read by" CLI tags from `readers.ts`). Add → `skills.remote_add`, then refresh list.
- skills.sh failure: "Couldn't reach skills.sh." with Retry; installed matches still show.
- No matches anywhere: empty state with a "Write a "<q>" skill" chip that opens New skill with the
  query prefilled in the description.
- `?skill=<key>` still opens the existing skill detail/editor (`skill-detail.tsx`) on this page.
- Delete `skill-discover.tsx` and its render test if nothing else imports them.

## 5. New skill — Draft with Claude (the only creation path)
UI (mockup screens `new`, `new-draft`), at `/skills?new=1`:
- "What should the skill do?" textarea (required).
- "Start from an existing skill" — optional path input (folder or SKILL.md; `~` allowed).
  On blur/debounce call backend validation; show "✓ Found SKILL.md + N other files. Claude adapts
  it rather than starting blank." or an inline error.
- "Who reads it": radio — Every CLI you have on (`~/.agents/skills`, linked from
  `~/.claude/skills`; default) / Claude Code only (`~/.claude/skills`).
- Footer: "Runs as a Claude task. You review the files before anything is saved." Cancel /
  Draft it.
- After Draft it: a "Draft" card showing task progress (link "Open task" to the task), then when
  SKILL.md exists: summary (name, files), an "Ask for changes" input (sends a follow-up to the same
  task), "Save skill", "Edit before saving" (opens the staged SKILL.md in the existing editor
  component, writing to the staging dir). Right aside: live SKILL.md (raw, frontmatter
  highlighted) + file list; before a draft exists the aside explains what Claude writes.
- Validation before Save (show failures inline, disable Save): frontmatter `name` matches
  `^[a-z0-9][a-z0-9_-]*$`, <=64 chars; `description` present, <=1024 chars; target slug doesn't
  already exist (offer a different name rather than overwrite).

Backend (`fused_render/claude_config/skills.py`, `main(action=...)`, POST
`/api/claude-config/skills`, mutating calls need `X-Fused`):
- `draft_check(path)` → resolves `~`, accepts a folder containing SKILL.md or a SKILL.md file;
  returns `{ok, root, name, files}` or `{ok: false, error}`.
- `draft_start(source="")` → creates a fresh staging dir under the fused-render state dir (find
  where other state lives, e.g. `~/.fused-render/…`; NOT /tmp — see memory: /tmp chats reload
  empty), named `skill-drafts/<timestamp-rand>/`. If `source` given, copy it into the staging dir
  as the starting point (reuse `_copy_skill` caps). Returns `{staging}`.
- `draft_status(staging)` → files list, raw SKILL.md text (or null), parsed name/description,
  validation problems list.
- `draft_save(staging, placement)` → validate, then place via the existing `_place(slug, fill)`
  (placement "all" = today's default; "claude" = `~/.claude/skills` only — add that option to
  `_place`/`_placement` without changing current callers' behaviour), then delete staging.
  Returns the new skill key so the UI can navigate to `?skill=`.
- `draft_discard(staging)` → delete staging. Every staging arg must be verified to live inside
  the skill-drafts root (no path traversal).
- The Claude task itself is started from the frontend with the existing tasks API:
  `POST /api/tasks/create` `{prompt, target: <staging>, title: "Draft skill: …", queue: false}`
  (see `fused_render/static/runtime.js:6722-6750` for the body shape and how follow-ups are sent —
  `tasksSend`). Find the shell-side helper/fetch convention used by other shell pages for tasks
  rather than inventing one. The prompt (build it in one place, unit-tested) tells Claude: write
  exactly one skill folder in the cwd (SKILL.md with YAML frontmatter `name` + `description`
  starting "Use when…", body with steps; references/ or scripts/ only if needed), adapt the
  existing copy if present, include the user's description verbatim, don't touch anything outside
  the cwd.
- Poll `draft_status` every ~2s while the task runs and once after it finishes.

## Testing (scoped only)
- Python: `pytest tests/test_skills_remote.py` + a new `tests/test_skills_draft.py` (check, start,
  status, save for both placements, discard, traversal rejection) + any existing
  claude-config tests you touch, by file/`-k`.
- Frontend: `bun test <file>` for router.test.ts, ClaudeConfig.render.test.tsx, the new Skills
  page render test, GlobalSidebar tests touching menuEntries. Note memory: some pytest files grep
  literal frontend source lines (`grep -rn "GlobalSidebar\|ClaudeConfig\|claude-config" tests/`)
  — run those too.
- `bun run build` in `frontend/` once at the end (catches CSS/TS errors tests miss).
- Env setup: follow the `setting-up-dev-env` skill for this worktree (own .venv, `bun install`),
  so tests import THIS worktree, not the main checkout.

## Commits
One per unit: menu+icon+routing; Claude Code page (tabs + plugins search); Skills page;
draft backend; New skill UI; cleanup. Messages end with:
`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## Builder notes

- Commit trailers: commits 1-2 end with the Opus 5.5 line from the spec; commits 3 onward use `Claude Sonnet 5.5` (the session's attribution reminder changed).
- `draft_start` takes `path` (not `source`): `source` already means "user"/"plugin" in `skills.main`. The staging dir is the skill folder itself.
- `draft_prompt(text, adapting)` is a backend action so the prompt is built and unit-tested in one place.
- Added `draft_write(staging, path, text)` (not in the spec) so "Edit before saving" and "Use this name" write into staging, never into a skills dir. The editor is a plain textarea, not `SkillDetail` (which writes to real skill dirs).
- `tasks.ts` gained `queue?: boolean` on `TaskSpec` and exported `tasksSend` / `tasksCancel`.
- Dead CSS left in place: `.cc-sgroup*`, `.cc-skill-new` (harmless; removal is a separate cleanup).
- Pre-existing red, unrelated to this diff (canvases.py untouched): `tests/test_skill_plugin.py::test_both_canvas_entry_points_fetch_and_neither_can_fail_over_it`.
- Spec error: `/claude-config?tab=skills` legacy redirects were handled in router.ts; `skills.available` backend action kept but unused by the UI.
