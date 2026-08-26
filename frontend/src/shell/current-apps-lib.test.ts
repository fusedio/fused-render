import { describe, expect, it } from "bun:test";
import type { TaskPulseTask } from "@platform/lib/api";
import {
  appDirOf,
  appPageTabFromPath,
  appPageUrl,
  isBareAppPath,
  currentApps,
  isUnderDir,
  localAppsRoot,
  slugFromAppPath,
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
