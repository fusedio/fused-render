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

  it("stops piercing at a modal or a popover that opts in with data-hint-stop", () => {
    // The Tasks tab editor's + menu hangs over the list; without the stop,
    // hovering "PR" in it captioned the task row underneath (Akshil, 2026-10-16).
    expect(SRC).toContain('if (node.closest(".modal-overlay, [data-hint-stop]")) return null;');
    const scheduled = readFileSync(join(import.meta.dir, "../../shell/Scheduled.tsx"), "utf8");
    expect(scheduled).toMatch(/className="schedule-view-add-menu"[^>]*data-hint-stop/);
  });

  it("re-asks on a move over a pierce-through surface, because one element can span two answers", () => {
    // The row's stretched link is one continuous element: moving from the title
    // onto the empty space beside it crosses no event boundary, so a handler
    // that only reacted to `pointerover` would leave the caption up over a
    // region that has none. Only such a surface (`data-hint-through`) pays for
    // a hit test per move; everywhere else over/out are enough (2026-10-09).
    const move = SRC.slice(SRC.indexOf("function onMove"));
    const body = move.slice(0, move.indexOf("\n}"));
    expect(body).toContain("hintAt(e.clientX, e.clientY, e.target)");
    expect(body).toContain("if (!el) {");
    expect(body).toContain("throughAt(e.target)");
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
    // Stale content in a hidden panel is content that flashes on the next
    // show, before that show's own text lands. `replaceChildren` rather than
    // `textContent = ""`, because a columnised caption leaves an element
    // behind, not a string.
    const hide = SRC.slice(SRC.indexOf("export function hideHint"));
    expect(hide.slice(0, hide.indexOf("\n}"))).toContain("panel.replaceChildren()");
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

describe("the task row's two-line form", () => {
  const SRC = readFileSync(new URL("./hints.ts", import.meta.url), "utf8");
  it("opts in on the ATTRIBUTE, and draws whichever lines it was given", () => {
    // Title + reply: the List row. Reply alone: the Board card (Akshil,
    // 2026-09-20: "let's not show the title, only the last reply"). Neither:
    // not this form — the plain `data-hint` draws instead.
    expect(SRC).toContain('if (!el.hasAttribute("data-hint-title")) return false;');
    expect(SRC).toContain("if (!title && !reply) return false;");
    expect(SRC).toMatch(/if \(title\) \{[\s\S]*?hint-task-title/);
    expect(SRC).toMatch(/if \(reply\) \{[\s\S]*?hint-task-reply/);
  });
  it("the reply-only form starts at the cursor and reads narrow — the band is the row's", () => {
    // Akshil, 2026-09-20: the 80%-band clamp is for the two-line row caption;
    // the Board card's reply-only caption is placed like every other hint.
    expect(SRC).toContain('p.classList.toggle("is-reply", !title);');
    expect(SRC).toContain('if (p.classList.contains("is-task") && !p.classList.contains("is-reply")) {');
    const CSS = readFileSync(new URL("../../styles/tasks.css", import.meta.url), "utf8");
    expect(CSS).toMatch(/\.hint-panel\.is-task\.is-reply \{\s*max-width: min\(46ch, 60vw\);/);
  });
  it("the Board card asks for the reply only, and has no caption without one", () => {
    const VIEWS = readFileSync(new URL("../../shell/ScheduleTaskViews.tsx", import.meta.url), "utf8");
    expect(VIEWS).toContain('data-hint={lockedDraft ? "Finish the draft to run it." : task.last_reply || ""}');
    expect(VIEWS).toContain('data-hint-title=""');
    expect(VIEWS).toContain('data-hint-reply={lockedDraft ? "" : task.last_reply || ""}');
  });
});

describe("the task's row in one line (a rail ring's hover, 2026-11-16)", () => {
  const SRC = readFileSync(new URL("./hints.ts", import.meta.url), "utf8");
  const TASKS_CSS = readFileSync(new URL("../../styles/tasks.css", import.meta.url), "utf8");
  const fn = (name: string) => {
    const at = SRC.indexOf(`function ${name}(`);
    expect(at).toBeGreaterThan(-1);
    return SRC.slice(at, SRC.indexOf("\n}\n", at));
  };
  const rule = (sel: string) => {
    const at = TASKS_CSS.indexOf(`${sel} {`);
    expect(at).toBeGreaterThan(-1);
    return TASKS_CSS.slice(at, TASKS_CSS.indexOf("}", at));
  };

  it("opts in on its own attribute, beside the task form's — and needs a title", () => {
    // `data-hint-row` on a host that already carries `data-hint-title` /
    // `data-hint-reply`: a host without it keeps the two-line caption, and a
    // row with no title is no row (the plain hint draws).
    const row = fn("renderRowHint");
    expect(row).toContain('if (!el.hasAttribute("data-hint-row")) return false;');
    expect(row).toContain("if (!title) return false;");
  });

  it("the row form is asked FIRST and the other two forms stand down for it", () => {
    const show = fn("show");
    expect(show).toContain("const row = renderRowHint(p, el);");
    expect(show).toContain('p.classList.toggle("is-row", row);');
    expect(show).toContain("const task = !row && renderTaskHint(p, el);");
    expect(show).toContain('if (!row && !task) renderHint(p, el.getAttribute("data-hint") || "");');
    // A host that is NOT a row must not inherit the last row's width.
    expect(show).toContain('if (!row) p.style.width = "";');
    expect(show.indexOf("renderRowHint(p, el)")).toBeLessThan(show.indexOf("renderTaskHint(p, el)"));
  });

  it("draws the row's own order: mark, number, title, reply, count, time", () => {
    const row = fn("renderRowHint");
    const order = ['"hint-row-mark"', '"hint-row-id"', '"hint-row-title"', '"hint-row-reply"', '"hint-row-count"', '"hint-row-time"'].map((c) =>
      row.indexOf(c),
    );
    expect(order.every((i) => i > -1)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    // The mark is the host's own drawing, cloned: the ring being pointed at.
    expect(row).toContain("for (const kid of Array.from(el.children)) mark.append(kid.cloneNode(true));");
    // A fact that was not given leaves no seat; the reply's seat always
    // stands — it is the row's slack, so the facts stay at the right end.
    expect(row).toContain('if (id) row.append(span("hint-row-id", id));');
    expect(row).toContain("if (count) {");
    expect(row).toContain('if (when) row.append(span("hint-row-time", when));');
    expect(row).toContain('row.append(span("hint-row-reply", (el.getAttribute("data-hint-reply") || "").trim()));');
  });

  it("every word a task supplies goes in as TEXT, never as markup", () => {
    // A title or a reply is whatever a chat said; only the count's glyph —
    // a constant of this file — is written as HTML.
    const row = fn("renderRowHint");
    expect(row).toContain("s.textContent = text;");
    expect(row).not.toContain("innerHTML");
    expect((row.match(/insertAdjacentHTML/g) ?? []).length).toBe(1);
    expect(row).toContain('c.insertAdjacentHTML("beforeend", ROW_COUNT_ICON);');
    expect(SRC).toMatch(/const ROW_COUNT_ICON =\n\s*'<svg /);
  });

  it("is as wide as the window the ring opens — clamped to the room beside the column, never under 120px", () => {
    const row = fn("renderRowHint");
    // The window's width NOW when it is on screen (a drag included), the
    // stamped one otherwise.
    expect(row).toContain('const live = liveWidth(el.getAttribute("data-hint-width-of"));');
    expect(row).toContain('const want = live || Number(el.getAttribute("data-hint-width")) || 0;');
    expect(fn("liveWidth")).toContain("if (!selector) return 0;");
    expect(fn("liveWidth")).toContain("return Math.round(box.getBoundingClientRect().width);");
    expect(row).toContain("const room = rowEdge(el) - 8 - EDGE;");
    expect(row).toContain('p.style.width = want > 0 ? `${Math.max(120, Math.min(want, room))}px` : "";');
    // The clamp, worked: a 420px window beside a column at x=1452 keeps its
    // 420; the same window in a 300px-wide viewport gets the room; a column
    // hard against the left edge still gets the floor.
    const EDGE = Number(SRC.match(/const EDGE = (\d+);/)?.[1]);
    expect(EDGE).toBeGreaterThan(0);
    const width = (want: number, edge: number) => Math.max(120, Math.min(want, edge - 8 - EDGE));
    expect(width(420, 1452)).toBe(420);
    expect(width(420, 252)).toBe(252 - 8 - EDGE);
    expect(width(420, 60)).toBe(120);
  });

  it("prefers the window's LIVE width over the stamped one, and falls back when the window is not up", () => {
    // The host stamps `data-hint-width` at its last render; a card the reader
    // has just dragged is wider than that (2026-11-16: "we click a task ring,
    // then resize and hover over another — the hover row's size doesn't
    // change"). `data-hint-width-of` names the window; its box wins while it
    // is on screen.
    const row = fn("renderRowHint");
    expect(row).toContain('const live = liveWidth(el.getAttribute("data-hint-width-of"));');
    expect(row.indexOf("const live = liveWidth(")).toBeLessThan(row.indexOf("const want = live ||"));
    // The live width goes through the same clamp as the stamped one.
    expect(row.indexOf("const want = live ||")).toBeLessThan(row.indexOf("Math.max(120, Math.min(want, room))"));
    // Every way of having no live window answers 0, which `||` passes over:
    // no attribute, a selector the engine refuses, nothing matching it.
    const live = fn("liveWidth");
    expect(live).toContain("if (!selector) return 0;");
    expect(live).toMatch(/try \{\s*box = document\.querySelector\(selector\);\s*\} catch \{\s*return 0;\s*\}/);
    expect(live).toContain('if (!box || typeof box.getBoundingClientRect !== "function") return 0;');
    // The preference, worked: a 515px window up beats a stamped 360; no
    // window up, the stamped 360 stands; neither, no inline width at all.
    const want = (liveW: number, stamped: string | null) => liveW || Number(stamped) || 0;
    expect(want(515, "360")).toBe(515);
    expect(want(0, "360")).toBe(360);
    expect(want(0, null)).toBe(0);
    expect(want(0, "")).toBe(0);
  });

  it("stands against the column's edge, where its windows stand — not against the one ring", () => {
    expect(fn("rowEdge")).toContain('const col = el.closest("[data-hint-edge]") ?? el;');
    expect(fn("rowEdge")).toContain("return col.getBoundingClientRect().left;");
    const place = fn("placeBeside");
    expect(place).toContain('const edge = p.classList.contains("is-row") ? rowEdge(el) : r.left;');
    expect(place).toContain("const left = Math.max(EDGE, edge - 8 - w);");
  });

  it("a row raised by a scroll under a resting pointer is marked passing; a pointer somewhere new, or the keyboard, clears it", () => {
    // The scroll that hides the hint also arms the mark…
    expect(SRC).toContain('window.addEventListener("scroll", onScrolled, true);');
    const scrolled = SRC.slice(SRC.indexOf("function onScrolled"));
    expect(scrolled.slice(0, scrolled.indexOf("\n}"))).toContain("passing = true;");
    // …a row shown while it is armed wears it, and only a row…
    const show = SRC.slice(SRC.indexOf("function show("));
    expect(show.slice(0, show.indexOf("\n}"))).toContain('p.classList.toggle("is-passing", row && passing);');
    // …and it goes when a pointer event reports a NEW spot: the events a
    // scroll causes under a pointer at rest carry the spot it already had.
    const pointed = SRC.slice(SRC.indexOf("function pointed("));
    const body = pointed.slice(0, pointed.indexOf("\n}"));
    expect(body).toContain("if (known && e.clientX === pointerX && e.clientY === pointerY) return;");
    expect(body.indexOf("return;")).toBeLessThan(body.indexOf("passing = false;"));
    // The FIRST event seen is a baseline, not a move (CodeRabbit on #2320): a
    // pointer resting on the rail at load has no earlier spot to compare
    // with, so its first scroll-made event must record the spot and leave
    // the mark armed.
    expect(SRC).toContain("let pointerX = Number.NaN;");
    expect(body).toContain("const known = !Number.isNaN(pointerX) && !Number.isNaN(pointerY);");
    const record = body.indexOf("pointerY = e.clientY;");
    const baseline = body.indexOf("if (!known) return;");
    expect(record).toBeGreaterThan(-1);
    expect(baseline).toBeGreaterThan(record);
    expect(baseline).toBeLessThan(body.indexOf("passing = false;"));
    expect(body).toContain('panel.classList.remove("is-passing");');
    for (const fn of ["function onOver(", "function onMove("]) {
      const at = SRC.indexOf(fn);
      expect(SRC.slice(at, at + 80)).toContain("pointed(e);");
    }
    // The keyboard's hint is the reader's own.
    const focus = SRC.slice(SRC.indexOf("function onFocus("));
    const fbody = focus.slice(0, focus.indexOf("\n}"));
    expect(fbody.indexOf("passing = false;")).toBeGreaterThan(-1);
    expect(fbody.indexOf("passing = false;")).toBeLessThan(fbody.indexOf("show(el"));
    // A hide takes the mark down with the row.
    const hide = SRC.slice(SRC.indexOf("export function hideHint"));
    expect(hide.slice(0, hide.indexOf("\n}"))).toContain('panel.classList.remove("is-passing");');
  });

  it("a hide takes the row form and its width with it", () => {
    // The panel is shared: a plain hint shown next must not come up a 420px
    // row-shaped box.
    const hide = SRC.slice(SRC.indexOf("export function hideHint"));
    const body = hide.slice(0, hide.indexOf("\n}"));
    expect(body).toContain("panel.replaceChildren();");
    expect(body).toContain('panel.classList.remove("is-row");');
    expect(body).toContain('panel.style.width = "";');
  });

  it("the stylesheet: one line, only the title and the reply give", () => {
    const panel = rule(".hint-panel.is-row");
    expect(panel).toContain("box-sizing: border-box;");
    // The app's own popover dress, not the window's ground (2026-11-17:
    // "check other places in our platform … just make sure it is visible
    // enough, doesn't mix in with the bg"): a hovered Tasks row's ground, the
    // chat popovers' strong edge and shadow. No dimming of the window behind.
    expect(panel).toContain("background: var(--row-bg-hover);");
    expect(panel).not.toContain("background: var(--bg);");
    expect(panel).toContain("border-color: var(--nt-card-border-strong);");
    expect(panel).toContain("box-shadow: 0 6px 24px var(--shadow-md);");
    expect(TASKS_CSS).not.toContain("is-spot");
    // While the row is up the rail's windows stand under a light scrim
    // (2026-11-17: "let's do scrim 30"): 55% of the app's modal scrim token —
    // 30% black dark, 22% light — over the card, taking no pointer.
    expect(TASKS_CSS).toContain(
      '.claude-rail-peek > .claude-rail-peek-card::after,\n.modal-popover .modal-dialog.new-task-dialog::after {\n  content: "";\n  position: absolute;\n  inset: 0;\n  z-index: 50;\n  border-radius: inherit;\n  background: color-mix(in srgb, var(--scrim) 55%, transparent);\n  opacity: 0;\n  pointer-events: none;\n  transition: opacity var(--dur-med) var(--ease-out);\n}',
    );
    // It fades in and out ("a transition fade for this scrim"): only its
    // opacity moves, and the card is never made a positioned box for it — its
    // resize grips are earlier siblings and would end up under it.
    // …and the + New task popover with them (2026-11-17: "scrim should apply
    // for + new button, bots and tasks as well"): window A in the Modal
    // chassis, named beside the cards. Its `::before` is the accent line.
    expect(TASKS_CSS).toContain(
      "body:has(.hint-panel.is-row.is-on:not(.is-passing)) .claude-rail-peek > .claude-rail-peek-card::after,\nbody:has(.hint-panel.is-row.is-on:not(.is-passing)) .modal-popover .modal-dialog.new-task-dialog::after {\n  opacity: 1;\n}",
    );
    // No scrim for a row the SCROLL brought (2026-11-17: "when I scroll there
    // is a little flash of the overlay that comes in and goes out"): no rule
    // dims on `.is-row.is-on` alone any more.
    expect(TASKS_CSS).not.toContain("body:has(.hint-panel.is-row.is-on) ");
    const newTask = readFileSync(join(import.meta.dir, "../../styles/new-task.css"), "utf8");
    expect(newTask).toContain(".new-task-dialog::before {");
    expect(newTask).not.toContain(".new-task-dialog::after");
    expect(newTask).toMatch(/\n\.new-task-dialog \{[^}]*overflow: hidden;/);
    expect(TASKS_CSS).not.toContain(".claude-rail-peek-card {\n  position: relative;");
    const rail = readFileSync(join(import.meta.dir, "../../styles/claude-rail.css"), "utf8");
    expect(rail).toMatch(/\n\.claude-rail-peek \{[^}]*position: fixed;/);
    const tokens = readFileSync(join(import.meta.dir, "../../styles/tokens.css"), "utf8");
    expect(tokens).toContain("--scrim: rgba(0, 0, 0, 0.55);");
    expect(tokens).toContain("--scrim: rgba(0, 0, 0, 0.4);");
    // The same recipe the chat's popovers wear.
    const composer = readFileSync(join(import.meta.dir, "../../apps/claude/styles/composer.css"), "utf8");
    expect(composer).toMatch(/\.c-kebabpop \{[^}]*border: 1px solid var\(--c-border-strong\);[^}]*box-shadow: 0 6px 24px var\(--c-shadow\);/);
    expect(panel).toContain("max-width: none;");
    expect(panel).toContain("white-space: nowrap;");
    expect(rule(".hint-row")).toContain("display: flex;");
    expect(rule(".hint-row-title")).toMatch(/flex: 0 1 auto;\s*min-width: 0;\s*overflow: hidden;\s*text-overflow: ellipsis;/);
    expect(rule(".hint-row-reply")).toMatch(/flex: 1 1 0;\s*min-width: 0;\s*overflow: hidden;\s*text-overflow: ellipsis;/);
    for (const fixed of [".hint-row-mark", ".hint-row-id"]) expect(rule(fixed)).toContain("flex: 0 0 auto;");
    expect(TASKS_CSS).toMatch(/\.hint-row-count,\n\.hint-row-time \{\s*flex: 0 0 auto;/);
  });
});
