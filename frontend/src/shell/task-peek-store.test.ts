// The task side peek's rules, executed rather than grepped
// (.claude-design/task-side-peek/design.md). Four of them are arithmetic and
// one is a state machine, and every one is a rule a screenshot cannot check:
//
//   * the width clamp, including the case where its ceiling falls below its
//     floor (a narrow window) — a `NaN` or a negative here becomes an inline
//     `width: NaNpx` on the panel;
//   * the `?peek=` codec, which must carry the view and the filters through
//     untouched;
//   * the prev/next walk, which must NOT wrap;
//   * make-room: when the sidebar is collapsed on the reader's behalf, when it
//     is put back, and when the panel gives up on squeezing and covers instead;
//   * the store: who may open a peek at all (nobody, off the Tasks page).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { beforeEach, describe, expect, it } from "bun:test";

// bun has neither storage, and BOTH of this store's persisted facts are round
// trips through one — the dragged width, and the marker that says a collapsed
// sidebar is ours to hand back after a reload. Real (tiny) stores rather than
// spies, for the reason dismiss-store.test.ts gives: what is being checked is
// the round trip. `??=` so a suite that installed its own first keeps it —
// every file in a bun run shares one `globalThis`.
function tinyStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
  };
}
const g = globalThis as { localStorage?: unknown; sessionStorage?: unknown };
g.localStorage ??= tinyStorage();
g.sessionStorage ??= tinyStorage();

const sidebar = await import("@platform/lib/sidebarstate");

const {
  PEEK_AUTOCOLLAPSE_KEY,
  PEEK_MIN_WIDTH,
  PEEK_WIDTH_KEY,
  clampPeekWidth,
  defaultPeekWidth,
  peekWidthFor,
  closePeek,
  getPeekState,
  openPeek,
  peekHostReady,
  peekSearch,
  planRoom,
  readPeekParam,
  applyRoom,
  resetPeekStoreForTests,
  resetPeekWidth,
  frameClickCloses,
  resolvePeekKey,
  settlePeek,
  setPeekHost,
  setPeekWidth,
  stepPeekKey,
  syncPeekFromUrl,
} = await import("./task-peek-store");

beforeEach(() => {
  resetPeekStoreForTests();
  sidebar.setSidebarState({ width: 232, collapsed: false });
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {
    /* no storage in this runtime — every write is already best-effort */
  }
});

/** The window the sidebar must get out of the way for: 1000 − 232 = 768 of
 *  content, less a 564 peek → 204, well under the 430 floor. */
function narrowWindow(): void {
  (globalThis as { window: { innerWidth: number } }).window.innerWidth = 1000;
}

const named = (key: string, task_id: string) => ({ key, task_id });

// The sidebar's rail, the number `planRoom` settles a collapsed sidebar at.
const RAIL = 44;

describe("the width", () => {
  it("opens at half the content area when nothing has been dragged", () => {
    expect(defaultPeekWidth(2000)).toBe(1000);
    expect(peekWidthFor(null, 2000)).toBe(1000);
  });

  it("keeps a dragged width that fits", () => {
    expect(clampPeekWidth(700, 2000)).toBe(700);
    expect(peekWidthFor(700, 2000)).toBe(700);
  });

  it("floors at 564 — which is a floor and not the default", () => {
    expect(clampPeekWidth(400, 2000)).toBe(PEEK_MIN_WIDTH);
    expect(PEEK_MIN_WIDTH).toBe(564);
  });

  it("caps at two thirds of the content area", () => {
    expect(clampPeekWidth(1900, 2000)).toBe(1333);
  });

  it("RE-CLAMPS a persisted width onto a window it no longer fits", () => {
    // Dragged to 1200 on a wide display, reopened on a laptop: two thirds of
    // 1400 is 933, and the stored number must not be honoured past it.
    expect(peekWidthFor(1200, 1400)).toBe(933);
  });

  it("keeps the floor when the ceiling falls below it", () => {
    // A window too narrow to express the split at all: the panel is about to be
    // in cover mode, where this number is not what renders — so the honest
    // answer is the floor rather than something smaller than it.
    expect(clampPeekWidth(564, 700)).toBe(PEEK_MIN_WIDTH);
  });

  it("never answers NaN for a broken persisted value", () => {
    expect(clampPeekWidth(Number.NaN, 2000)).toBe(1000);
  });
});

