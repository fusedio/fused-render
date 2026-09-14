// THE FLAG'S CONTRACT: with `task_peek_enabled` off, the Tasks page is the page
// it was before any of this existed.
//
// That is a claim about ABSENCE, and absence is the one thing a feature's own
// tests never check — every other suite here exercises the peek with the peek
// on. What follows is the other side: the store refuses to open, and no marking
// of the feature's reaches the markup or the stylesheet's live selectors.
//
// Two kinds of check, deliberately:
//
//   * BEHAVIOURAL, for the store — `openPeek` answers false, which is what
//     sends every one of the four views back to `navigateUrl` (performOpen);
//   * SOURCE, for the markup — every attribute and class the feature adds is
//     written behind a `peekOn` guard, and every stylesheet rule that changes
//     the toolbar's layout is scoped to the `data-fit` that only exists while
//     the feature is on. A render test would need four mounted views and the
//     whole task feed; the guards are one line each and reading them is what a
//     reviewer would do anyway.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { beforeEach, describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SHELL = new URL(".", import.meta.url).pathname;
const read = (rel: string) => readFileSync(join(SHELL, rel), "utf8");
const VIEWS = read("ScheduleTaskViews.tsx");
const CARDS = read("TaskCards.tsx");
const CALENDAR = read("ScheduleCalendar.tsx");
const PAGE = read("Scheduled.tsx");
const APP = read("App.tsx");
const FIT = read("row-fit.ts");
const FLAG = read("task-peek-flag.ts");
const SCHEDULE_CSS = read("../styles/schedule.css");
const PEEK_CSS = read("../styles/task-peek.css");
const TASKS_CSS = read("../styles/tasks.css");
const CARDS_CSS = read("../styles/task-cards.css");
const SIDEBAR = read("../platform/ui/sidebar/SidebarFrame.tsx");

const store = await import("./task-peek-store");

beforeEach(() => {
  store.resetPeekStoreForTests();
});

describe("the store, with the feature off", () => {
  it("refuses to open — which is what sends every view back to navigating", () => {
    // `performOpen` reads exactly this answer: false means "do what you always
    // did", and what it always did is `navigateUrl(intent.href)`.
    expect(store.peekHostReady()).toBe(false);
    expect(store.openPeek("sess-1")).toBe(false);
    expect(store.getPeekState().key).toBeNull();
    expect(store.getPeekState().host).toBe(false);
  });

  it("ignores a `?peek=` in the URL entirely", () => {
    // A link someone shared, or a stale tab: with the feature off the param
    // names nothing and nothing adopts it.
    store.syncPeekFromUrl("?view=list&peek=sess-1");
    expect(store.getPeekState().key).toBeNull();
  });

  it("has nothing to settle, so it cannot rewrite the URL either", () => {
    store.settlePeek([{ key: "sess-1", task_id: "TASK-001" }]);
    expect(store.getPeekState().key).toBeNull();
  });

  it("…and the page turning the host ON is the only thing that changes that", () => {
    store.setPeekHost(true);
    expect(store.getPeekState().host).toBe(true);
    expect(store.openPeek("sess-1")).toBe(true);
  });
});

describe("the flag module", () => {
  it("is the native chat's idiom, down to the tri-state", () => {
    expect(FLAG).toContain("export function useTaskPeekFlag(): boolean | null");
    expect(FLAG).toContain("export function useTaskPeekEnabled(): boolean");
    expect(FLAG).toContain("export function publishTaskPeekEnabled");
    expect(FLAG).toContain("export function resetTaskPeekFlagForTests");
    // One shared read, a generation guard, and a bounded retry — the three
    // things that make two mounts cost one GET.
    expect(FLAG).toContain("let reading: Promise<void> | null = null;");
    expect(FLAG).toContain("let generation = 0;");
    expect(FLAG).toContain(".catch(() => getPrefs())");
  });

  it("reads the pref strictly: only a stored true is on", () => {
    // A server that predates the switch sends no `task_peek` at all, and that
    // must read as off — the pref's own default and today's behaviour.
    expect(FLAG).toContain("p.task_peek?.enabled === true");
  });

  it("settles a failed read on OFF rather than leaving it unknown", () => {
    // `null` sticking would be a page that never decides which behaviour it has.
    expect(FLAG).toContain("if (generation === departed) set(false);");
  });
});

