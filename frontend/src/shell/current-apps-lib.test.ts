import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import type { TaskPulseTask } from "@platform/lib/api";
import {
  appDirOf,
  assignSequences,
  bySequence,
  moveSlug,
  reorderTo,
  appPageTab,
  appPageUrl,
  currentApps,
  isUnderDir,
  localAppsRoot,
  orderedSlugs,
  parseSavedOrder,
  REMEMBERED_LIMIT,
  slugFromAppPath,
  withAppPageTab,
  type AppOrder,
} from "./current-apps-lib";

const FUSED = "/Users/me/Fused";
const ROOT = "/Users/me/Fused/local/";

function task(
  key: string,
  project: string,
  status: TaskPulseTask["status"] = "done",
  last_active = 0,
): TaskPulseTask {
  return { key, project, status, last_active, unread: 0 };
}

describe("currentApps", () => {
  it("groups non-archived tasks by workspace app, newest activity first", () => {
    const apps = currentApps(
      [
        task("a1", ROOT + "alpha", "done", 10),
        task("a2", ROOT + "alpha/sub/dir", "in_progress", 50),
        task("b1", ROOT + "beta", "upcoming", 30),
        task("c1", ROOT + "gamma", "archived", 99),
        task("x1", "/Users/me/code/other", "in_progress", 100),
        task("r1", ROOT.slice(0, -1), "done", 100),
      ],
      FUSED,
    );
    expect(apps.map((a) => a.slug)).toEqual(["alpha", "beta"]);
    expect(apps[0]).toEqual({
      slug: "alpha",
      dir: ROOT + "alpha",
      taskKeys: ["a1", "a2"],
      lastActive: 50,
      running: true,
    });
    expect(apps[1].running).toBe(false);
  });

  it("renders every app, uncapped, newest first", () => {
    const tasks = Array.from({ length: 8 }, (_, i) =>
      task(`k${i}`, `${ROOT}app${i}`, "done", i),
    );
    const apps = currentApps(tasks, FUSED);
    expect(apps.map((a) => a.slug)).toEqual([
      "app7",
      "app6",
      "app5",
      "app4",
      "app3",
      "app2",
      "app1",
      "app0",
    ]);
  });

  it("is empty until the workspace root is known", () => {
    expect(currentApps([task("a", ROOT + "alpha")], "")).toEqual([]);
  });
});

// The apps a set of (slug, last_active) pairs adds up to, in recency order.
const of = (...specs: [string, number][]) =>
  currentApps(
    specs.map(([slug, at]) => task("k-" + slug, ROOT + slug, "done", at)),
    FUSED,
  );

// The displayed order (sequence per app). `shown` is what the section renders:
// seed from whatever recency currently says, then sort by sequence.
function shown(order: AppOrder, apps: ReturnType<typeof currentApps>): string[] {
  assignSequences(order, apps);
  return bySequence(apps, order).map((a) => a.slug);
}