describe("the ?peek= codec", () => {
  it("reads the key a deep link names", () => {
    expect(readPeekParam("?peek=sess-7&view=board")).toBe("sess-7");
  });

  it("reads null when there is none, and for an empty one", () => {
    expect(readPeekParam("?view=board")).toBeNull();
    expect(readPeekParam("?peek=")).toBeNull();
  });

  it("sets the key and leaves every other param where it was", () => {
    expect(peekSearch("?view=board&project=x", "sess-7")).toBe(
      "?view=board&project=x&peek=sess-7",
    );
  });

  it("replaces the key on a swap rather than appending a second one", () => {
    expect(peekSearch("?peek=a&view=board", "b")).toBe("?peek=b&view=board");
  });

  it("removes it on close, keeping the lens the reader set", () => {
    expect(peekSearch("?peek=a&view=board", null)).toBe("?view=board");
  });

  it("answers an empty string when nothing is left, so a bare path stays bare", () => {
    expect(peekSearch("?peek=a", null)).toBe("");
  });
});

describe("stepPeekKey", () => {
  const order = ["a", "b", "c"];

  it("walks the visible order", () => {
    expect(stepPeekKey(order, "a", 1)).toBe("b");
    expect(stepPeekKey(order, "c", -1)).toBe("b");
  });

  it("does NOT wrap at either end", () => {
    expect(stepPeekKey(order, "c", 1)).toBeNull();
    expect(stepPeekKey(order, "a", -1)).toBeNull();
  });

  it("has nowhere to go from a task the view is not showing", () => {
    // Filtered away while the peek held it: the arrows go quiet rather than
    // jumping to whatever happens to be first.
    expect(stepPeekKey(order, "z", 1)).toBeNull();
    expect(stepPeekKey(order, null, 1)).toBeNull();
  });
});

describe("planRoom", () => {
  const open = {
    open: true,
    chosenWidth: 564,
    sidebarWidth: 232,
    sidebarCollapsed: false,
    autoCollapsed: false,
  };

  it("leaves a wide window's sidebar alone", () => {
    // 1538 − 232 = 1306 of content, less a 653 peek (half) → 653 frame, well
    // over the 430 floor.
    const plan = planRoom({ ...open, chosenWidth: null, viewport: 1538 });
    expect(plan.sidebar).toBeNull();
    expect(plan.cover).toBe(false);
    expect(plan.peekWidth).toBe(653);
    expect(plan.frameAfter).toBe(653);
    expect(plan.autoCollapsed).toBe(false);
  });

  it("collapses the sidebar when the frame would drop under 430", () => {
    // 1000 − 232 = 768 of content, less a 564 peek → 204 frame → collapse. The
    // peek is then re-derived against the content area it actually gets:
    // 1000 − 44 = 956, and 564 still fits under the two-thirds cap.
    const plan = planRoom({ ...open, viewport: 1000 });
    expect(plan.sidebar).toBe(true);
    expect(plan.autoCollapsed).toBe(true);
    expect(plan.cover).toBe(false);
    expect(plan.peekWidth).toBe(564);
    expect(plan.frameAfter).toBe(1000 - RAIL - 564);
  });

  it("does NOT collapse at Notion's 430 where the old 600 would have", () => {
    // 1250 − 232 = 1018 of content, less 564 → 454: over 430, under 600. The
    // threshold is the whole of this test.
    const plan = planRoom({ ...open, viewport: 1250 });
    expect(plan.sidebar).toBeNull();
    expect(plan.frameAfter).toBe(454);
  });

  it("does not ask for a collapse the reader has already made", () => {
    const plan = planRoom({
      ...open,
      viewport: 1000,
      sidebarWidth: RAIL,
      sidebarCollapsed: true,
    });
    expect(plan.sidebar).toBeNull();
    // …and it does not claim that collapse as ours, so closing leaves it shut.
    expect(plan.autoCollapsed).toBe(false);
  });

  it("covers instead of squeezing once even the rail cannot buy 360px", () => {
    // 900 − 44 = 856 of content, less a 564 floor peek → 292 < 360.
    const plan = planRoom({ ...open, viewport: 900 });
    expect(plan.cover).toBe(true);
    // The frame is NOT shrunk in cover mode — it keeps the whole content area
    // and the panel is laid over it at that same width.
    expect(plan.frameAfter).toBe(900 - RAIL);
    expect(plan.peekWidth).toBe(900 - RAIL);
  });

  it("re-derives the peek's share against the area it is actually getting", () => {
    // Undragged at 1000: half of 768 is 384, under the 564 floor, so the peek
    // opens at 564 and the sidebar goes. Once it has, the content area is 956 —
    // and the peek must be half of THAT floor-clamped, not half of the area it
    // was about to leave.
    const plan = planRoom({ ...open, chosenWidth: null, viewport: 1000 });
    expect(plan.sidebar).toBe(true);
    expect(plan.peekWidth).toBe(564);
  });

  it("puts back only a sidebar it collapsed itself", () => {
    const ours = planRoom({
      open: false,
      viewport: 1138,
      chosenWidth: 564,
      sidebarWidth: RAIL,
      sidebarCollapsed: true,
      autoCollapsed: true,
    });
    expect(ours.sidebar).toBe(false);
    expect(ours.autoCollapsed).toBe(false);

    const theirs = planRoom({
      open: false,
      viewport: 1138,
      chosenWidth: 564,
      sidebarWidth: RAIL,
      sidebarCollapsed: true,
      autoCollapsed: false,
    });
    expect(theirs.sidebar).toBeNull();
  });

  it("keeps the flag latched across a resize while the peek is up", () => {
    // We collapsed it at 1000; the reader then widens the window. The sidebar
    // is still down and still OURS to put back — forgetting that here is how a
    // sidebar gets stranded collapsed.
    const plan = planRoom({
      ...open,
      viewport: 1600,
      sidebarWidth: RAIL,
      sidebarCollapsed: true,
      autoCollapsed: true,
    });
    expect(plan.autoCollapsed).toBe(true);
  });
});