describe("the markup adds nothing when the feature is off", () => {
  it("stamps the walk's attribute only behind the guard, in all four views", () => {
    for (const src of [VIEWS, CARDS, CALENDAR]) {
      // Every spread of the attribute is conditional, and none is left bare.
      expect(src).toContain("{...(peekOn ? { [PEEK_ITEM_ATTR]:");
      expect(src).not.toContain("{...{ [PEEK_ITEM_ATTR]:");
    }
    // The Board's is in the same file as the List's — two call sites, both
    // guarded, which is why the file is asserted to hold no unguarded spread.
    expect(VIEWS).not.toContain("{...{ [PEEK_ITEM_ATTR]: task.key }}");
  });

  it("draws no halo — the open key is read, and then spent only when the host is up", () => {
    for (const src of [VIEWS, CARDS, CALENDAR]) {
      // The FLAG IS SPENT ON THE VALUE, never on the call. `host` starts false
      // and flips true in a layout effect, so a hook behind that condition is a
      // hook that appears between two renders — which React throws on. Every
      // one of these reads the key unconditionally and gates what it does with
      // it (Bugbot, PR #1133).
      expect(src).toMatch(/=\s*usePeekedKey\(\);/);
      expect(src).not.toMatch(/\?\s*usePeekedKey\(\)/);
      expect(src).toMatch(/peekOn \? (peekedKey|openKey) : null/);
    }
  });

  it("draws no quick-open door", () => {
    expect(VIEWS).toContain("{peekOn && page && !openDraft && (");
    expect(VIEWS).toContain("{peekOn && page && !isDraftTask(task) && (");
    // …and the card's hover strip is not drawn FOR one either.
    expect(VIEWS).toContain("{((peekOn && page) || file || folderMissing");
  });

  it("writes no fit attributes on the list or the toolbar", () => {
    expect(VIEWS).toContain('{...(peekOn ? { "data-fit": fit } : {})}');
    expect(PAGE).toContain('{...(peekOn ? { "data-fit": toolbar.level } : {})}');
  });

  it("attaches no observers at all — the hooks return before they measure", () => {
    // Not merely "writes no attribute": an off page must not be paying for a
    // ResizeObserver and a MutationObserver per list either.
    expect(FIT).toContain("if (!enabled) return;");
    expect(FIT).toContain("if (!enabled || !el) return;");
    expect(FIT).toContain("return enabled ? level : 0;");
    expect(FIT).toContain("return [enabled ? level : 0, setEl];");
    // …and the toolbar's wrapper still answers the stable OFF verdict, so a
    // disabled page does not get a fresh object every render either.
    expect(FIT).toContain("(enabled ? { level } : OFF_FIT)");
  });

  it("mounts no panel: the host is the flag AND the page", () => {
    expect(PAGE).toContain("const peekable = !scope && peekOn;");
    expect(PAGE).toContain("if (!peekable) return page;");
    // Which also means no param-boundary claim from this page: the claim lives
    // in TaskPeek, and TaskPeek is inside the branch above.
    expect(read("TaskPeek.tsx")).toContain("useParamBoundary(nativeChat === false && !!src)");
  });

  it("keeps the nav epoch's ignore list empty", () => {
    // On main a traversal is judged on the whole URL; off, it still is.
    expect(APP).toContain("const NO_PAGE_PARAMS: readonly string[] = [];");
    expect(APP).toContain("useNavEpoch(taskPeekOn ? PAGE_PARAMS : NO_PAGE_PARAMS)");
  });

  it("leaves the sidebar's collapse exactly as it was", () => {
    expect(SIDEBAR).toContain("const collapsing = tuckOnCollapse && sidebarCollapsed");
    expect(SIDEBAR).toContain("tuckOnCollapse?: boolean;");
    expect(read("GlobalSidebar.tsx")).toContain("tuckOnCollapse={taskPeekOn}");
  });
});

