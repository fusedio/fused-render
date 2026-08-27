---
name: fused-render-theming
description: Light/dark theming for a fused-render view — data-fused-theme="shell", the token rules, and colours handed to canvas/charts/maplibre. Use when a view must follow the app's appearance or looks wrong in the opposite one.
---

# Style and theming a view

There is no imposed CSS — the iframe is a blank canvas, and by default nothing is
written into your document: no class, no attribute, no stylesheet. But the
explorer around it follows the OS light/dark preference, with a Light/Dark
override in Preferences → Appearance, so a hardcoded palette will sooner or later
sit inside the opposite one.

Pick one of these and commit to it — the failure mode is picking none and
half-following.

**1. Fixed palette.** Fine for a view with its own strong look (a dark map, a
photo grid). Just don't pretend to follow.

**2. Follow the app** — one attribute, no JS. Put `data-fused-theme="shell"` on
your `<html>` and the injected runtime resolves the app's setting, writes
`data-theme="light"`/`"dark"` on that same element before your stylesheet is
parsed, and keeps it in step afterwards — including an in-app pin, an OS flip
mid-session, and a change made in another window. Author against the attribute:

```html
<html data-fused-theme="shell">
```
```css
:root       { color-scheme: dark;  --bg: #101318; --text: #dce2ea; --line: #2a303a; }
:root[data-theme="light"]
            { color-scheme: light; --bg: #f7f8fa; --text: #1a1f27; --line: #d8dce3; }
body        { background: var(--bg); color: var(--text); }
```

This is what the built-in templates use (`SPEC.md` §30, AP-8/AP-9), and it is the
only option that agrees with the app when the user pins Light or Dark.

**3. Follow the desktop** — `@media (prefers-color-scheme: light)` around the
second `:root`, same tokens, no attribute. Tracks the OS, which is what the app's
default System mode tracks too. It does *not* see an in-app pin.

**4. Your own switcher.** Put the choice in a param
(`fused.params.set("theme", …)`) so it is bookmarkable like the rest of your view
state, and drive the same one `data-theme` attribute from it. Use it *instead of*
option 2, never alongside: the runtime re-applies on every storage/OS event, so
your button would silently lose to the app setting.

## Two rules that make the second palette actually work

- **Every colour comes from a token.** Two blocks defining *the same token set*,
  and no colour literal anywhere else in the stylesheet. A stray `#1a1f27` in a
  rule is one the other mode cannot repaint — and it shows up as an unreadable
  smear, not an obvious bug.
- **Colours you hand to JS don't follow.** Canvas fills, chart ramps, maplibre
  paint expressions — `var()` does not resolve inside a JS string. Read them at
  *draw* time and redraw when the attribute changes:

  ```js
  const token = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  new MutationObserver(() => redraw())
    .observe(document.documentElement, { attributeFilter: ["data-theme"] });
  ```

Do not read the app's own `localStorage` key. It is private, it is not part of
`window.fused`, and a view that reads it becomes a second copy of a resolution
rule that will drift from the first — options 2 and 3 both get you the answer
without one.

Related: **`fused-render-authoring`** (everything else about writing the page).
