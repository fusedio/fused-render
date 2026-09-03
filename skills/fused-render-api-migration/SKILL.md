---
name: fused-render-api-migration
description: Use when migrating app to current fused page API — Migrate task, stale/missing fused-api-version meta, "fused.ai is not a function" after upgrade.
---

# API migration

Entry page declares API version in `<head>`: `<meta name="fused-api-version" content="N" />` (right after `<meta name="fused-app" />`, inside first 4 KiB). No tag = version 0. Each breaking change = one note `docs/vN.md` beside this SKILL.md; **current version = highest vN.md** — no other registry (server reads this folder too: `fused_render/fused_api_version.py`). Migrate A → B = apply `v{A+1}.md` … `v{B}.md` in order.

## Procedure (one pass, self-contained)

1. **FROM**: read entry page's meta tag (task target = page with `fused-app`); no tag = 0. Task text naming versions wins.
2. **TO**: highest `docs/vN.md` here. FROM ≥ TO → ensure tag present + correct, stop.
3. **Read every note in (FROM, TO], oldest first, ALL before editing** — symbol renamed twice must land on final name.
4. **Sweep whole folder** (subfolders; skip `.fused/`, `node_modules/`): `fused.*` in html, `import fused_ai`/`fused_ai.*` in py, direct `fetch("/api/ai…")`. Rewrite call shapes AND result readers per notes. Verify what each site actually calls first — already-current sites stay. No restyling beyond notes.
5. **Declare**: set `content="TO"` on meta tag (add/replace, directly after `fused-app`).
6. **Check**: re-grep every OLD spelling notes list; none remain outside obvious non-call strings.
7. **Report**: FROM → TO, files changed, anything left + why.

Diagnosing only ("fused.ai is not a function", `res.model` undefined): read note that introduced shape — `docs/v1.md` covers fused.ai namespace + result frame.

## Maintainers: shipping new version

1. Add `docs/v{N}.md` here (what changed, old → new, checklist). 2. Bump `content="N"` in `fused_render/app_starter/index.html`. Nothing else — server, Migrate button, task all key off this folder.