describe("the store", () => {
  it("declines to open anywhere the Tasks page is not hosting a peek", () => {
    expect(peekHostReady()).toBe(false);
    expect(openPeek("sess-1")).toBe(false);
    expect(getPeekState().key).toBeNull();
  });

  it("opens, swaps and closes once the page is hosting", () => {
    setPeekHost(true);
    expect(openPeek("sess-1")).toBe(true);
    expect(getPeekState().key).toBe("sess-1");
    // A swap keeps the panel where it is — `instant` stays false, which is what
    // says "no re-slide" to the CSS.
    expect(openPeek("sess-2")).toBe(true);
    expect(getPeekState().key).toBe("sess-2");
    expect(getPeekState().instant).toBe(false);
    closePeek();
    expect(getPeekState().key).toBeNull();
  });

  it("treats re-opening the task already in the panel as a no-op", () => {
    setPeekHost(true);
    openPeek("sess-1");
    const before = getPeekState();
    expect(openPeek("sess-1")).toBe(true);
    expect(getPeekState()).toBe(before);
  });

  it("refuses an empty key", () => {
    setPeekHost(true);
    expect(openPeek("")).toBe(false);
  });

  it("closes when the page goes away — navigating away is a close trigger", () => {
    setPeekHost(true);
    openPeek("sess-1");
    setPeekHost(false);
    expect(getPeekState().key).toBeNull();
    // …and the peek does not spring back the next time the page mounts.
    setPeekHost(true);
    expect(getPeekState().key).toBeNull();
  });

  it("follows the URL on a traversal, without animating", () => {
    setPeekHost(true);
    syncPeekFromUrl("?peek=sess-9");
    expect(getPeekState().key).toBe("sess-9");
    expect(getPeekState().instant).toBe(true);
    syncPeekFromUrl("?view=board");
    expect(getPeekState().key).toBeNull();
  });

  it("ignores the URL off the Tasks page", () => {
    syncPeekFromUrl("?peek=sess-9");
    expect(getPeekState().key).toBeNull();
  });

  it("pushes ONE entry per open, per swap and per close", () => {
    const h = globalThis.history as { pushState: (...a: unknown[]) => void };
    const loc = globalThis.location as { pathname: string; search: string };
    const real = h.pushState;
    const wasSearch = loc.search;
    let pushes = 0;
    // A push that MOVES the address, because the store declines to push an
    // entry identical to the one it is standing on — a real guard, and one a
    // frozen `location` would hide.
    h.pushState = (_state: unknown, _title: unknown, url: unknown) => {
      pushes += 1;
      const q = String(url).indexOf("?");
      loc.search = q === -1 ? "" : String(url).slice(q);
    };
    try {
      setPeekHost(true);
      openPeek("sess-1");
      expect(pushes).toBe(1);
      openPeek("sess-2");
      expect(pushes).toBe(2);
      closePeek();
      expect(pushes).toBe(3);
      // …and NONE when the caller is about to navigate itself ("Open as page"):
      // a `/tasks` entry pushed a tick before the Explorer's would make one
      // Back land on the Tasks page with the panel already gone.
      openPeek("sess-3");
      pushes = 0;
      closePeek({ push: false });
      expect(pushes).toBe(0);
    } finally {
      h.pushState = real;
      loc.search = wasSearch;
    }
  });

  it("keeps the width the reader dragged, and forgets it on a reset", () => {
    expect(getPeekState().width).toBeNull(); // no choice yet — half the area
    setPeekWidth(700);
    expect(getPeekState().width).toBe(700);
    resetPeekWidth();
    expect(getPeekState().width).toBeNull();
  });
});

