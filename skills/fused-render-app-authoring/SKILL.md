---
name: fused-render-app-authoring
description: How to turn a folder into a fused-render app — a fused_app.json manifest that makes the folder render as a multi-page app (sidebar nav + routed pages) instead of a file listing, with ?route= URLs and fused.navigate() for in-app navigation. Use this whenever the user wants to "make this folder an app", asks about fused_app.json, wants to add a page/route to an app, group several views into one navigable app, or asks about app navigation between views / fused.navigate. For writing the individual page html/py files, this skill delegates to the fused-render-authoring skill — read that one too.
---

# Authoring a fused-render app

A **fused app** is a folder that renders as an application instead of a file listing: a left sidebar (title, description, pages nav) and a main frame showing the current page. What makes it one is a single file — `fused_app.json` at the folder root. Each page is an **ordinary fused-render html view** (same `window.fused` runtime, `runPython`, params — see the `fused-render-authoring` skill for writing them); the app layer only adds the manifest, the routed shell, and `fused.navigate()`.

Reference app: `examples_seed/demo_app/` in the fused-render repo (3 pages, one with a live `runPython` call and `fused.navigate` buttons).

**Division of labor between skills (do not duplicate):**

- **This skill:** the manifest grammar, what makes a folder valid, routing/URLs, `fused.navigate`.
- **`fused-render-authoring`:** how each page's html and its sibling `.py` are written.

## What makes a folder an app (validity rules)

The server gates the app view with a fail-closed condition — the folder renders as an app **only if all of these hold**:

1. `fused_app.json` exists at the folder root and is **≤ 256 KiB**.
2. It parses as a **JSON object** (not an array/scalar).
3. It has a **`pages` array** (required).
4. `pages` contains an item with **`path: "/"`** — the entry page — whose `file` is a string.
5. That entry file **exists inside the folder**. Absolute paths and any `..` segment are rejected — pages live in the app folder.

Anything else — missing manifest, malformed JSON, no `"/"` page, dangling or escaping file — fails closed: the folder stays a plain listing, never an error. There is **no `entry` field**; the entry page is defined by the `"/"` route.

## Manifest grammar

```json
{
  "fused_app": 1,
  "name": "demo-app",
  "title": "Demo App",
  "description": "One line shown under the title in the sidebar.",
  "version": "0.1.0",
  "author": { "name": "you" },
  "pages": [
    { "path": "/",      "file": "index.html", "title": "Overview" },
    { "path": "/stats", "file": "stats.html", "title": "Live Stats" },
    { "path": "/about", "file": "about.html", "title": "About" }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `pages` | **yes** | Ordered nav list. Each item: `path` (route, `"/"` = entry), `file` (html filename inside the folder), `title` (nav label; falls back to the filename). |
| `title` | no | App display name (sidebar header + tab title). Falls back to `name`, then `"App"`. |
| `name` | no | Slug. |
| `description` | no | Sidebar subtitle. |
| `version` | no | Shown as `v0.1.0` in the sidebar meta line. |
| `author.name` | no | Shown next to the version. |
| `fused_app` | no | Schema version marker (`1`); not validated today, include it anyway. |

Notes: the `"/"` page always sorts first in the nav regardless of position. Duplicate files or duplicate routes are dropped (first wins). A page item without a usable `path` derives its route from the filename sans `.html`.

## Routing and URLs

The open page rides the shell URL as `?route=<name>`:

- Route name = `pages[].path` with slashes stripped: `"/stats"` → `route=stats`.
- The **entry page (`"/"`) has no route param** — a clean *route*, not a clean URL: selecting it removes only `route`; other visible params stay.
- Refresh/bookmark restores the open page; Back/Forward work (the template follows URL changes).
- **Unknown route** → inline error bar ("Unknown route …") above the frame + the entry page renders. Never a blank view.
- **Missing page file** → inline "Page file not found" state, app shell stays up.

Example: `http://127.0.0.1:1777/view/<abs app dir>?route=stats`.

## fused.navigate(route, params?, config?)

Any rendered page can move the shell to another route of its **enclosing app** — the nearest ancestor directory of the *page's own file* holding a valid `fused_app.json` (resolved server-side via `/api/app/resolve`). Works from a page inside the app shell **and** from the same file opened standalone (`/view/…/stats.html`) — both land on `/view/<app dir>?route=…`, in place, no reload.

```js
// from demo_app?route=stats&p=10:
await fused.navigate("/about", { p: "11" });
// → demo_app?route=about&p=11      (merge: existing params kept, p overwritten)

await fused.navigate("/about", { p: "11" }, { params: "overwrite" });
// → demo_app?route=about&p=11      (overwrite: all other visible params dropped)

await fused.navigate("/");
// → demo_app?p=10                   (entry clears only the route param — other
//                                    params still follow the merge default)

await fused.navigate("/", {}, { params: "overwrite" });
// → demo_app                        (explicit overwrite: no visible params at all)
```

Semantics:

- `route` uses the manifest `pages[].path` spelling; `"/"` or `""` = entry — only the `route` param is removed, other params follow the merge/overwrite config.
- `params` values must be **strings** (same rule as `fused.params.set`); merged onto the shell URL's current params by default, replaced entirely with `config: {params: "overwrite"}`.
- Reserved `_`-prefixed shell params (`_mode`, `_layout`, …) are **preserved in both modes** — they are shell state, not yours; passing one in `params` rejects.
- Resolves with `{app_dir, url}`. **Rejects** when: no ancestor directory has a valid manifest ("no enclosing fused_app found"), a param key is reserved or `route`, a value isn't a string, or the page's own file path can't be determined.

## App view vs file listing (mode switcher)

The app view is one **mode** on the folder, gated by manifest validity; the plain file listing stays available:

- A valid app folder opens as the app **by default**; the header mode switcher (or the corner chip in embed) flips to the listing and back.
- `?_mode=_listing` pins the listing; `?_mode=fused_app` pins the app view. An explicit `_mode` survives navigation — `fused.navigate` never touches it.
- While the validity check resolves (it runs in the background), the listing may flash briefly before the app swaps in — expected.

## Workflow

1. Write the pages as ordinary views (per `fused-render-authoring`): loose `.html` files in the folder, sibling `.py` files next to them, relative `runPython` paths.
2. Add `fused_app.json` with a `pages` array whose `"/"` item points at the entry html.
3. Open the folder: `/view/<abs folder path>` (or `open -a FusedRender <folder>`). It renders as the app.
4. Wire cross-page links/buttons with `fused.navigate("/other", {...})`.
5. Sanity loop: click through nav → `?route=` updates → hard refresh → same page; flip to the listing via the mode switcher and back.
