import { describe, expect, it } from "bun:test";
import type { TaskPulseTask } from "@platform/lib/api";
import {
  appDirOf,
  appPageTab,
  appPageUrl,
  currentApps,
  isUnderDir,
  localAppsRoot,
  slugFromAppPath,
  withAppPageTab,
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

  it("caps the list at five", () => {
    const tasks = Array.from({ length: 8 }, (_, i) =>
      task(`k${i}`, `${ROOT}app${i}`, "done", i),
    );
    const apps = currentApps(tasks, FUSED);
    expect(apps.length).toBe(5);
    expect(apps[0].slug).toBe("app7");
  });

  it("is empty until the workspace root is known", () => {
    expect(currentApps([task("a", ROOT + "alpha")], "")).toEqual([]);
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
