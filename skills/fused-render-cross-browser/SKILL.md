---
name: fused-render-cross-browser
description: Use when a fused-render view looks or behaves wrong in one browser only (Chrome, Firefox, Safari/WebKit), or to check support for a CSS/JS feature before using it — deep reference behind the authoring skill's mandatory Cross-browser section.
---

# Cross-browser views

A view is opened wherever the user's **default browser** is (Chrome/Edge, Firefox, Safari), inside **WKWebView** (macOS pinned popover, SPEC §25; the iOS shell under `ios/`), and by strangers who receive a hosted `.fused` export. **WebKit rules apply even when the author's browser is Chrome.** Test in one engine = shipped for one engine.

## Support target

- Use only features MDN marks **Baseline: Widely available**. Not in this file → check the MDN compat table before using it. Never assume from Chrome.
- **Newly available** (last ~30 months) → `@supports (…) {}` with a fallback that still works, or drop it.
- Chrome-only → don't. There is no "just for now".
- No transpiler, no autoprefixer, no build step (authoring contract). Prefixes are hand-written where this table says so.

## Paste-in baseline

Drop into every view's `<style>`, beneath the theme tokens (`fused-render-theming`):

```css
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
body { margin: 0; -webkit-tap-highlight-color: transparent; }
button, input, select, textarea { font: inherit; color: inherit; margin: 0; }
button { background: none; border: 0; padding: 0; cursor: pointer; }
::placeholder { color: var(--muted); opacity: 1; }
input, select, textarea, progress { accent-color: var(--accent, currentColor); }
img, svg, video, canvas { display: block; max-width: 100%; }
@media (hover: hover) { .btn:hover { /* hover only where a pointer exists */ } }
.fill { min-height: 100vh; min-height: 100dvh; }
```

Why each line: Safari buttons/inputs don't inherit font or colour; Firefox dims placeholders to 0.54 opacity; iOS Safari inflates text on landscape rotate; `:hover` sticks on touch; `100vh` overshoots behind the iOS toolbar (`dvh` fallback pattern: last rule wins where supported).

## Trap table

