// The three draggable seams in the shell — sidebar | middle pane | side column —
// and the two rules they now share:
//
//   1. WHAT LIGHTS UP IS THE HAIRLINE, NOT THE HIT BOX. Every seam is a 1px line
//      with a 5-6px invisible target around it, and each of the three used to
//      paint that whole target on hover: the line appeared to grow five- or
//      six-fold under the cursor (Akshil, 2026-09-10 — the seams read "a bit
//      thick").
//   2. THE SIDE COLUMN'S SEAM IS DRAWN FROM OUTSIDE THE COLUMN. It used to be a
//      pseudo-element inside it, and the native chat — `position: relative` boxes
//      with an opaque ground — painted straight over it, so the flag-on path had
//      no left border at all.
//
// Both are CSS-only facts about paint order, which no render test can see, so
// they are asserted against the stylesheets themselves. Under shell/ rather
// than apps/explorer/ because the sidebar seam is shell chrome and the file
// reads sidebar.css alongside the explorer's two sheets.
//
// The glow that rides the accent eases with it and is bounded at 3px: at 4px
// the lit band measured ~9px, wider than the wash this replaced.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const STYLES = join(import.meta.dir, "../styles");
const read = (name: string) => readFileSync(join(STYLES, name), "utf8");

const SIDEBAR_CSS = read("sidebar.css");
const EXPLORER_CSS = read("explorer.css");
const PREVIEW_CSS = read("preview.css");

/** A rule's declarations, comments stripped — the headstones in this repo name
 *  the properties they no longer set, and a substring search finds those. */