describe("resolvePeekKey", () => {
  const tasks = [named("sess-a", "TASK-001"), named("sess-b", "TASK-002")];

  it("takes a row key as it stands", () => {
    expect(resolvePeekKey("sess-b", tasks)).toBe("sess-b");
  });

  it("resolves a task NUMBER to that task's key, case-insensitively", () => {
    expect(resolvePeekKey("TASK-002", tasks)).toBe("sess-b");
    expect(resolvePeekKey("task-002", tasks)).toBe("sess-b");
  });

  it("refuses an AMBIGUOUS number — two projects can both hold a TASK-007", () => {
    const twins = [named("sess-x", "TASK-007"), named("sess-y", "TASK-007")];
    expect(resolvePeekKey("TASK-007", twins)).toBeNull();
  });

  it("answers null for a key that names nothing, and for nothing", () => {
    expect(resolvePeekKey("sess-gone", tasks)).toBeNull();
    expect(resolvePeekKey(null, tasks)).toBeNull();
    expect(resolvePeekKey("sess-a", [])).toBeNull();
  });
});

describe("settlePeek", () => {
  it("CLOSES a peek whose key names no task, rather than standing open and empty", () => {
    setPeekHost(true);
    syncPeekFromUrl("?peek=sess-gone");
    expect(getPeekState().key).toBe("sess-gone");
    settlePeek([named("sess-a", "TASK-001")]);
    expect(getPeekState().key).toBeNull();
  });

  it("rewrites a task NUMBER to that task's row key in place", () => {
    setPeekHost(true);
    syncPeekFromUrl("?peek=TASK-002");
    settlePeek([named("sess-a", "TASK-001"), named("sess-b", "TASK-002")]);
    expect(getPeekState().key).toBe("sess-b");
  });

  it("leaves a key that already names a task alone", () => {
    setPeekHost(true);
    syncPeekFromUrl("?peek=sess-a");
    const before = getPeekState();
    settlePeek([named("sess-a", "TASK-001")]);
    expect(getPeekState()).toBe(before);
  });

  it("says nothing while no peek is open", () => {
    setPeekHost(true);
    settlePeek([]);
    expect(getPeekState().key).toBeNull();
  });
});

describe("the sidebar the peek borrows", () => {
  it("collapses it WITHOUT writing the reader's preference", () => {
    narrowWindow();
    setPeekHost(true);
    openPeek("sess-1");
    expect(sidebar.getSidebarState().collapsed).toBe(true);
    expect(getPeekState().autoCollapsed).toBe(true);
    // The persisted preference is untouched — an auto-collapse is the layout
    // getting out of the way, not the reader shutting the panel.
    expect(sidebar.loadSidebarState().collapsed).toBe(false);
  });

  it("remembers across a reload that the collapse was ours", () => {
    narrowWindow();
    setPeekHost(true);
    openPeek("sess-1");
    let stored: string | null = null;
    try {
      stored = sessionStorage.getItem(PEEK_AUTOCOLLAPSE_KEY);
    } catch {
      /* no storage */
    }
    expect(stored).toBe("1");
  });

  it("hands it back when the page goes away, not just the flag", () => {
    narrowWindow();
    setPeekHost(true);
    openPeek("sess-1");
    expect(sidebar.getSidebarState().collapsed).toBe(true);
    // Navigating away from /tasks with the peek open: the sidebar must not be
    // stranded collapsed with nothing on screen that collapsed it.
    setPeekHost(false);
    expect(sidebar.getSidebarState().collapsed).toBe(false);
    expect(getPeekState().autoCollapsed).toBe(false);
  });

  it("hands it back on an ordinary close", () => {
    narrowWindow();
    setPeekHost(true);
    openPeek("sess-1");
    closePeek();
    expect(sidebar.getSidebarState().collapsed).toBe(false);
  });

  it("stops arguing once the reader opens it again themselves", () => {
    narrowWindow();
    setPeekHost(true);
    openPeek("sess-1");
    expect(getPeekState().autoCollapsed).toBe(true);
    // The reader presses the rail's chevron.
    sidebar.setSidebarState((s) => ({ ...s, collapsed: false }));
    expect(getPeekState().autoCollapsed).toBe(false);
    // …and the next make-room pass (a resize, a prev/next) leaves it open
    // rather than collapsing it straight back under them.
    applyRoom();
    expect(sidebar.getSidebarState().collapsed).toBe(false);
    // Nothing left to hand back, either.
    closePeek();
    expect(sidebar.getSidebarState().collapsed).toBe(false);
  });

  it("leaves a sidebar the reader collapsed themselves collapsed", () => {
    narrowWindow();
    sidebar.setSidebarState({ width: 232, collapsed: true });
    setPeekHost(true);
    openPeek("sess-1");
    expect(getPeekState().autoCollapsed).toBe(false);
    closePeek();
    expect(sidebar.getSidebarState().collapsed).toBe(true);
  });
});

