import { describe, expect, it } from "bun:test";
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

// The displayed order (sequence per app). `shown` is what the section renders:
// seed from whatever recency currently says, then sort by sequence.
function shown(order: AppOrder, apps: ReturnType<typeof currentApps>): string[] {
  assignSequences(order, apps);
  return bySequence(apps, order).map((a) => a.slug);
}

describe("the displayed order", () => {
  const of = (...specs: [string, number][]) =>
    currentApps(
      specs.map(([slug, at]) => task("k-" + slug, ROOT + slug, "done", at)),
      FUSED,
    );

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

  it("re-adds an app as new once it has left the list", () => {
    const order: AppOrder = new Map();
    shown(order, of(["app1", 30], ["app2", 20], ["app3", 10]));
    // app3's tasks are all archived — it is off the desk, so it loses its slot.
    expect(shown(order, of(["app1", 30], ["app2", 20]))).toEqual(["app1", "app2"]);
    expect(order.has("app3")).toBe(false);
    expect(shown(order, of(["app1", 30], ["app2", 20], ["app3", 10]))[0]).toBe("app3");
  });

  it("holds the order through a pulse that has not loaded yet", () => {
    const order: AppOrder = new Map();
    shown(order, of(["app1", 10], ["app2", 20]));
    expect(shown(order, [])).toEqual([]);
    expect(shown(order, of(["app1", 10], ["app2", 20]))).toEqual(["app2", "app1"]);
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