| Area | Trap | Fix |
|---|---|---|
| Scrollbars | `::-webkit-scrollbar` invisible in Firefox; standard props reached Safari last (Baseline only Dec 2025). Any engine that supports `scrollbar-color` **ignores** `::-webkit-scrollbar` once it is set (inherits!). | Write both pairs on the same element: `scrollbar-width: thin; scrollbar-color: var(--line) transparent;` plus `::-webkit-scrollbar { width: 8px } ::-webkit-scrollbar-thumb { background: var(--line) }`. Firefox, Chrome and new Safari take the standard pair; older Safari/WKWebView takes the pseudo-elements. Same colours in both so no engine looks different. |
| Selection | `user-select` still needs `-webkit-user-select` in Safari (not Baseline). | Always both, prefix first. |
| Line clamp | Needs the legacy trio. | `display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden;` |
| Blur panels | `backdrop-filter` unprefixed only Safari ≥ 18 (Baseline Sep 2024). | `-webkit-backdrop-filter: blur(8px); backdrop-filter: blur(8px);` and a solid-ish `background` fallback — blur is decoration, never the only thing making text readable. |
| `<select>` | Native arrow/padding differ per engine; Safari cannot style `<option>` at all. | `appearance: none; -webkit-appearance: none;` + own `background-image` arrow + `padding-right`. Options stay native everywhere — build a listbox if they must be styled. |
| `<input type=range>` | Thumb/track pseudo-elements are per engine. | Style both `::-webkit-slider-thumb` and `::-moz-range-thumb` (and `::-webkit-slider-runnable-track` / `::-moz-range-track`); start from `appearance: none`. |
| `<details>` | Marker differs; Safari keeps its own. | `summary { list-style: none } summary::-webkit-details-marker { display: none }` then draw your own. |
| Date/number/file/color inputs | Chrome is different by design (Firefox has no number spinner styling, Safari's date picker is a wheel on iOS). | Don't depend on native chrome. Style the box only; assume the picker looks native. `inputmode="decimal"` for numeric text when you need full control. |
| iOS zoom on focus | Input `font-size` < 16px → viewport zooms in on tap. | Inputs ≥ 16px on touch: `@media (pointer: coarse) { input, select, textarea { font-size: 16px } }`. |
| `position: fixed` | Broken under a transformed/filtered ancestor (all engines). | Portal fixed elements to `<body>`; never `transform` on a layout ancestor of a fixed child. |
| Sticky headers | `position: sticky` needs an `overflow` ancestor that's the actual scroller; `overflow: hidden` on an ancestor between sticky and scroller kills it (all engines). | Use `overflow: clip` for clipping, keep `sticky` directly in the scroller. |
| Smooth scroll | `scroll-behavior: smooth` fine; `scrollIntoView({behavior:"smooth"})` OK; scroll-driven animations (`animation-timeline: scroll()`) Chrome-only. | Gate scroll-driven animations behind `@supports (animation-timeline: scroll())` — plain, no fallback needed, it's decoration. |
| Fonts | System stack renders differently per OS; intermediate weights (500) fall back to 400 or 700 depending on the installed face. | Stack: `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif`; mono: `ui-monospace, Menlo, Consolas, monospace`. Use weights 400/600/700 only unless loading a font. |
| Flex/grid gaps | `gap` in flex — Baseline, fine. `min-height: 0`/`min-width: 0` on flex children: Safari overflows harder than Chrome. | Add `min-width: 0` to any flex child that holds text/`overflow`. |
| `aspect-ratio`, `inset`, `:has()`, `:is()/:where()`, container queries, nesting, `dvh`, `color-mix()`, `@layer`, `text-wrap: balance`, `overflow: clip` | Widely available. | Use freely. |
| `scrollbar-gutter` (Baseline Dec 2024), `light-dark()`, `field-sizing`, `text-wrap: pretty` in Firefox, anchor positioning, `@starting-style`, `popover` attr w/ anchors, `:user-valid` | Newly available or partial as of 2026-09. | `@supports` + fallback, or skip. `scrollbar-gutter: stable` may go bare — absence only costs a layout shift. `light-dark()` is not a substitute for the two token blocks in `fused-render-theming`. |

### JS

- `Date.parse("2025-01-02 10:00")` → NaN in Safari (space separator). Always ISO `T`, or build from parts.
- `showOpenFilePicker`/`showSaveFilePicker`, `navigator.userAgentData` → Chrome-only; `scheduler.postTask` not in Safari. Files come via `fused.readFile`/`writeFile` anyway.
- `structuredClone`, `Array.prototype.at`, `Object.hasOwn`, `??=`, top-level `await`, regex lookbehind → fine (Safari ≥ 16.4). Don't go past ES2022 syntax; `using`, decorators, `Iterator.prototype.*` are not Baseline widely.
- `ResizeObserver`/`IntersectionObserver` fine. `element.scrollIntoViewIfNeeded` is WebKit/Chrome only — use `scrollIntoView({block:"nearest"})`.
- `requestIdleCallback` missing in Safari → `window.requestIdleCallback ?? (cb => setTimeout(cb, 1))`.
- Canvas: `ctx.roundRect` fine; `OffscreenCanvas` OK from Safari 16.4; `canvas.captureStream` and `MediaRecorder` webm → Safari records mp4 — go through `fused.capture` instead.
- `wheel` event `deltaMode` differs (Firefox lines vs pixels). Normalise: `deltaMode === 1 ? delta * 16 : delta`.

## Theming interplay

`color-scheme` on `:root` (already in the starter) is what makes native controls, scrollbars and form chrome follow dark mode in every engine. `appearance: none` throws that away for that control — restyle it fully from tokens or leave it native. Never half.

## Verify before "done"

Same `/explorer/embed/<path>` URL in at least two engines. On macOS both are one command away:

```
open -a Safari  "http://127.0.0.1:1777/explorer/embed/…"
open -a Firefox "http://127.0.0.1:1777/explorer/embed/…"
```

Look for: dead `:hover` states on touch, clipped text in flex rows, unstyled `<select>` arrow, scrollbar styling that vanished, blur panel with unreadable text, controls inheriting a different font. Layout differs by more than a pixel or two → engine-specific rule is missing, not "Safari being Safari".

Rest of page authoring → `fused-render-authoring`. Colour/token rules → `fused-render-theming`.