describe("the displayed order", () => {
  it("seeds from recency, newest first", () => {
    const order: AppOrder = new Map();
    expect(shown(order, of(["app1", 10], ["app2", 20], ["app3", 30]))).toEqual([
      "app3",
      "app2",
      "app1",
    ]);
  });

  it("does not move an app that is already listed when its work advances", () => {
    const order: AppOrder = new Map();
    shown(order, of(["app1", 30], ["app2", 20], ["app3", 10]));
    // app2 gets a brand-new task — the newest activity anywhere. It stays put.
    expect(shown(order, of(["app1", 30], ["app2", 99], ["app3", 10]))).toEqual([
      "app1",
      "app2",
      "app3",
    ]);
  });

  it("puts an app that is NOT listed at the top, however old its task", () => {
    const order: AppOrder = new Map();
    shown(order, of(["app1", 50], ["app2", 40]));
    expect(shown(order, of(["app1", 50], ["app2", 40], ["fresh", 1]))).toEqual([
      "fresh",
      "app1",
      "app2",
    ]);
  });

  it("numbers several new apps so the newest of them lands on top", () => {
    const order: AppOrder = new Map();
    expect(shown(order, of(["a", 1], ["b", 2], ["c", 3]))[0]).toBe("c");
    expect(shown(order, of(["a", 1], ["b", 2], ["c", 3], ["d", 4], ["e", 5]))).toEqual([
      "e",
      "d",
      "c",
      "b",
      "a",
    ]);
  });

  it("keeps a reorder across later recompute, whatever recency says", () => {
    const order: AppOrder = new Map();
    shown(order, of(["app1", 10], ["app2", 20], ["app3", 30]));
    reorderTo(order, ["app1", "app3", "app2"]);
    expect(shown(order, of(["app1", 10], ["app2", 99], ["app3", 30]))).toEqual([
      "app1",
      "app3",
      "app2",
    ]);
  });

  it("remembers an app that leaves, and gives it its slot back", () => {
    const order: AppOrder = new Map();
    shown(order, of(["app1", 30], ["app2", 20], ["app3", 10]));
    // app3's tasks are all archived: no row, but the slot is remembered — the
    // store is add-only so that two tabs cannot fight over it (see the lib
    // header), and the display is what prunes.
    expect(shown(order, of(["app1", 30], ["app2", 20]))).toEqual(["app1", "app2"]);
    expect(order.has("app3")).toBe(true);
    expect(shown(order, of(["app1", 30], ["app2", 20], ["app3", 10]))).toEqual([
      "app1",
      "app2",
      "app3",
    ]);
  });

  it("forgets the oldest slots the desk is not using once past the limit", () => {
    const order: AppOrder = new Map();
    const many = Array.from({ length: REMEMBERED_LIMIT + 5 }, (_, i): [string, number] => [
      "app" + i,
      i,
    ]);
    shown(order, of(...many));
    expect(order.size).toBe(REMEMBERED_LIMIT + 5); // every one is LIVE, none droppable
    // Now only two are on the desk. The trim takes the lowest slots that have
    // no row, and never one that has.
    shown(order, of(["app0", 1], ["app1", 2]));
    expect(order.size).toBe(REMEMBERED_LIMIT);
    expect(order.has("app0")).toBe(true);
    expect(order.has("app1")).toBe(true);
    expect(order.has("app" + (REMEMBERED_LIMIT + 4))).toBe(true); // newest slot kept
    expect(order.has("app2")).toBe(false); // oldest droppable slot went
  });

  it("holds the order through a pulse that has not loaded yet", () => {
    const order: AppOrder = new Map();
    shown(order, of(["app1", 10], ["app2", 20]));
    expect(shown(order, [])).toEqual([]);
    expect(shown(order, of(["app1", 10], ["app2", 20]))).toEqual(["app2", "app1"]);
  });
});

