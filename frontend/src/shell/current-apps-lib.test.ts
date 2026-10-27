import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import type { CurrentAppEntry } from "@platform/lib/api";
import {
  APP_PAGE_TABS,
  appPageTabFromSearch,
  appPageUrl,
  appPathFromPath,
  currentApps,
  isUnderDir,
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
  it("is the server's array, drawn as it arrives (by recency, server-sorted)", () => {
    // No client numbering: whatever order GET /api/current-apps answers in is
    // the order on screen — the server sorts by newest task activity.
    const out = currentApps([entry(L, "linked"), entry(B), entry(A)], []);
    expect(out.map((a) => a.path)).toEqual([L, B, A]);
  });
});

describe("CurrentAppsSection's half of the order", () => {
  const SECTION = readFileSync(
    new URL("./CurrentAppsSection.tsx", import.meta.url),
    "utf8",
  );

  it("keeps no order of its own — nothing sorts, numbers, drags or imports one", () => {
    // Recency is the server's (owner, 2026-10-23: no user order, no dragging).
    for (const gone of [
      "assignSequences", "bySequence", "reorderTo", "orderedSlugs", "appOrder",
      "setCurrentAppsOrder", "moveSlug", "parseSavedOrder", "LEGACY_ORDER_KEY",
      "onDragStart", "onDrop", "draggable",
    ]) {
      expect(SECTION).not.toContain(gone);
    }
  });

  it("keeps the cross: the row's ✕ hides the app and archives its tasks", () => {
    expect(SECTION).toContain("current-app-archive");
    expect(SECTION).toContain("await removeCurrentApp(app.path);");
  });

  it("keeps the fold in the ui-layout doc, not in localStorage", () => {
    // The accordion store (platform/ui/sidebar/sidebar-accordion.ts) owns the
    // doc's `currentAppsCollapsed` flag; the section only asks it.
    expect(SECTION).toContain('const collapsed = !useSidebarSectionOpen(sectionKey);');
    expect(SECTION).toContain("setSidebarSectionOpen(sectionKey, collapsed)");
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

  it("carries a Windows drive as the explorer does", () => {
    const url = appPageUrl("C:\\Users\\me\\Fused\\local\\a", "files");
    expect(url).toBe("/apps/C%3A/Users/me/Fused/local/a?_tab=files");
    expect(appPathFromPath(url.split("?")[0])).toBe("C:/Users/me/Fused/local/a");
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

  it("Community replaced Proposals: ?_tab=proposals names the community tab", () => {
    expect(APP_PAGE_TABS).toContain("community");
    expect(APP_PAGE_TABS).not.toContain("proposals");
    expect(appPageTabFromSearch("?_tab=community")).toBe("community");
    expect(appPageTabFromSearch("?_tab=proposals")).toBe("community");
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
