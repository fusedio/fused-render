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
    expect(FIT).toContain("return [enabled ? fit : OFF_FIT, setEl];");
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
    '.schedule-toolbar[data-fit][data-wrap="1"] {',
  ]) {
    it(`scopes \`${rule}\` to the flag's attribute`, () => {
      expect(SCHEDULE_CSS).toContain(rule);
    });
  }

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