describe("the peek header", () => {
  const HEAD = read("TaskPeek.tsx");

  it("is the PEEK's own row — no Claude wordmark, no model cluster", () => {
    // It wore `@apps/claude/ui/Topbar` for a day (design.md, Round 3), which
    // bought one row instead of two and cost the row its subject: a ✻ Claude
    // mark and a model/run status are facts about the TOOL.
    // The import is gone (the comment that records why it left is not).
    expect(HEAD).not.toContain('from "@apps/claude/ui/Topbar"');
    expect(HEAD).not.toContain("<ChatTopbar");
  });

  it("orders the row left to right, and that order is the tab order", () => {
    // Controls · who · acts. Read off the source in the order it renders,
    // because the tab order IS the DOM order and there is no tabindex anywhere
    // in this header to say otherwise.
    const head = HEAD.slice(
      HEAD.indexOf('<header className="task-side-peek-head"'),
      HEAD.indexOf("</header>"),
    );
    const at = (needle: string) => {
      const i = head.indexOf(needle);
      expect(i).toBeGreaterThan(-1);
      return i;
    };
    const order = [
      'aria-label={layout.cover ? "Close" : "Hide the task panel"}',
      'aria-label="Previous task"',
      'aria-label="Next task"',
      "<StatusIcon",
      "task-side-peek-id",
      "task-side-peek-title",
      "task-side-peek-open",
      "task-side-peek-project",
      'aria-label="More actions"',
    ].map(at);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(head).not.toContain("tabIndex");
  });

  it("hides the panel with the app's shared panel glyph, and × only in cover", () => {
    // `PanelIcon side="right"` is the frame-with-one-half-filled the Explorer's
    // own companion column wears — this IS that column. In cover mode there is
    // no column beside anything, so the × comes back.
    expect(HEAD).toContain('{layout.cover ? ICON_CLOSE : <PanelIcon side="right" />}');
    expect(HEAD).toContain('import PanelIcon from "@platform/ui/PanelIcon"');
  });

  it("uses the page's ONE open-in-Explorer glyph", () => {
    // Same path as the row's door and the wall card's — one picture for one act
    // (design.md, Header + list state v2).
    expect(HEAD).toContain("<svg {...ICON}><path d={ICON_OPEN_FOLDER_PATH} /></svg>");
    expect(VIEWS).toContain("const ICON_OPEN_DOOR = icon(<path d={ICON_OPEN_FOLDER_PATH} />, 12);");
    expect(CARDS).toContain("<path d={ICON_OPEN_FOLDER_PATH} />");
    // …and the arrows it replaced are gone from the rows and cards.
    expect(VIEWS).not.toContain("M14 4h6v6M20 4l-7 7M10 20H4v-6M4 20l7-7");
    expect(HEAD).not.toContain("M14 4h6v6M20 4l-7 7M10 20H4v-6M4 20l7-7");
  });

  it("walks with CHEVRONS, not arrows", () => {
    expect(HEAD).toContain('<svg {...ICON}><polyline points="18 15 12 9 6 15" /></svg>');
    expect(HEAD).toContain('<svg {...ICON}><polyline points="6 9 12 15 18 9" /></svg>');
  });

  it("carries a VERTICAL kebab holding exactly the three stated acts", () => {
    // Vertical because it sits at the end of a row rather than in one.
    expect(HEAD).toContain('<circle cx="12" cy="5"');
    for (const label of ["Continue this task in terminal", "Archive task", "Delete task"]) {
      expect(HEAD).toContain(label);
    }
    expect(HEAD).toContain("Unarchive task");
    // The two that left: the first is a control of its own in the header now,
    // the second was a menu row nobody could find for an act the address bar
    // already does.
    expect(HEAD).not.toContain('label: "Open as page"');
    expect(HEAD).not.toContain('label: "Copy link"');
  });

  it("gives every icon a tooltip", () => {
    const head = HEAD.slice(
      HEAD.indexOf('<header className="task-side-peek-head"'),
      HEAD.indexOf("</header>"),
    );
    // One `data-hint` per icon control — the app's own tooltip contract. Five
    // controls: hide, prev, next, open, kebab.
    expect(head.match(/data-hint=/g)?.length).toBe(5);
  });

  it("stays ONE LINE at 220px by folding, in a stated order", () => {
    // The title ellipsises all the way down first; then the project name goes,
    // then the door — and the door reappears in the ⋮, because a hidden control
    // has to be somewhere (design.md, Header + list state v2).
    expect(FIT).toContain('export const PEEK_HEAD_DROPS = [\n  ".task-side-peek-project",\n  ".task-side-peek-open",\n]');
    expect(PEEK_CSS).toContain(
      '.task-side-peek-head[data-fit]:not([data-fit="0"]) .task-side-peek-project',
    );
    expect(PEEK_CSS).toContain('.task-side-peek-head[data-fit="2"] .task-side-peek-open');
    expect(HEAD).toContain("headFit >= PEEK_HEAD_DROPS.length");
    // Nothing else ever folds: the way out of a panel and the way to its
    // actions are what a narrow window must never take away.
    for (const kept of ["task-side-peek-title", "task-side-peek-id", "task-side-peek-kebab"]) {
      expect(PEEK_CSS).not.toContain(`[data-fit] .${kept} {\n  display: none`);
    }
  });

  it("advances rather than closing when the task is filed or deleted", () => {
    // Filing is a sweep. The order is read BEFORE the act, because the poll
    // that follows takes the row away (shell/task-peek-store.ts).
    expect(HEAD).toContain("const visible = order();");
    expect(HEAD).toContain("advancePast(from, visible);");
    // The delete path reads it at the KEBAB PRESS, not in the modal's `onDone`:
    // by then the confirmation has been on screen for as long as the reader
    // took to read it, and a poll in between would have moved the list.
    expect(HEAD).toContain("eraseOrder.current = order();");
    expect(HEAD).toContain("advancePast(task.key, eraseOrder.current);");
    expect(HEAD).toContain("if (next) openPeek(next);\n    else closePeek();");
  });
});