describe("the saved order", () => {
  it("round-trips the display order through the store shape", () => {
    const order: AppOrder = new Map();
    reorderTo(order, ["b", "a", "c"]);
    expect(orderedSlugs(order)).toEqual(["b", "a", "c"]);
    const again: AppOrder = new Map();
    reorderTo(again, parseSavedOrder(JSON.stringify(orderedSlugs(order))));
    expect(orderedSlugs(again)).toEqual(["b", "a", "c"]);
  });

  it("holds a saved order against recency, and puts an unsaved app on top", () => {
    const order: AppOrder = new Map();
    reorderTo(order, parseSavedOrder('["app1","app3","app2"]'));
    const apps = currentApps(
      [
        task("k1", ROOT + "app1", "done", 10),
        task("k2", ROOT + "app2", "done", 99),
        task("k3", ROOT + "app3", "done", 50),
        task("k4", ROOT + "later", "done", 1),
      ],
      FUSED,
    );
    assignSequences(order, apps);
    expect(bySequence(apps, order).map((a) => a.slug)).toEqual([
      "later",
      "app1",
      "app3",
      "app2",
    ]);
  });

  it("keeps a slug the saved order remembers and the desk is not using", () => {
    const order: AppOrder = new Map();
    reorderTo(order, parseSavedOrder('["gone","live"]'));
    assignSequences(order, currentApps([task("k", ROOT + "live", "done", 5)], FUSED));
    expect(orderedSlugs(order)).toEqual(["gone", "live"]);
  });

  it("converges between two tabs whose pulses disagree", () => {
    // Bugbot, 2026-08-26 (High): with a store that PRUNED, two tabs sharing one
    // localStorage row could not settle. Tab A sees `c`; tab B has not polled
    // since it appeared, so B writes {a,b}; A reads that and re-adds `c`; B
    // reads THAT and prunes it again — a write loop for as long as the pulses
    // disagree, which after an in-tab poke is up to a full poll interval.
    // Add-only assignment makes each exchange either teach a tab something or
    // change nothing, so it terminates.
    const A: AppOrder = new Map();
    const B: AppOrder = new Map();
    const liveA = of(["a", 30], ["b", 20], ["c", 10]);
    const liveB = of(["a", 30], ["b", 20]);
    // A settles first and writes.
    assignSequences(A, liveA);
    let saved = orderedSlugs(A);
    // Now the tabs take turns adopting and re-deriving. Two rounds is the bound.
    const round = (order: AppOrder, live: typeof liveA) => {
      order.clear();
      reorderTo(order, parseSavedOrder(JSON.stringify(saved)));
      assignSequences(order, live);
      const next = orderedSlugs(order);
      const wrote = JSON.stringify(next) !== JSON.stringify(saved);
      saved = next;
      return wrote;
    };
    expect(round(B, liveB)).toBe(false); // B has nothing to add, so it stays quiet
    expect(round(A, liveA)).toBe(false); // and A already knew everything saved
    // A wrote first from its own recency, so `c` is at the BOTTOM (oldest
    // activity) — and it stays there. Under the old prune, B's write would have
    // dropped it and A's next pass would have re-added it on top, forever.
    expect(saved).toEqual(["a", "b", "c"]);
  });

  it("settles in one round when the tab that wrote first knew less", () => {
    const A: AppOrder = new Map();
    const B: AppOrder = new Map();
    const liveA = of(["a", 30], ["b", 20], ["c", 10]);
    const liveB = of(["a", 30], ["b", 20]);
    // The tab that has NOT seen `c` writes first this time.
    assignSequences(B, liveB);
    let saved = orderedSlugs(B);
    A.clear();
    reorderTo(A, parseSavedOrder(JSON.stringify(saved)));
    assignSequences(A, liveA);
    // A teaches the row about `c`, once, and it goes on top: to that saved
    // order it IS new.
    expect(orderedSlugs(A)).toEqual(["c", "a", "b"]);
    saved = orderedSlugs(A);
    // B adopts and has nothing to add back — the exchange is over.
    B.clear();
    reorderTo(B, parseSavedOrder(JSON.stringify(saved)));
    assignSequences(B, liveB);
    expect(orderedSlugs(B)).toEqual(saved);
  });

  it("degrades anything unreadable to no saved order", () => {
    expect(parseSavedOrder(null)).toEqual([]);
    expect(parseSavedOrder("")).toEqual([]);
    expect(parseSavedOrder("not json")).toEqual([]);
    expect(parseSavedOrder('{"a":1}')).toEqual([]);
    expect(parseSavedOrder("[1,2,3]")).toEqual([]);
  });

  it("drops junk entries and collapses duplicates rather than sharing a sequence", () => {
    expect(parseSavedOrder('["a",7,"",null,"b","a"]')).toEqual(["a", "b"]);
    const order: AppOrder = new Map();
    reorderTo(order, parseSavedOrder('["a","b","a"]'));
    expect([...order.values()].length).toBe(new Set(order.values()).size);
  });
});

describe("moveSlug", () => {
  const list = ["a", "b", "c"];

  it("inserts above or below the target", () => {
    expect(moveSlug(list, "c", "a", false)).toEqual(["c", "a", "b"]);
    expect(moveSlug(list, "c", "a", true)).toEqual(["a", "c", "b"]);
    expect(moveSlug(list, "a", "c", true)).toEqual(["b", "c", "a"]);
  });

  it("is a no-op for a drag onto itself or onto a slug it does not know", () => {
    expect(moveSlug(list, "a", "a", true)).toBe(list);
    expect(moveSlug(list, "a", "zz", true)).toBe(list);
  });
});

