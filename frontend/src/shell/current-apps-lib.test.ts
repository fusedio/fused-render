import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import type { CurrentAppEntry } from "@platform/lib/api";
import {
  APP_PAGE_TABS,
  moveSlug,
  appPageTabFromSearch,
  appPageUrl,
  appPathFromPath,
  currentApps,
  isUnderDir,
  parseSavedOrder,
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

describe("currentApps", () => {
  it("keeps the store's order and marks running by containment", () => {
    const out = currentApps([entry(A), entry(B), entry(L, "linked")], [B + "/sub"]);
    expect(out.map((a) => a.path)).toEqual([A, B, L]);
    expect(out.map((a) => a.running)).toEqual([false, true, false]);
    expect(out[2].kind).toBe("linked");
  });

  it("counts the tasks WAITING on each folder, and defaults to none", () => {
    // "· 2 queued" on the sidebar row (the project queue). A COUNT and not a
    // flag, because the row prints the number — two tasks waiting on one folder
    // is the ordinary case the moment the queue is on.
    const out = currentApps(
      [entry(A), entry(B)],
      [A],
      new Set(),
      [B + "/sub", B + "/other", A + "/x"],
    );
    expect(out.map((a) => a.queued)).toEqual([1, 2]);
    // Containment is the same test the running dot uses: a task on a subfolder
    // belongs to the app.
    expect(out[1].running).toBe(false);
    // A caller that predates the queue — and every suite — still answers 0
    // rather than being rewritten to say so.
    expect(currentApps([entry(A)], []).map((a) => a.queued)).toEqual([0]);
  });
});

describe("the displayed order", () => {
  it("is the server's array, drawn as it arrives (D1377)", () => {
    // No client numbering: whatever order GET /api/current-apps answers in is
    // the order on screen, newest-on-top included (the server inserts at 0).
    const out = currentApps([entry(L, "linked"), entry(B), entry(A)], []);
    expect(out.map((a) => a.path)).toEqual([L, B, A]);
  });
});

describe("the import-only saved-order parser", () => {
  it("reads a pre-D1377 row back as a path list", () => {
    expect(parseSavedOrder(JSON.stringify([B, A]))).toEqual([B, A]);
    expect(parseSavedOrder(null)).toEqual([]);
  });

  it("degrades a corrupt row to no order", () => {
    expect(parseSavedOrder("nope")).toEqual([]);
    expect(parseSavedOrder(JSON.stringify({ a: 1 }))).toEqual([]);
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

describe("CurrentAppsSection's half of the order", () => {
  const SECTION = readFileSync(
    new URL("./CurrentAppsSection.tsx", import.meta.url),
    "utf8",
  );

  it("keeps no order of its own — nothing sorts, nothing numbers", () => {
    for (const gone of ["assignSequences", "bySequence", "reorderTo", "orderedSlugs", "appOrder"]) {
      expect(SECTION).not.toContain(gone);
    }
    expect(SECTION).not.toContain("localStorage.setItem(LEGACY_ORDER_KEY");
  });

  it("POSTs the whole list ONLY from the drop handler", () => {
    const calls = SECTION.split("setCurrentAppsOrder(").length - 1;
    expect(calls).toBe(2); // the one-shot import, and the drop
    const drop = SECTION.indexOf("onDrop: (e) => {");
    const dropEnd = SECTION.indexOf("onDragEnd:", drop);
    const handler = SECTION.slice(drop, dropEnd);
    expect(handler).toContain("moveSlug(paths, from, path, isBelow(e))");
    expect(handler).toContain("setCurrentAppsOrder(next)");
    // The answer is adopted and the other windows are nudged to refetch.
    expect(handler).toContain("adopt(r.apps ?? [])");
    expect(handler).toContain("announceDeskChanged(");
  });

  it("imports the old order once at module load, then removes the key", () => {
    const imp = SECTION.indexOf("parseSavedOrder(localStorage.getItem(LEGACY_ORDER_KEY))");
    expect(imp).toBeGreaterThan(-1);
    expect(imp).toBeLessThan(SECTION.indexOf("export default function"));
    expect(SECTION).toContain('LEGACY_ORDER_KEY = "fused-render:current-apps-order:v2"');
    expect(SECTION).toContain("localStorage.removeItem(LEGACY_ORDER_KEY)");
  });

  it("imports the old order ONLY over a desk nobody has arranged (D1377)", () => {
    // A stale origin left behind by the port walk must not overwrite an order
    // the user dragged since: the server's rows are read first, and the old
    // list goes up only while they are still in added order, newest first.
    const imp = SECTION.slice(
      SECTION.indexOf("parseSavedOrder(localStorage.getItem(LEGACY_ORDER_KEY))"),
      SECTION.indexOf("/** The desk's table, fetched on mount"),
    );
    expect(imp.indexOf("getCurrentApps()")).toBeLessThan(imp.indexOf("setCurrentAppsOrder(legacy)"));
    expect(imp).toContain("if (!neverArranged(current.apps ?? [])) {\n          dropLegacyOrder();\n          return;");
    expect(SECTION).toContain("return y.at - x.at || x.i - y.i;");
  });

  it("keeps the fold in the ui-layout doc, not in localStorage", () => {
    expect(SECTION).toContain("getUiLayout().currentAppsCollapsed ?? false");
    expect(SECTION).toContain("patchUiLayout((d) => ({ ...d, currentAppsCollapsed: next }))");
    // The old key's import is ui-layout.ts's, with the rest of the layout.
    expect(SECTION).not.toContain("fused-render:current-apps-collapsed");
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
    expect(url).toBe("/apps/Users/me/Fused/local/my%20app%231?_tab=tasks");
    const [pathname, search] = url.split("?");
    expect(appPathFromPath(pathname)).toBe(dir);
    expect(appPageTabFromSearch("?" + search)).toBe("tasks");
  });

  it("gives the default tab exactly one address — no _tab", () => {
    expect(appPageUrl("/Users/me/a")).toBe("/apps/Users/me/a");
    expect(appPageUrl("/Users/me/a", "overview", "?_tab=tasks&view=board")).toBe(
      "/apps/Users/me/a?view=board",
    );
  });

  it("carries a tab's own params across a switch, rewriting only _tab", () => {
    expect(appPageUrl("/Users/me/a", "files", "?view=board")).toBe(
      "/apps/Users/me/a?view=board&_tab=files",
    );
    expect(appPageUrl("/Users/me/a", "tasks", "?_tab=files&file=x")).toBe(
      "/apps/Users/me/a?_tab=tasks&file=x",
    );
    expect(appPageUrl("/Users/me/a", "api")).toBe("/apps/Users/me/a?_tab=api");
  });

  it("treats a folder named like a tab as a folder", () => {
    expect(appPathFromPath("/apps/Users/me/tasks")).toBe("/Users/me/tasks");
    expect(appPathFromPath("/apps/overview")).toBe("/overview");
  });

  it("refuses dot segments, empties and bad escapes", () => {
    expect(appPathFromPath("/apps/")).toBeNull();
    expect(appPathFromPath("/apps/Users/../etc")).toBeNull();
    expect(appPathFromPath("/apps/%E0%A4%A")).toBeNull();
    expect(appPathFromPath("/tasks")).toBeNull();
  });

  it("reads the tab from the query, falling back to the overview", () => {
    expect(appPageTabFromSearch("?_tab=files")).toBe("files");
    expect(appPageTabFromSearch("?_tab=api")).toBe("api");
    expect(appPageTabFromSearch("?view=board")).toBe("overview");
    expect(appPageTabFromSearch("?_tab=bogus")).toBe("overview");
    expect(appPageTabFromSearch("")).toBe("overview");
  });

  it("the Git tab is gone: APP_PAGE_TABS never offers it, and a stale ?_tab=git deep link falls back to the overview like any other unknown tab", () => {
    expect(APP_PAGE_TABS).not.toContain("git");
    expect(appPageTabFromSearch("?_tab=git")).toBe("overview");
  });
});

// bugbot, PR #969: Current Apps read a project as live off `status ===
// "in_progress"` alone, while tasksPulse already counts a needs_attention row
// (a run parked on a card) as running too — the Tasks rail could say work was
// in flight while the same project's Current Apps row sat idle. Source-text
// checked, like the order describe above, since the reading lives
// inline in the component rather than in a pure export.
describe("CurrentAppsSection's running projects", () => {
  const SECTION = readFileSync(
    new URL("./CurrentAppsSection.tsx", import.meta.url),
    "utf8",
  );

  it("counts needs_attention as live, via the same inFlight helper tasks-lib uses", () => {
    expect(SECTION).toContain("inFlight(statusColumn(r.status))");
    expect(SECTION).not.toContain('r.status === "in_progress"');
  });
});