describe("the one selected style, and the flag that gates it", () => {
  // The restyle of 2026-09-14 (design.md, Header + list state v2) replaces a
  // grey `.is-selected` fill and three neutral hover washes with one accent
  // ring and one accent tint. `is-selected` is the LIST's own memory of where
  // the reader was and exists with the feature off, so a bare
  // `.tasks-row.is-selected` rule is not a peek selector at all — it would ship
  // the whole restyle to a reader who opted out.

  it("reaches nothing with the feature off: every rule names the host", () => {
    // `.tasks-peek-host` is rendered by Scheduled.tsx only when `peekable` —
    // the flag AND the unscoped /tasks route — so with the feature down the
    // wrapper does not exist and not one of these selectors can match.
    expect(PAGE).toContain('<div className="tasks-peek-host">');
    expect(PAGE).toContain("if (!peekable) return page;");
    const rules = PEEK_CSS.split("}")
      .map((chunk) => chunk.slice(chunk.lastIndexOf("*/") + 1).split("{")[0] ?? "")
      .filter((sel) => /is-peeked|is-selected|\.tasks-row:hover|card-head:hover|tv-card:hover/.test(sel));
    expect(rules.length).toBeGreaterThan(4);
    for (const sel of rules) {
      for (const one of sel.split(",")) {
        if (!one.trim()) continue;
        expect(one).toContain(".tasks-peek-host");
      }
    }
  });

  it("leaves main's grey selected row and hover EXACTLY where they were", () => {
    // What a flag-off reader still gets, word for word.
    expect(TASKS_CSS).toContain(".tasks-row:hover {\n  background: var(--row-bg-hover);\n}");
    expect(TASKS_CSS).toContain(
      ".tasks-row.is-selected,\n.tasks-row.is-selected:hover {\n  background: var(--row-bg-hover);\n}",
    );
    expect(TASKS_CSS).toContain(".tasks-row.is-inert:hover {\n  background: transparent;\n}");
    expect(CARDS_CSS).toContain(
      ".task-card-head:hover {\n  /* One more step of the same wash the token itself is made of (tokens.css:",
    );
    expect(SCHEDULE_CSS).toContain(
      "background: color-mix(in srgb, var(--fg) 6%, var(--tasks-card-bg));",
    );
  });

  it("is ONE look on all three surfaces once the flag is on", () => {
    // Same ring width, same tint, same radius rule (the element's own — a
    // `box-shadow` follows whatever `border-radius` the component already has).
    const ring = "box-shadow: inset 0 0 0 1.5px var(--peek-halo)";
    const tint = "background: color-mix(in srgb, var(--peek-halo) 8%, transparent)";
    expect(PEEK_CSS).toContain(ring);
    expect(PEEK_CSS).toContain(tint);
    for (const surface of [
      ".tasks-row.is-peeked",
      ".tasks-row.is-selected",
      ".schedule-tv-card.is-peeked",
      ".task-card.is-peeked",
    ]) {
      expect(PEEK_CSS).toContain(`.tasks-peek-host ${surface}`);
    }
    // Neither card may keep its 1px border under the ring, or the mark is 2.5px
    // on a card and 1.5px on a row.
    expect(PEEK_CSS).toContain("border-color: transparent;");
    // Hover is the same colour, lighter, on all three.
    expect(PEEK_CSS).toContain("color-mix(in srgb, var(--peek-halo) 4%, transparent)");
    expect(PEEK_CSS).toContain("color-mix(in srgb, var(--peek-halo) 4%, var(--tasks-card-bg))");
  });
});