describe("the persisted keys", () => {
  it("are the two names the store reads and writes", () => {
    // Spelled here so a rename has to come through this file: the width is a
    // preference the reader built by dragging, and the marker is what stops a
    // reload stranding their sidebar.
    expect(PEEK_WIDTH_KEY).toBe("tasks.peek.width");
    expect(PEEK_AUTOCOLLAPSE_KEY).toBe("tasks.peek.autocollapsed");
  });
});

// ---- clicking the frame ------------------------------------------------------
// A hand-rolled node with a REAL `closest`: the rule under test is a selector
// list, and a stub that answered yes/no would be testing the stub. Only the
// selector shapes the list actually uses are understood (tag, .class,
// [attr], [attr="value"]) — a fifth shape appearing in `PEEK_FRAME_KEEPS_OPEN`
// should make this throw rather than quietly pass.
interface FakeNode {
  tag: string;
  classes?: string[];
  attrs?: Record<string, string>;
  parent?: FakeNode;
}

function matchesOne(node: FakeNode, selector: string): boolean {
  const sel = selector.trim();
  if (sel.startsWith(".")) return (node.classes ?? []).includes(sel.slice(1));
  if (sel.startsWith("[")) {
    const body = sel.slice(1, -1);
    const eq = body.indexOf("=");
    if (eq === -1) return body in (node.attrs ?? {});
    const name = body.slice(0, eq);
    const want = body.slice(eq + 1).replace(/^["']|["']$/g, "");
    return (node.attrs ?? {})[name] === want;
  }
  if (/^[a-z]+$/.test(sel)) return node.tag === sel;
  throw new Error(`unhandled selector shape: ${sel}`);
}

function el(node: FakeNode): Element {
  const self = {
    closest(list: string): Element | null {
      const parts = list.split(",");
      let at: FakeNode | undefined = node;
      while (at) {
        for (const part of parts) if (matchesOne(at, part)) return self;
        at = at.parent;
      }
      return null;
    },
  } as unknown as Element;
  return self;
}

describe("frameClickCloses", () => {
  const page: FakeNode = { tag: "div", classes: ["schedule-page"] };

  it("closes on the page's own background", () => {
    expect(frameClickCloses(el({ tag: "div", parent: page }))).toBe(true);
  });

  it("does NOT close on a row, a card or a chip — those open their own task", () => {
    const row: FakeNode = {
      tag: "div",
      classes: ["tasks-row"],
      attrs: { "data-peek-key": "sess-1" },
      parent: page,
    };
    // …including a press on the ink INSIDE one.
    expect(frameClickCloses(el({ tag: "span", parent: row }))).toBe(false);
    expect(frameClickCloses(el(row))).toBe(false);
  });

  it("does NOT close on the toolbar, or on a control anywhere", () => {
    const toolbar: FakeNode = { tag: "div", classes: ["schedule-toolbar"], parent: page };
    expect(frameClickCloses(el({ tag: "span", parent: toolbar }))).toBe(false);
    expect(frameClickCloses(el({ tag: "button", parent: page }))).toBe(false);
    expect(frameClickCloses(el({ tag: "a", parent: page }))).toBe(false);
    expect(frameClickCloses(el({ tag: "input", parent: page }))).toBe(false);
    expect(frameClickCloses(el({ tag: "div", attrs: { role: "button" }, parent: page }))).toBe(
      false,
    );
  });

  it("does NOT close on a menu or a dialog portalled over the page", () => {
    const menu: FakeNode = { tag: "div", classes: ["context-menu"] };
    expect(frameClickCloses(el({ tag: "div", parent: menu }))).toBe(false);
    const dialog: FakeNode = { tag: "div", classes: ["modal-dialog"] };
    expect(frameClickCloses(el({ tag: "p", parent: dialog }))).toBe(false);
  });

  it("says nothing about a click with no target at all", () => {
    expect(frameClickCloses(null)).toBe(false);
  });
});
