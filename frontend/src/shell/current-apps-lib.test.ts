import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import type { TaskPulseTask } from "@platform/lib/api";
import {
  appDirOf,
  assignSequences,
  bySequence,
  moveSlug,
  reorderTo,
  appPageTabFromPath,
  appPageUrl,
  isBareAppPath,
  currentApps,
  isUnderDir,
  localAppsRoot,
  orderedSlugs,
  parseSavedOrder,
  slugFromAppPath,
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
function shown(
  order: AppOrder,
  apps: ReturnType<typeof currentApps>,
): string[] {
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
    expect(
      shown(order, of(["a", 1], ["b", 2], ["c", 3], ["d", 4], ["e", 5])),
    ).toEqual(["e", "d", "c", "b", "a"]);
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

  it("forgets an app that leaves, so it comes back on top", () => {
    const order: AppOrder = new Map();
    shown(order, of(["app1", 30], ["app2", 20], ["app3", 10]));
    // app3's tasks are all archived — off the desk, so the slot goes with it.
    // Nothing removed is remembered (owner, 2026-08-26).
    expect(shown(order, of(["app1", 30], ["app2", 20]))).toEqual([
      "app1",
      "app2",
    ]);
    expect(order.has("app3")).toBe(false);
    expect(shown(order, of(["app1", 30], ["app2", 20], ["app3", 10]))[0]).toBe(
      "app3",
    );
  });

  it("holds nothing but the desk, so the store needs no size limit", () => {
    const order: AppOrder = new Map();
    const many = Array.from({ length: 50 }, (_, i): [string, number] => [
      "app" + i,
      i,
    ]);
    shown(order, of(...many));
    expect(order.size).toBe(50);
    shown(order, of(["app0", 1], ["app1", 2]));
    expect(orderedSlugs(order)).toEqual(["app1", "app0"]);
  });

  it("holds the order through a pulse that has not loaded yet", () => {
    const order: AppOrder = new Map();
    shown(order, of(["app1", 10], ["app2", 20]));
    expect(shown(order, [])).toEqual([]);
    expect(shown(order, of(["app1", 10], ["app2", 20]))).toEqual([
      "app2",
      "app1",
    ]);
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

  it("drops a saved slug the desk is no longer using", () => {
    const order: AppOrder = new Map();
    reorderTo(order, parseSavedOrder('["gone","live"]'));
    assignSequences(
      order,
      currentApps([task("k", ROOT + "live", "done", 5)], FUSED),
    );
    expect(orderedSlugs(order)).toEqual(["live"]);
  });

  it("saves exactly the rows on screen, with no remembered tail to interleave", () => {
    // Bugbot, 2026-08-26 (High): a middle version remembered non-live slugs
    // while a drop renumbered only the visible run, so a remembered slug kept a
    // stale sequence and could sort above the rows the user had just arranged.
    // The prune is what removes the whole class of bug — after an assignment the
    // store IS the display.
    const order: AppOrder = new Map();
    reorderTo(order, parseSavedOrder('["old1","a","old2","b"]'));
    const live = of(["a", 20], ["b", 10]);
    assignSequences(order, live);
    expect(orderedSlugs(order)).toEqual(
      bySequence(live, order).map((x) => x.slug),
    );
    // And a drag over that list writes a contiguous arrangement, top to bottom.
    reorderTo(order, moveSlug(orderedSlugs(order), "b", "a", false));
    expect(orderedSlugs(order)).toEqual(["b", "a"]);
    expect([...order.values()].sort((x, y) => x - y)).toEqual([1, 2]);
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
    expect(SECTION).toContain(
      "if (localStorage.getItem(ORDER_KEY) === next) return;",
    );
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

  it("writes ONLY from the drop handler", () => {
    // The whole cross-tab design, pinned. Bugbot twice on 2026-08-26: a persist
    // effect keyed on the app list fires on every pulse (the store hands out a
    // fresh `rows` array per poll), and two tabs then take turns saving their
    // own view of a world they briefly disagree about — first a second tab
    // clobbering a drag, then an outright write loop. A drag is one user
    // gesture, so there is no second writer. Anything that saves on derived
    // state reopens both bugs.
    const calls = SECTION.split("saveOrder(").length - 1;
    expect(calls).toBe(2); // the declaration, and the one call
    const drop = SECTION.indexOf("onDrop: (e) => {");
    const dropEnd = SECTION.indexOf("onDragEnd:", drop);
    expect(SECTION.slice(drop, dropEnd)).toContain("saveOrder(next)");
    // And no effect may quietly become a second writer.
    expect(SECTION).not.toContain("saveOrder(orderedSlugs(appOrder))");
  });
});

describe("appDirOf", () => {
  it("does not prefix-match a sibling folder", () => {
    expect(appDirOf("/Users/me/Fused/localother/x", ROOT)).toBeNull();
    expect(localAppsRoot("/Users/me/Fused/")).toBe(ROOT);
  });

  it("folds a backslashed Windows fused_dir onto forward-slash task paths", () => {
    expect(localAppsRoot("C:\\Users\\me\\Fused")).toBe(
      "C:/Users/me/Fused/local/",
    );
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
    expect(appPageUrl("my app")).toBe("/apps/my%20app/overview");
    expect(slugFromAppPath("/apps/my%20app/overview")).toBe("my app");
    expect(slugFromAppPath("/apps/my%20app")).toBe("my app");
    expect(appPageUrl("x", "tasks")).toBe("/apps/x/tasks");
    expect(appPageUrl("x", "files")).toBe("/apps/x/files");
  });

  it("refuses anything that is not one folder name under the workspace", () => {
    expect(slugFromAppPath("/apps")).toBeNull();
    expect(slugFromAppPath("/apps/")).toBeNull();
    expect(slugFromAppPath("/apps/a/tasks/deeper")).toBeNull();
    expect(slugFromAppPath("/apps//tasks")).toBeNull();
    expect(slugFromAppPath("/apps/..")).toBeNull();
    expect(slugFromAppPath("/apps/.")).toBeNull();
    expect(slugFromAppPath("/apps/%2e%2e")).toBeNull();
    expect(slugFromAppPath("/apps/a%2Fb")).toBeNull();
    expect(slugFromAppPath("/apps/a%5Cb")).toBeNull();
    expect(slugFromAppPath("/apps/%E0%A4%A")).toBeNull();
    expect(slugFromAppPath("/tasks")).toBeNull();
  });

  it("reads the tab from the path, falling back to the overview", () => {
    expect(appPageTabFromPath("/apps/x/tasks")).toBe("tasks");
    expect(appPageTabFromPath("/apps/x/files")).toBe("files");
    expect(appPageTabFromPath("/apps/x/overview")).toBe("overview");
    // A stale two-level builder link (`/apps/<tag>/<name>`) is an unknown tab.
    expect(appPageTabFromPath("/apps/tag/name")).toBe("overview");
    expect(appPageTabFromPath("/apps/x")).toBe("overview");
  });

  it("knows the bare slug address that App.tsx rewrites to the default tab", () => {
    expect(isBareAppPath("/apps/x")).toBe(true);
    expect(isBareAppPath("/apps/x/overview")).toBe(false);
    expect(isBareAppPath("/apps/x/nope")).toBe(false);
    expect(isBareAppPath("/apps/..")).toBe(false);
    expect(isBareAppPath("/apps")).toBe(false);
  });
});