describe("the middle pane's floor", () => {
  // SOURCE checks, for the same reason the rest of this file uses them: the
  // floor is one attribute and one custom property written by the page and read
  // by four selectors, and what can go wrong is the two halves drifting apart —
  // a switch nothing listens for, or a rule keyed on an attribute nobody
  // writes. Neither needs a mounted list to catch.
  it("writes the switch and the number onto the frame, and only there", () => {
    expect(PAGE).toContain('data-floored={peek.floored ? "1" : undefined}');
    expect(PAGE).toContain('"--tasks-floor": `${contentFloor}px`');
    // The number is a CONTENT width: the frame's ¾ baseline counts the page's
    // gutters, and the views live inside them.
    expect(PAGE).toContain("peek.floor - peekGutter()");
  });

  it("scrolls each view sideways at the floor instead of reflowing it", () => {
    for (const view of [".tasks-list", ".task-cards-scroll", ".schedule-cal"]) {
      // Each view is named twice: once as a scroller, once for the content
      // inside it that stops shrinking.
      expect(PEEK_CSS).toContain(`.tasks-frame[data-floored="1"] ${view} > *`);
      const scrollers = PEEK_CSS.slice(
        PEEK_CSS.indexOf('.tasks-frame[data-floored="1"] .tasks-list,'),
      ).slice(0, 220);
      expect(scrollers).toContain(`.tasks-frame[data-floored="1"] ${view}`);
    }
    expect(PEEK_CSS).toContain("overflow-x: auto;");
    expect(PEEK_CSS).toContain("min-width: var(--tasks-floor, 0px);");
  });

  it("leaves the toolbar out of it — the toolbar is exempt at every width", () => {
    // A toolbar that scrolled sideways would put New task somewhere you have to
    // go looking for it (design.md, Widths v2). It folds and hides instead.
    expect(PEEK_CSS).not.toContain('[data-floored="1"] .schedule-toolbar');
  });

  it("never lets the page itself scroll sideways", () => {
    // design-principles §0. The frame's host clips, and every scroller above is
    // a view inside it.
    const host = PEEK_CSS.slice(PEEK_CSS.indexOf(".tasks-peek-host {"));
    expect(host.slice(0, host.indexOf("}"))).toContain("overflow: hidden");
  });
});