describe("CurrentAppsSection's half of the saved order", () => {
  const SECTION = readFileSync(
    new URL("./CurrentAppsSection.tsx", import.meta.url),
    "utf8",
  );

  it("touches localStorage only inside a try", () => {
    // A blocked or full store costs the saved order, never the section — the
    // same rule Scheduled.tsx's view memory and the sidebar's task dismissals
    // follow. Pinned as source text because the alternative is mounting the
    // shell with a throwing store.
    expect(SECTION).toContain("localStorage.getItem(ORDER_KEY)");
    expect(SECTION).toContain("localStorage.setItem(ORDER_KEY");
    for (const call of ["localStorage.getItem", "localStorage.setItem"]) {
      const before = SECTION.slice(0, SECTION.indexOf(call));
      // Every touch is preceded by a `try {` that no `catch` has closed yet.
      expect((before.match(/try \{/g) ?? []).length).toBeGreaterThan(
        (before.match(/\} catch/g) ?? []).length,
      );
    }
  });

  it("hydrates the order at import, not in an effect", () => {
    // A synchronous read at module scope is the whole reason localStorage was
    // chosen over a server pref: the order is in hand before the first render,
    // so recency never wins a race against what the user dragged.
    const hydrate = SECTION.indexOf("adoptSavedOrder(readSavedOrder())");
    expect(hydrate).toBeGreaterThan(-1);
    expect(hydrate).toBeLessThan(SECTION.indexOf("export default function"));
  });

  it("writes only a CHANGED order", () => {
    // Bugbot, 2026-08-26: the persist effect runs on every pulse, because the
    // store hands out a fresh `rows` array each poll. An unconditional write
    // there let a second tab re-save its own pre-drag order over this tab's
    // drag on its next tick — and it is also what would make the cross-tab
    // adopt below ping-pong between two tabs forever.
    expect(SECTION).toContain("if (localStorage.getItem(ORDER_KEY) === next) return;");
  });

  it("adopts another tab's order instead of fighting it", () => {
    // `storage` fires only in OTHER documents, which is exactly the cross-tab
    // channel (the wiring App.tsx uses for the chat's activity stamp).
    expect(SECTION).toContain('window.addEventListener("storage"');
    expect(SECTION).toContain("adoptSavedOrder(parseSavedOrder(e.newValue))");
    // An absent or cleared key is not an order: adopting it must not flatten
    // the live one.
    expect(SECTION).toContain("if (!slugs.length) return;");
    // And the adopt REPLACES rather than merges — a stale slug left behind with
    // a higher sequence than anything incoming would outrank the whole list.
    const adopt = SECTION.indexOf("function adoptSavedOrder");
    const end = SECTION.indexOf("}", SECTION.indexOf("reorderTo", adopt));
    expect(SECTION.slice(adopt, end)).toContain("appOrder.clear()");
  });

  it("never saves an empty list over the order it is about to show", () => {
    // The first render (and any moment the pulse has not answered) has no apps;
    // writing that would erase the order — the same trap the sidebar's task
    // dismissals fell into (sidebar-tasks.test.ts, 2026-08-18).
    expect(SECTION).toContain("if (apps.length) saveOrder(orderedSlugs(appOrder))");
  });
});

describe("appDirOf", () => {
  it("does not prefix-match a sibling folder", () => {
    expect(appDirOf("/Users/me/Fused/localother/x", ROOT)).toBeNull();
    expect(localAppsRoot("/Users/me/Fused/")).toBe(ROOT);
  });

  it("folds a backslashed Windows fused_dir onto forward-slash task paths", () => {
    expect(localAppsRoot("C:\\Users\\me\\Fused")).toBe("C:/Users/me/Fused/local/");
  });
});

describe("isUnderDir", () => {
  it("claims the folder and its subfolders, never a sibling with the same prefix", () => {
    expect(isUnderDir(ROOT + "foo", ROOT + "foo")).toBe(true);
    expect(isUnderDir(ROOT + "foo/deep/er", ROOT + "foo")).toBe(true);
    expect(isUnderDir(ROOT + "foo2", ROOT + "foo")).toBe(false);
    expect(isUnderDir(ROOT, ROOT + "foo")).toBe(false);
  });
});

describe("app page codec", () => {
  it("round-trips a slug through the URL, escaping what needs it", () => {
    expect(appPageUrl("my app")).toBe("/apps/my%20app");
    expect(slugFromAppPath("/apps/my%20app")).toBe("my app");
    expect(appPageUrl("x", "tasks")).toBe("/apps/x?tab=tasks");
  });

  it("refuses anything that is not one folder name under the workspace", () => {
    expect(slugFromAppPath("/apps")).toBeNull();
    expect(slugFromAppPath("/apps/")).toBeNull();
    expect(slugFromAppPath("/apps/tag/name")).toBeNull();
    expect(slugFromAppPath("/apps/..")).toBeNull();
    expect(slugFromAppPath("/apps/.")).toBeNull();
    expect(slugFromAppPath("/apps/%2e%2e")).toBeNull();
    expect(slugFromAppPath("/apps/a%2Fb")).toBeNull();
    expect(slugFromAppPath("/apps/a%5Cb")).toBeNull();
    expect(slugFromAppPath("/apps/%E0%A4%A")).toBeNull();
    expect(slugFromAppPath("/tasks")).toBeNull();
  });

  it("keeps the tab in the query and leaves other params alone", () => {
    expect(appPageTab("?tab=tasks&view=board")).toBe("tasks");
    expect(appPageTab("?tab=nope")).toBe("overview");
    expect(appPageTab("")).toBe("overview");
    expect(withAppPageTab("?view=board", "tasks")).toBe("?view=board&tab=tasks");
    expect(withAppPageTab("?view=board&tab=tasks", "overview")).toBe("?view=board");
    expect(withAppPageTab("?tab=tasks", "overview")).toBe("");
  });
});
