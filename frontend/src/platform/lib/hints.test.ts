// The instant tooltip's two hard parts, driven directly: WHICH element a point
// resolves to, and WHERE the panel lands.
//
// Both were shipped wrong once. The resolver has to see through an overlay,
// because the Tasks row's navigation is an `<a>` stretched over the whole row
// and the title underneath it is what carries the caption — `event.target`
// alone answers "the link" and the caption never appears. The placement has to
// flip near a viewport edge, because the version before this one was positioned
// against its own element and hung outside the scroller on the last cell of a
// row.
//
// Read out of the source rather than run in a DOM: bun's test runner has no
// layout, so `elementsFromPoint` and `offsetWidth` are both zero there — a test
// that "passed" against a fake would not have caught either bug. What is checked
// here is the shape of the decisions; the geometry was verified in a real
// browser (see the PR).
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SRC = readFileSync(join(import.meta.dir, "hints.ts"), "utf8");
const CSS = readFileSync(join(import.meta.dir, "../../styles/base.css"), "utf8");

describe("what a point resolves to", () => {
  it("pierces overlays with elementsFromPoint, not just event.target", () => {
    // The bug this exists for: a stretched row link is the topmost element over
    // its own row, so the title beneath it never sees a pointer. Lifting the
    // title above the link would take the row's click with it, so the piercing
    // belongs in the resolver.
    expect(SRC).toContain("document.elementsFromPoint(x, y)");
    expect(SRC).toContain('node.closest("[data-hint]")');
  });

  it("treats an EMPTY hint as an opt-out that stops the walk", () => {
    // This is the whole mechanism behind the Tasks row's dead band: the shield
    // around the folder chip carries `data-hint=""` and sits above the row, so a
    // near-miss resolves to "nothing" instead of walking up to a caption.
    // Returning null on empty — rather than continuing to the next element in
    // the stack — is what makes an opt-out placed ABOVE a caption win.
    expect(SRC).toContain('return (el.getAttribute("data-hint") || "").trim() ? el : null;');
  });

  it("re-asks on every move, because one element can span two answers", () => {
    // The row's stretched link is one continuous element: moving from the title
    // onto the empty space beside it crosses no event boundary, so a handler
    // that only reacted to `pointerover` would leave the caption up over a
    // region that has none.
    const move = SRC.slice(SRC.indexOf("function onMove"));
    const body = move.slice(0, move.indexOf("\n}"));
    expect(body).toContain("hintAt(e.clientX, e.clientY, e.target)");
    expect(body).toContain("if (!el) {");
  });

  it("listens in the CAPTURE phase", () => {
    // Several rows on this page call `stopPropagation` on their own pointer
    // events; a bubble-phase listener would be silenced for everything inside
    // them.
    for (const type of ["pointerover", "pointermove", "pointerout"]) {
      expect(SRC).toContain(`document.addEventListener("${type}", `);
    }
    expect(SRC).toMatch(/addEventListener\("pointerover", onOver, true\)/);
  });
});

describe("where the panel lands", () => {
  it("follows the pointer, offset clear of the cursor", () => {
    // Positioned at the CURSOR, which is the one placement that cannot caption
    // the wrong row — the failure of the CSS panel it replaces. Below and right,
    // so it never covers the thing being pointed at.
    expect(SRC).toContain("const OFFSET_X = 12");
    expect(SRC).toContain("const OFFSET_Y = 18");
    expect(SRC).toContain("p.style.left = `${Math.round(left)}px`");
  });

  it("flips at a viewport edge rather than clamping", () => {
    // Clamping would slide the panel under the cursor, covering the ink it is
    // explaining. Verified in a browser: at the row's time cell, 787px into an
    // 854px viewport, the panel drew to the LEFT of the cursor and inside the
    // viewport.
    expect(SRC).toContain("left = Math.max(EDGE, x - OFFSET_X - w)");
    expect(SRC).toContain("top = Math.max(EDGE, y - OFFSET_Y - h)");
  });

  it("is fixed, un-clippable and never takes the pointer", () => {
    const rule = CSS.slice(CSS.indexOf(".hint-panel {"));
    const body = rule.slice(0, rule.indexOf("}"));
    // `fixed` on a child of <body> is outside every `overflow` on the page —
    // the other half of the old panel's clipping bug.
    expect(body).toContain("position: fixed");
    // A panel 12px from the cursor is one the cursor can reach; taking the
    // pointer would hide it and flicker forever.
    expect(body).toContain("pointer-events: none");
    // Hidden by `display`, so a stale width cannot be measured before the next
    // show's text lands.
    expect(body).toContain("display: none");
    expect(CSS).toContain(".hint-panel.is-on {");
  });

  it("has no delay and no transition — that is the point", () => {
    // The whole reason this module exists: a native `title` waits four to five
    // seconds on the first hover of a session, and that delay is the browser's
    // and unreachable from CSS.
    const rule = CSS.slice(CSS.indexOf(".hint-panel {"));
    const body = rule.slice(0, rule.indexOf("}"));
    expect(body).not.toContain("transition");
    expect(SRC).not.toContain("setTimeout");
  });
});

describe("what it cleans up after", () => {
  it("hides on press, scroll and blur", () => {
    // A scroll invalidates the viewport coordinates the panel is pinned to; a
    // press means the reader has decided and, on a control that navigates,
    // would otherwise outlive the page.
    expect(SRC).toContain('addEventListener("pointerdown", hideHint, true)');
    expect(SRC).toContain('addEventListener("scroll", hideHint, true)');
    expect(SRC).toContain('addEventListener("blur", hideHint)');
  });

  it("empties the panel when it hides", () => {
    // A stale string in a hidden panel is a string that flashes on the next
    // show, before that show's own text lands.
    const hide = SRC.slice(SRC.indexOf("export function hideHint"));
    expect(hide.slice(0, hide.indexOf("\n}"))).toContain('panel.textContent = ""');
  });

  it("installs once", () => {
    expect(SRC).toContain("if (installed || typeof document === \"undefined\") return;");
    expect(SRC).toContain("installed = true;");
  });

  it("keeps itself out of the accessibility tree", () => {
    // Every element that opts in carries its own accessible name, so announcing
    // this panel too would say the same sentence twice.
    expect(SRC).toContain('panel.setAttribute("aria-hidden", "true")');
  });

  it("answers a keyboard focus as well as a pointer", () => {
    // A control whose only explanation is its hint is one a keyboard cannot
    // understand without this.
    expect(SRC).toContain('addEventListener("focusin", onFocus, true)');
    expect(SRC).toContain("el.getBoundingClientRect()");
  });
});
