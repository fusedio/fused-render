---
name: fused-render-api-migration
description: Use when migrating a fused-render app to the current fused page API — a Migrate task, a behind/missing fused-api-version meta tag, or "fused.ai is not a function" after an upgrade.
---

# API migration

Entry pages declare their API version in `<head>`: `<meta name="fused-api-version" content="N" />` (right after `<meta name="fused-app" />`, inside the first 4 KiB). No tag = version 0. Each breaking change is one note `docs/vN.md` beside this SKILL.md; **the current version is the highest vN.md** — no other registry (the server reads this folder too: `fused_render/fused_api_version.py`). Migrating A → B = apply `v{A+1}.md` … `v{B}.md` in order.

## Procedure (one pass, self-contained)

1. **FROM**: read the entry page's meta tag (task target = the page with `fused-app`); no tag = 0. Task text naming versions wins.
2. **TO**: highest `docs/vN.md` here. FROM ≥ TO → ensure the tag is present and correct, stop.
3. **Read every note in (FROM, TO], oldest first, all before editing** — a symbol renamed twice must land on its final name.
4. **Sweep the whole folder** (subfolders; skip `.fused/`, `node_modules/`): `fused.*` in html, `import fused_ai`/`fused_ai.*` in py, direct `fetch("/api/ai…")`. Rewrite call shapes AND result readers per the notes. Verify what each site actually calls first — leave already-current sites alone. No restyling beyond what the notes ask.
5. **Declare**: set `content="TO"` on the meta tag (add/replace, directly after `fused-app`).
6. **Check**: re-grep for every OLD spelling the notes list; none should remain outside obvious non-call strings.
7. **Report**: FROM → TO, files changed, anything deliberately left and why.

Diagnosing only ("fused.ai is not a function", `res.model` undefined): read the note that introduced the shape — `docs/v1.md` covers the fused.ai namespace + result frame.

## Maintainers: shipping a new version

1. Add `docs/v{N}.md` here (what changed, old → new, checklist). 2. Bump `content="N"` in `fused_render/app_starter/index.html`. Nothing else — the server, the Migrate button and its task all key off this folder.