describe("the stylesheet changes nothing when the feature is off", () => {
  // Every rule that alters how the toolbar LAYS OUT is scoped to `data-fit`,
  // which shell/row-fit.ts writes only while the feature is on. Unscoped, these
  // changed the page for readers who had opted out: the search floored at 72px
  // where it used to collapse to nothing, the row stopped shrinking, and the
  // filter group's minimum moved.
  for (const rule of [
    ".schedule-toolbar[data-fit] > .schedule-view-seg",
    ".schedule-toolbar[data-fit] {",
    ".schedule-toolbar[data-fit] .schedule-tv-filters {",
    ".schedule-toolbar[data-fit] .schedule-tv-search {",
  ]) {
    it(`scopes \`${rule}\` to the flag's attribute`, () => {
      expect(SCHEDULE_CSS).toContain(rule);
    });
  }

  it("scopes every HIDE rung to a `data-fit` value the flag alone writes", () => {
    // The ladder's second half (design.md, Widths v2) takes controls off the
    // row. Every one of those rules names an explicit `[data-fit="N"]`, so a
    // toolbar with no attribute at all — the flag-off page — matches none of
    // them and keeps all four views, both filters and its search.
    for (const rung of [
      ".schedule-tv-pop-wrap",
      ".schedule-tv-search",
      '.schedule-view-btn[data-view="calendar"]',
      '.schedule-view-btn[data-view="cards"]',
      '.schedule-view-btn[data-view="board"]',
    ]) {
      const at = SCHEDULE_CSS.indexOf(`${rung} {\n  display: none;`);
      if (at < 0) continue;
      const head = SCHEDULE_CSS.slice(Math.max(0, at - 400), at);
      expect(head).toContain('[data-fit="');
    }
  });

  it("leaves the base toolbar, filter group and search as they were", () => {
    const base = (selector: string) => {
      const at = SCHEDULE_CSS.indexOf(`\n${selector} {`);
      expect(at).toBeGreaterThan(-1);
      return SCHEDULE_CSS.slice(at, SCHEDULE_CSS.indexOf("}", at));
    };
    // No wrap, no flex pinning, no overflow clip on the bare selector.
    const toolbar = base(".schedule-toolbar");
    expect(toolbar).not.toContain("flex-wrap");
    expect(toolbar).not.toContain("overflow");
    expect(toolbar).not.toContain("flex:");
    // The filter group keeps the `min-width: 0` it has always had…
    expect(base(".schedule-tv-filters")).toContain("min-width: 0");
    // …and the search its plain 260px width with no floor.
    const search = base(".schedule-tv-search");
    expect(search).toContain("width: 260px");
    expect(search).toContain("min-width: 0");
    expect(search).not.toContain("--fit-natural");
  });

  it("folds no label without the attribute", () => {
    // Every fold rule names `data-fit`; a `.schedule-fit-lbl` in an off page is
    // a span with no rule pointing at it.
    // Per RULE, not per line: the selectors are multi-line, and the attribute
    // is on the first of them.
    const rules = SCHEDULE_CSS.split("}")
      .map((chunk) => chunk.slice(chunk.lastIndexOf("*/") + 1))
      .filter((chunk) => chunk.includes(".schedule-fit-lbl"));
    expect(rules.length).toBeGreaterThan(0);
    for (const rule of rules) {
      expect(rule).toContain("data-fit");
      // AND THE ATTRIBUTE, not just a negation of one of its values:
      // `:not([data-fit="0"])` on its own matches a toolbar that has no
      // attribute at all — the flag-off page — and folded its labels
      // (measured live, 2026-09-13).
      expect(rule).not.toMatch(/\.schedule-toolbar:not\(/);
    }
  });

  it("every `:not([data-fit=…])` in the tasks stylesheets is preceded by a `[data-fit]` presence guard", () => {
    // Same trap, other file: `.tasks-list:not([data-fit="0"]) .tasks-row-time`
    // hid every row's age for readers with the flag OFF (no attribute at all
    // matches the negation). Found in review, 2026-09-13.
    for (const rel of ["../styles/schedule.css", "../styles/tasks.css", "../styles/task-peek.css"]) {
      const css = read(rel).replace(/\/\*[\s\S]*?\*\//g, "");
      const bare = css.match(/[^\]]:not\(\[data-fit/g) ?? [];
      expect({ file: rel, bare }).toEqual({ file: rel, bare: [] });
    }
  });
});
