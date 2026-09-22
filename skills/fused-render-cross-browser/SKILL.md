---
name: fused-render-cross-browser
description: Use when a fused-render view looks or behaves wrong in one desktop browser only (Chrome/Edge, Firefox, Safari/WebKit), or to check support for a CSS/JS feature before using it — deep reference behind the authoring skill's mandatory Cross-browser section.
---

# Cross-browser views (desktop)

A view is opened in whatever the user's **default desktop browser** is — Chrome/Edge, Firefox, Safari — and inside **WKWebView** (the macOS pinned popover, SPEC §25). **WebKit rules apply even when the author's browser is Chrome.** Test in one engine = shipped for one engine.

Scope is **computers only**: phones and tablets are out. Nothing here is about touch, viewport zoom, mobile toolbars or iOS/Android.

## Support target

- Use only features MDN marks **Baseline: Widely available**. Not in this file → check the MDN compat table. Never assume from Chrome.
- **Newly available** (last ~30 months) → `@supports (…) {}` with a fallback that still works, or drop it.
- Chrome-only → don't. There is no "just for now".
- No transpiler, no autoprefixer, no build step (authoring contract). Prefixes are hand-written where the table says so.

## Paste-in baseline

Drop into every view's `<style>`, beneath the theme tokens (`fused-render-theming`):

```css
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; }
button, input, select, textarea { font: inherit; color: inherit; margin: 0; }
button { background: none; border: 0; padding: 0; cursor: pointer; }
::placeholder { color: var(--muted); opacity: 1; }
input, select, textarea, progress { accent-color: var(--accent, currentColor); }
img, svg, video, canvas { display: block; max-width: 100%; }
```

Why: Safari buttons/inputs don't inherit font or colour; Firefox dims placeholders to 0.54 opacity; replaced elements default to inline baseline gaps.

## Trap table

| Area | Trap | Fix |
|---|---|---|
| Scrollbars | `::-webkit-scrollbar` invisible in Firefox; standard props reached Safari last (Baseline Dec 2025). An engine that supports `scrollbar-color` **ignores** `::-webkit-scrollbar` once it is set (inherits!). | Write both on the same element: `scrollbar-width: thin; scrollbar-color: var(--line) transparent;` plus `::-webkit-scrollbar { width: 8px } ::-webkit-scrollbar-thumb { background: var(--line) }`. Same colours in both so no engine looks different. |
| Selection | `user-select` still needs `-webkit-user-select` in Safari. | Always both, prefix first. |
| Line clamp | Needs the legacy trio. | `display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden;` |
| Blur panels | `backdrop-filter` unprefixed only Safari ≥ 18. | `-webkit-backdrop-filter: blur(8px); backdrop-filter: blur(8px);` plus a solid-ish `background` — blur is decoration, never what makes text readable. |
| `<select>` | Native arrow/padding differ per engine; Safari cannot style `<option>` at all. | `appearance: none; -webkit-appearance: none;` + own `background-image` arrow + `padding-right`. Options stay native — build a listbox if they must be styled. |
| `<input type=range>` | Thumb/track pseudo-elements are per engine. | Style both `::-webkit-slider-thumb` and `::-moz-range-thumb` (and the `-runnable-track` / `-moz-range-track` pair); start from `appearance: none`. |
| `<details>` | Marker differs; Safari keeps its own. | `summary { list-style: none } summary::-webkit-details-marker { display: none }` then draw your own. |
| Date/number/file/color inputs | Native chrome differs by design (Firefox has no number-spinner styling; Safari's pickers are its own). | Style the box only; assume the picker looks native. |
| `position: fixed` | Broken under a transformed/filtered ancestor (all engines). | Portal fixed elements to `<body>`; never `transform` a layout ancestor of a fixed child. |
| Sticky headers | `position: sticky` needs the `overflow` ancestor to be the actual scroller; `overflow: hidden` in between kills it (all engines). | Use `overflow: clip` for clipping, keep `sticky` directly in the scroller. |
| Scroll-driven animation | `animation-timeline: scroll()` Chrome-only. | Gate behind `@supports (animation-timeline: scroll())`, no fallback needed — decoration. |
| Fonts | System stack renders differently per OS; weight 500 falls back to 400 or 700 depending on the installed face. | Stack: `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif`; mono: `ui-monospace, Menlo, Consolas, monospace`. Weights 400/600/700 only unless loading a font. |
| Flex overflow | Safari overflows flex children holding text harder than Chrome. | `min-width: 0` on any flex child that holds text or `overflow`. |
| `aspect-ratio`, `inset`, `:has()`, `:is()/:where()`, container queries, nesting, `color-mix()`, `@layer`, `text-wrap: balance`, `overflow: clip`, flex `gap` | Widely available. | Use freely. |
| `scrollbar-gutter`, `light-dark()`, `field-sizing`, `text-wrap: pretty` in Firefox, anchor positioning, `@starting-style`, `popover` w/ anchors, `:user-valid` | Newly available or partial as of 2026-09. | `@supports` + fallback, or skip. `scrollbar-gutter: stable` may go bare — absence only costs a layout shift. `light-dark()` is not a substitute for the two token blocks in `fused-render-theming`. |

### JS

- `Date.parse("2025-01-02 10:00")` → NaN in Safari (space separator). Always ISO `T`, or build from parts.
- `showOpenFilePicker`/`showSaveFilePicker`, `navigator.userAgentData` → Chrome-only; `scheduler.postTask` not in Safari. Files come via `fused.readFile`/`writeFile` anyway.
- `structuredClone`, `Array.prototype.at`, `Object.hasOwn`, `??=`, top-level `await`, regex lookbehind → fine (Safari ≥ 16.4). Don't go past ES2022 syntax; `using`, decorators, `Iterator.prototype.*` are not Baseline widely.
- `element.scrollIntoViewIfNeeded` is WebKit/Chrome only — use `scrollIntoView({block:"nearest"})`.
- `requestIdleCallback` missing in Safari → `window.requestIdleCallback ?? (cb => setTimeout(cb, 1))`.
- Canvas: `ctx.roundRect` fine; `OffscreenCanvas` OK from Safari 16.4; `MediaRecorder` gives webm in Chrome/Firefox and mp4 in Safari — go through `fused.capture` instead.
- `wheel` event `deltaMode` differs (Firefox lines vs pixels). Normalise: `deltaMode === 1 ? delta * 16 : delta`.

## Theming interplay

`color-scheme` on `:root` (already in the starter) makes native controls, scrollbars and form chrome follow dark mode in every engine. `appearance: none` throws that away for that control — restyle it fully from tokens or leave it native. Never half.

## Verify before "done"

Same `/explorer/embed/<path>` URL in at least two engines. On macOS both are one command away:

```
open -a Safari  "http://127.0.0.1:1777/explorer/embed/…"
open -a Firefox "http://127.0.0.1:1777/explorer/embed/…"
```

Look for: clipped text in flex rows, unstyled `<select>` arrow, scrollbar styling that vanished, blur panel with unreadable text, controls inheriting a different font. Layout differs by more than a pixel or two → an engine-specific rule is missing, not "Safari being Safari".

Rest of page authoring → `fused-render-authoring`. Colour/token rules → `fused-render-theming`.
