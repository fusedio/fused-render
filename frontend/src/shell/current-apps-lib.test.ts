import { describe, expect, it } from "bun:test";
import type { TaskPulseTask } from "@platform/lib/api";
import { appDirOf, currentApps, localAppsRoot } from "./current-apps-lib";

const HOME = "/Users/me";
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
      HOME,
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

  it("caps the list at five", () => {
    const tasks = Array.from({ length: 8 }, (_, i) =>
      task(`k${i}`, `${ROOT}app${i}`, "done", i),
    );
    const apps = currentApps(tasks, HOME);
    expect(apps.length).toBe(5);
    expect(apps[0].slug).toBe("app7");
  });

  it("is empty until home is known", () => {
    expect(currentApps([task("a", ROOT + "alpha")], "")).toEqual([]);
  });
});

describe("appDirOf", () => {
  it("does not prefix-match a sibling folder", () => {
    expect(appDirOf("/Users/me/Fused/localother/x", ROOT)).toBeNull();
    expect(localAppsRoot("/Users/me/")).toBe(ROOT);
  });
});