function block(css: string, selector: string): string {
  // Anchored at a line start, or `.listing-pane-slot::before {` would match the
  // tail of the `:has()` hover rule that names the same pseudo earlier in the file.
  const at = css.indexOf("\n" + selector + " {");
  expect(at).toBeGreaterThan(-1);
  const open = at + 1 + selector.length + 2;
  const body = css.slice(open, css.indexOf("}", open));
  return body.replace(/\/\*[\s\S]*?\*\//g, "");
}

describe("a draggable seam lights up its hairline, not its hit area", () => {
  it("leaves the sidebar handle's own 6px box transparent, always", () => {
    // The handle is the target for the pointer; #sidebar's border-right is the
    // line. The accent goes on a 1px pseudo parked over that border (2px in,
    // because SidebarFrame places the handle at `sidebarWidth - 3`).
    expect(block(SIDEBAR_CSS, ".sidebar-resize-handle:hover::after,\n.sidebar-resize-handle.resizing::after"))
      .toContain("background: var(--seam-hot)");
    const line = block(SIDEBAR_CSS, ".sidebar-resize-handle::after");
    expect(line).toContain("width: 1px");
    expect(line).toContain("left: 2px");
    expect(line).toContain("pointer-events: none");
    // The old rule, which painted the whole hit box: it must not come back.
    expect(SIDEBAR_CSS).not.toContain(".sidebar-resize-handle:hover,\n.sidebar-resize-handle.resizing {");
  });

  it("recolours the listing's one seam rather than stacking a second on it", () => {
    // The slot's pseudo already IS the seam (and already carries the z-index
    // that lifts it over the sticky table header); a second line from the
    // unpositioned divider would need its own context and rank. One line, two
    // colours.
    expect(EXPLORER_CSS).toContain(".listing-split:has(.listing-divider:hover) .listing-pane-slot::before");
    expect(EXPLORER_CSS).toContain(".listing-split:has(.listing-divider.dragging) .listing-pane-slot::before");
    expect(EXPLORER_CSS).not.toContain(".listing-divider:hover,\n.listing-divider.dragging {");
    expect(block(EXPLORER_CSS, ".listing-divider")).not.toContain("background: var(--seam-hot)");
  });

  it("keeps the side column's divider and reopen strip 1px on hover", () => {
    expect(block(PREVIEW_CSS, ".preview-side-divider:hover::before,\n.preview-side-divider.dragging::before"))
      .toContain("background: var(--seam-hot)");
    expect(block(PREVIEW_CSS, ".preview-side-reopen::after")).toContain("width: 1px");
    expect(block(PREVIEW_CSS, ".preview-side-reopen:hover::after")).toContain("background: var(--seam-hot)");
    // Neither hit box paints any more.
    expect(PREVIEW_CSS).not.toContain(".preview-side-divider:hover,\n.preview-side-divider.dragging {");
    expect(PREVIEW_CSS).not.toContain(".preview-side-reopen:hover::before {");
  });
});

describe("a seam's glow is bounded and eases with its colour", () => {
  const RULES: Array<[string, string]> = [
    [SIDEBAR_CSS, ".sidebar-resize-handle:hover::after,\n.sidebar-resize-handle.resizing::after"],
    [EXPLORER_CSS, ".listing-split:has(.listing-divider:hover) .listing-pane-slot::before,\n.listing-split:has(.listing-divider.dragging) .listing-pane-slot::before"],
    [PREVIEW_CSS, ".preview-side-divider:hover::before,\n.preview-side-divider.dragging::before"],
    [PREVIEW_CSS, ".preview-side-reopen:hover::after"],
  ];
  const LINES: Array<[string, string]> = [
    [SIDEBAR_CSS, ".sidebar-resize-handle::after"],
    [EXPLORER_CSS, ".listing-pane-slot::before"],
    [PREVIEW_CSS, ".preview-side-divider::before"],
    [PREVIEW_CSS, ".preview-side-reopen::after"],
  ];

  it("blurs no wider than 3px on every seam", () => {
    for (const [css, sel] of RULES) {
      expect(block(css, sel)).toContain("box-shadow: 0 0 3px color-mix(in srgb, var(--seam-hot) 35%, transparent)");
    }
  });

  it("transitions the glow together with the colour, on every seam", () => {
    for (const [css, sel] of LINES) {
      const body = block(css, sel);
      expect(body).toContain("background var(--dur-fast) var(--ease-out)");
      expect(body).toContain("box-shadow var(--dur-fast) var(--ease-out)");
    }
  });
});

describe("the seam's hot colour is a token, tuned per theme", () => {
  const TOKENS_CSS = read("tokens.css");
  it("is the accent whole on dark and a tint of it on light", () => {
    // Lime on a dark ground is a thread; the light accent is a dark olive and a
    // solid 1px of it read as a black rule (Akshil, 2026-09-10).
    const dark = TOKENS_CSS.slice(0, TOKENS_CSS.indexOf(':root[data-theme="light"]'));
    const light = TOKENS_CSS.slice(TOKENS_CSS.indexOf(':root[data-theme="light"]'));
    expect(dark).toContain("--seam-hot: var(--accent);");
    expect(light).toMatch(/--seam-hot: color-mix\(in srgb, var\(--accent\) \d+%, var\(--bg-alt\)\);/);
  });
  it("is what every seam lights up in — no seam reaches for --accent directly", () => {
    for (const css of [SIDEBAR_CSS, EXPLORER_CSS, PREVIEW_CSS]) {
      const rules = css.match(/\n\.[^{]*(resize-handle|listing-divider|preview-side-divider|preview-side-reopen)[^{]*\{[^}]*\}/g) ?? [];
      for (const rule of rules) {
        const body = rule.replace(/\/\*[\s\S]*?\*\//g, "");
        expect(body).not.toContain("background: var(--accent)");
      }
    }
  });
});

describe("the side column's left border survives what the column frames", () => {
  it("draws the seam from the divider, outside the column", () => {
    const line = block(PREVIEW_CSS, ".preview-side-divider::before");
    expect(line).toContain("background: var(--border)");
    expect(line).toContain("width: 1px");
    // 3px in: the divider's negative margins pull the column 2px back over it,
    // so the column's left edge is this box's left + 3.
    expect(line).toContain("left: 3px");
    expect(line).toContain("top: 0");
    expect(line).toContain("bottom: 0");
    // It must never eat the drag it belongs to.
    expect(line).toContain("pointer-events: none");
    // And the divider has to be a positioning context for it.
    expect(block(PREVIEW_CSS, ".preview-side-divider")).toContain("position: relative");
  });

  it("no longer draws it inside the column, where the chat painted over it", () => {
    // `.chat-root` / `.c-chat` are `position: relative` with an opaque
    // `--c-panel` ground and come later in the DOM than a z-index:auto pseudo,
    // so they covered the line — on the native path only, because the legacy
    // iframe is a static box and painted under it.
    expect(PREVIEW_CSS).not.toContain(".preview-side::before {");
  });

  it("outranks the column's own grounds AND its pinned card, without isolating the column", () => {
    // z-index 3: above every z-index:auto positioned box the column paints
    // (`.chat-root`, `.c-chat`, whose opaque ground was the bug) and above the
    // chat's sticky `.chat-tailpin.is-pinned` at 2, which at a tie would win on
    // tree order and eat the seam's bottom segment behind a permission ask.
    // Still under the chat's transient popovers (4, 5), which should pass over.
    expect(block(PREVIEW_CSS, ".preview-side-divider")).toContain("z-index: 3");
    // And `isolation: isolate` on the column is deliberately NOT the fix: it
    // would scope `#annpop.sidebar`'s z-index 40 to the column, and the column
    // (auto → 0) then loses to the middle pane's shown frame at z-index 1.
    expect(block(PREVIEW_CSS, ".preview-side")).not.toContain("isolation");
  });
});
