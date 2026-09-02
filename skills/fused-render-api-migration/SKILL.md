---
name: fused-render-api-migration
description: Migrate a fused-render app from an older version of the `fused` page API to the current one. Use when a task says to migrate an app, when the app page's Migrate button created the task, when an entry page's `<meta name="fused-api-version">` is behind (or missing), or when an app breaks with "fused.ai is not a function" or an unexpected result shape after a fused-render upgrade. Self-contained — reads the per-version notes under docs/ and does the whole migration.
---

# Migrating an app across fused API versions

Every fused-render app declares which shape of the `fused` runtime it was written against, in its entry page's `<head>`, right after the app marker:

```html
<meta name="fused-app" />
<meta name="fused-api-version" content="1" />
```

A page **without** the tag is **version 0** (authored before versioning existed). The runtime moves on with breaking changes from time to time; each change is written up once, as one file under `docs/` beside this skill:

```
docs/v1.md    what changed in version 1 over version 0
docs/v2.md    what changed in version 2 over version 1
...
```

**The current API version is the highest `docs/vN.md` that exists.** There is no other registry. Migrating from A to B means applying `docs/v{A+1}.md` … `docs/v{B}.md`, **in order** — v2 → v5 reads v3, v4, v5.

## Procedure

Do all of it in one pass; the task that invoked you does not add anything beyond this file.

1. **Find the entry page and its declared version.** The task's target file is the entry page (it carries `<meta name="fused-app">`). Read its head. `content="N"` on `<meta name="fused-api-version">` is the FROM version; no tag = 0. If the task text names the versions ("from 0 to 1"), trust that.
2. **Find the current version.** List `docs/` beside this SKILL.md; the highest `vN.md` is the TO version. If FROM ≥ TO, the app is already current: make sure the tag is present and correct (step 5) and stop.
3. **Read every note in `(FROM, TO]`, oldest first.** Each note is self-contained: what changed, old spelling → new spelling, and a checklist. Read all of them before editing so a symbol renamed in v3 and again in v4 lands on its final name.
4. **Sweep the whole app folder, not just the entry page.** Every `.html` and `.py` in the folder (subfolders included, `.fused/` and `node_modules/` excluded) that touches the runtime: `fused.*` in pages, `import fused_ai` / `fused_ai.*` in Python, and direct `fetch("/api/ai…")` calls. For each site, apply the notes' rewrites — the call shape AND every reader of its result (`res.model`, `usage.input_tokens`, `res.url`, callback field names, …). **Verify what the code actually calls before changing it**: a page may already be on newer shapes without declaring so — leave those sites alone. Do not restyle, restructure or "improve" anything the notes do not ask for.
5. **Declare the new version.** In the entry page, place `<meta name="fused-api-version" content="TO" />` directly after `<meta name="fused-app" />` (replace an existing `fused-api-version` tag). Both tags stay inside the first 4 KiB of the file — detection reads only the head.
6. **Check it.** Re-grep the folder for every OLD spelling listed in the notes you applied (`fused.ai(`, `.input_tokens`, `onSegment`, …); none should remain except in strings that are clearly not runtime calls. If the app has a `.py` that the page calls through `fused.runPython`, read it once more for result-shape readers.
7. **Report.** One short summary: FROM → TO, which files changed, anything you deliberately left (a site you could not confidently rewrite — name it and say why).

## Which notes to read

| Situation | Read |
|---|---|
| Tag missing, or `content="0"` | every `docs/vN.md`, ascending |
| `content="K"` | `docs/v{K+1}.md` … highest, ascending |
| Only diagnosing ("fused.ai is not a function", `res.model` undefined) | the note that introduced the shape — `docs/v1.md` covers the `fused.ai` namespace and result-frame break |

## Maintainers: shipping a new API version

When a fused-render PR breaks the page API:

1. add `docs/v{N}.md` here — what changed over v{N-1}, old → new, and a checklist a migrating session can act on;
2. bump `content="N"` on the starter's `<meta name="fused-api-version">` (`fused_render/app_starter/index.html`).

Nothing else: the server reads the current version off this `docs/` folder (`fused_render/fused_api_version.py`), the app page's Migrate button compares against it, and the task it creates invokes this skill.
