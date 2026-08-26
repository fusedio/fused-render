import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import type { CurrentAppEntry } from "@platform/lib/api";
import {
  assignSequences,
  bySequence,
  moveSlug,
  reorderTo,
  appPageTabFromPath,
  appPageUrl,
  appPathFromPath,
  isBareAppPath,
  currentApps,
  isUnderDir,
  orderedSlugs,
  parseSavedOrder,
  type AppOrder,
  type CurrentApp,
} from "./current-apps-lib";

function entry(path: string, kind: CurrentAppEntry["kind"] = "workspace"): CurrentAppEntry {
  return {
    path,
    name: path.split("/").pop()!,
    kind,
    entry: path + "/index.html",
    exists: true,
    added_at: 0,
  };
}

const A = "/Users/me/Fused/local/a";
const B = "/Users/me/Fused/showcase/b";
const L = "/Users/me/elsewhere/linked";

function apps(...paths: string[]): CurrentApp[] {
  return currentApps(paths.map((p) => entry(p)), []);
}

describe("currentApps", () => {
  it("keeps the store's order and marks running by containment", () => {
    const out = currentApps([entry(A), entry(B), entry(L, "linked")], [B + "/sub"]);
    expect(out.map((a) => a.path)).toEqual([A, B, L]);
    expect(out.map((a) => a.running)).toEqual([false, true, false]);
    expect(out[2].kind).toBe("linked");
  });
});

describe("the displayed order", () => {
  it("seeds from added order, newest on top", () => {
    const order: AppOrder = new Map();
    const found = apps(A, B);
    assignSequences(order, found);
    expect(bySequence(found, order).map((a) => a.path)).toEqual([B, A]);
  });

  it("puts an app that is NOT listed at the top and keeps a reorder", () => {
    const order: AppOrder = new Map();
    assignSequences(order, apps(A, B));
    reorderTo(order, [A, B]);
    const found = apps(A, B, L);
    assignSequences(order, found);
    expect(bySequence(found, order).map((a) => a.path)).toEqual([L, A, B]);
  });

  it("forgets an app that leaves, so it comes back on top", () => {
    const order: AppOrder = new Map();
    assignSequences(order, apps(A, B));
    reorderTo(order, [A, B]);
    assignSequences(order, apps(B));
    expect(order.has(A)).toBe(false);
    const found = apps(B, A);
    assignSequences(order, found);
    expect(bySequence(found, order).map((a) => a.path)).toEqual([A, B]);
  });

  it("holds the order through a list that has not loaded yet", () => {
    const order: AppOrder = new Map();
    assignSequences(order, apps(A, B));
    assignSequences(order, []);
    expect(order.size).toBe(2);
  });
});

describe("the saved order", () => {
  it("round-trips the display order through the store shape", () => {
    const order: AppOrder = new Map();
    reorderTo(order, [B, A]);
    const saved = JSON.stringify(orderedSlugs(order));
    expect(parseSavedOrder(saved)).toEqual([B, A]);
  });

  it("degrades a corrupt row to no order", () => {
    expect(parseSavedOrder("nope")).toEqual([]);
    expect(parseSavedOrder(JSON.stringify([1, A, A, ""]))).toEqual([A]);
  });
});

describe("moveSlug", () => {
  it("inserts above or below the target", () => {
    expect(moveSlug([A, B, L], L, A, false)).toEqual([L, A, B]);
    expect(moveSlug([A, B, L], A, B, true)).toEqual([B, A, L]);
  });
  it("is a no-op for a drag onto itself or onto an unknown target", () => {
    expect(moveSlug([A, B], A, A, false)).toEqual([A, B]);
    expect(moveSlug([A, B], A, "/x", false)).toEqual([A, B]);
  });
});

describe("CurrentAppsSection's half of the saved order", () => {
  const SECTION = readFileSync(
    new URL("./CurrentAppsSection.tsx", import.meta.url),
    "utf8",
  );

  it("touches localStorage only inside a try", () => {
    expect(SECTION).toContain("localStorage.getItem(ORDER_KEY)");
    expect(SECTION).toContain("localStorage.setItem(ORDER_KEY");
    for (const call of ["localStorage.getItem", "localStorage.setItem"]) {
      const before = SECTION.slice(0, SECTION.indexOf(call));
      expect((before.match(/try \{/g) ?? []).length).toBeGreaterThan(
        (before.match(/\} catch/g) ?? []).length,
      );
    }
  });

  it("hydrates the order at import, not in an effect", () => {
    const hydrate = SECTION.indexOf("adoptSavedOrder(readSavedOrder())");
    expect(hydrate).toBeGreaterThan(-1);
    expect(hydrate).toBeLessThan(SECTION.indexOf("export default function"));
  });

  it("writes only a CHANGED order, and ONLY from the drop handler", () => {
    expect(SECTION).toContain(
      "if (localStorage.getItem(ORDER_KEY) === next) return;",
    );
    const calls = SECTION.split("saveOrder(").length - 1;
    expect(calls).toBe(2); // the declaration, and the one call
    const drop = SECTION.indexOf("onDrop: (e) => {");
    const dropEnd = SECTION.indexOf("onDragEnd:", drop);
    expect(SECTION.slice(drop, dropEnd)).toContain("saveOrder(next)");
  });

  it("reads the desk from its own endpoint, not from the pulse", () => {
    expect(SECTION).toContain("getCurrentApps()");
    expect(SECTION).toContain("removeCurrentApp(app.path)");
    expect(SECTION).not.toContain("archiveTask(");
  });
});

describe("isUnderDir", () => {
  it("claims the folder and its subfolders, never a sibling with the same prefix", () => {
    expect(isUnderDir(A, A)).toBe(true);
    expect(isUnderDir(A + "/sub", A)).toBe(true);
    expect(isUnderDir(A + "2", A)).toBe(false);
  });
});

describe("app page codec", () => {
  it("round-trips a folder through the URL, escaping what needs it", () => {
    const dir = "/Users/me/Fused/local/my app#1";
    const url = appPageUrl(dir, "tasks");
    expect(url).toBe("/apps/Users/me/Fused/local/my%20app%231/tasks");
    expect(appPathFromPath(url)).toBe(dir);
    expect(appPageTabFromPath(url)).toBe("tasks");
  });

  it("carries a Windows drive as the explorer does", () => {
    const url = appPageUrl("C:\\Users\\me\\Fused\\local\\a", "files");
    expect(url).toBe("/apps/C%3A/Users/me/Fused/local/a/files");
    expect(appPathFromPath(url)).toBe("C:/Users/me/Fused/local/a");
  });

  it("refuses dot segments, empties and bad escapes", () => {
    expect(appPathFromPath("/apps/")).toBeNull();
    expect(appPathFromPath("/apps/overview")).toBeNull();
    expect(appPathFromPath("/apps/Users/../etc/overview")).toBeNull();
    expect(appPathFromPath("/apps/%E0%A4%A/overview")).toBeNull();
    expect(appPathFromPath("/tasks")).toBeNull();
  });

  it("reads the tab from the last segment, falling back to the overview", () => {
    expect(appPageTabFromPath("/apps/Users/me/a/files")).toBe("files");
    expect(appPageTabFromPath("/apps/Users/me/a")).toBe("overview");
    expect(appPathFromPath("/apps/Users/me/a")).toBe("/Users/me/a");
  });

  it("knows the bare address that App.tsx rewrites to the default tab", () => {
    expect(isBareAppPath("/apps/Users/me/a")).toBe(true);
    expect(isBareAppPath("/apps/Users/me/a/overview")).toBe(false);
    expect(isBareAppPath("/apps")).toBe(false);
  });
});
