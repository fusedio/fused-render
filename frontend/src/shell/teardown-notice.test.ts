// Archiving removes a task's clean worktrees and KEEPS the rest. The reader is
// told about what was kept, and why, in words: a silent keep is a leaked folder.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { _resetNotificationsForTest, getPopupNotification, getRetainedNotifications } from "@platform/lib/notifications";
import type { TeardownKept, TeardownReport } from "@platform/lib/api";
import {
  batchKeptNotice,
  beginArchiveBatch,
  closedToastText,
  keptReasonText,
  keptWorktreeNotice,
  routeArchiveReport,
  teardownSummary,
} from "@shell/teardown-notice";

const kept = (over: Partial<TeardownKept> = {}): TeardownKept => ({
  path: "/r/.claude/worktrees/feat-x",
  reasons: ["dirty"],
  dirty_files: 3,
  unpushed: 0,
  ...over,
});
const report = (k: TeardownKept[]): TeardownReport => ({
  stopped: 0, killed: [], removed: [], kept: k, gone: [],
});

describe("keptReasonText", () => {
  test("names uncommitted files and unpushed commits with their counts", () => {
    expect(keptReasonText(kept({ reasons: ["dirty", "unpushed"], dirty_files: 3, unpushed: 2 })))
      .toBe("3 uncommitted files, 2 unpushed commits");
  });
  test("singulars", () => {
    expect(keptReasonText(kept({ dirty_files: 1 }))).toBe("1 uncommitted file");
    expect(keptReasonText(kept({ reasons: ["unpushed"], unpushed: 1 }))).toBe("1 unpushed commit");
  });
  test("unknown says it could not tell", () => {
    expect(keptReasonText(kept({ reasons: ["unknown"], dirty_files: null, unpushed: null })))
      .toBe("could not read its git state");
  });
  test("a folder outside the managed dir and a failed removal", () => {
    expect(keptReasonText(kept({ reasons: ["not-managed"] }))).toBe("not made by Fused Render");
    expect(keptReasonText(kept({ reasons: ["remove-failed"] }))).toBe("git would not remove it");
  });
});

describe("keptWorktreeNotice", () => {
  test("nothing kept, nothing said", () => {
    expect(keptWorktreeNotice("s1", report([]))).toBeNull();
    expect(keptWorktreeNotice("s1", undefined)).toBeNull();
  });
  test("one kept worktree is named with its folder and reason", () => {
    const n = keptWorktreeNotice("s1", report([kept()]))!;
    expect(n.title).toBe("Kept worktree feat-x");
    expect(n.detail).toBe("3 uncommitted files");
    expect(n.paths).toEqual(["/r/.claude/worktrees/feat-x"]);
  });
  test("several are counted and listed", () => {
    const n = keptWorktreeNotice("s1", report([kept(), kept({ path: "/r/.claude/worktrees/b", reasons: ["unpushed"], unpushed: 2 })]))!;
    expect(n.title).toBe("Kept 2 worktrees");
    expect(n.detail).toBe("feat-x: 3 uncommitted files; b: 2 unpushed commits");
    expect(n.paths.length).toBe(2);
  });
});

describe("batchKeptNotice", () => {
  test("a bulk archive raises ONE notice naming every kept worktree", () => {
    const n = batchKeptNotice([
      { key: "a", report: report([kept({ path: "/r/w/one" })]) },
      { key: "b", report: report([]) },
      { key: "c", report: report([kept({ path: "/r/w/two", reasons: ["unpushed"], unpushed: 2 })]) },
    ]);
    expect(n?.title).toBe("Kept 2 worktrees from 2 archived tasks");
    expect(n?.detail).toBe("one: 3 uncommitted files; two: 2 unpushed commits");
    expect(n?.byKey.map((b) => b.key)).toEqual(["a", "c"]);
  });
  test("nothing kept is no notice; one kept is the single notice", () => {
    expect(batchKeptNotice([{ key: "a", report: report([]) }, { key: "b", report: undefined }])).toBeNull();
    expect(batchKeptNotice([{ key: "a", report: report([kept()]) }])?.title).toBe("Kept worktree feat-x");
  });
});

describe("archive batches", () => {
  test("two overlapping batches each keep their own reports and keys", () => {
    const a = beginArchiveBatch(["a1", "a2"]);
    const b = beginArchiveBatch(["b1"]);
    expect(routeArchiveReport("a1", report([kept({ path: "/r/.claude/worktrees/A" })]))).toBe(true);
    expect(routeArchiveReport("b1", report([kept({ path: "/r/.claude/worktrees/B" })]))).toBe(true);
    expect(routeArchiveReport("a2", report([]))).toBe(true);
    const nb = b.end();
    expect(nb!.byKey).toEqual([{ key: "b1", paths: ["/r/.claude/worktrees/B"] }]);
    // b ending did not close a: its report still lands in a.
    expect(routeArchiveReport("a2", report([kept({ path: "/r/.claude/worktrees/A2" })]))).toBe(true);
    const na = a.end();
    expect(na!.byKey.map((x) => x.key)).toEqual(["a1", "a2"]);
  });

  test("a single archive during a batch is not swallowed by it", () => {
    const a = beginArchiveBatch(["a1"]);
    expect(routeArchiveReport("lone", report([kept()]))).toBe(false);
    expect(routeArchiveReport("a1", report([]))).toBe(true);
    expect(a.end()).toBeNull();
    expect(routeArchiveReport("a1", report([]))).toBe(false);
  });
});

describe("closedToastText", () => {
  test("says what was stopped and removed, and what Undo cannot do", () => {
    const r = { ...report([]), stopped: 1, killed: [5], removed: ["/x"] };
    expect(teardownSummary([r])).toBe("stopped 2 processes, removed 1 worktree");
    expect(closedToastText("Closed T123", [r])).toBe(
      "Closed T123 \u2014 stopped 2 processes, removed 1 worktree; Undo won't restart them",
    );
  });
  test("sums a bulk archive, and stays plain when nothing was torn down", () => {
    const one = { ...report([]), stopped: 1 };
    expect(teardownSummary([one, one, undefined])).toBe("stopped 2 processes");
    expect(closedToastText("Archived 2 tasks", [report([]), undefined])).toBe("Archived 2 tasks");
  });
});

// "Remove anyway": whatever the server answers, the click must visibly do
// something. The real notifications store is used; only fetch is stubbed.
describe("Remove anyway", () => {
  const realFetch = globalThis.fetch;
  let calls = 0;
  const answer = (body: unknown | Error) => {
    globalThis.fetch = (async () => {
      calls++;
      if (body instanceof Error) throw body;
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }) as unknown as typeof fetch;
  };
  // Raise the batch notice for one kept worktree and return its row.
  const raised = () => {
    const b = beginArchiveBatch(["t1"]);
    routeArchiveReport("t1", report([kept()]));
    b.end();
    const row = getRetainedNotifications().find((n) => n.title.startsWith("Kept"))!;
    return row;
  };
  const settle = () => new Promise((r) => setTimeout(r, 20));
  const titles = () => getRetainedNotifications().map((n) => n.title);

  beforeEach(() => { calls = 0; _resetNotificationsForTest(); });
  afterEach(() => { globalThis.fetch = realFetch; });

  test("removed: the Kept notice leaves and Removed 1 worktree appears", async () => {
    answer({ ok: true, key: "t1", removed: ["/r/.claude/worktrees/feat-x"], kept: [], gone: [] });
    raised().action!.onClick();
    await settle();
    expect(titles().some((t) => t.startsWith("Kept"))).toBe(false);
    expect(getPopupNotification()?.title).toBe("Removed 1 worktree");
  });
  test("already gone counts as removed", async () => {
    answer({ ok: true, key: "t1", removed: [], kept: [], gone: ["/a", "/b"] });
    raised().action!.onClick();
    await settle();
    expect(titles().some((t) => t.startsWith("Kept"))).toBe(false);
    expect(getPopupNotification()?.title).toBe("Removed 2 worktrees");
  });
  test("still kept: error notice with the reason, original dismissed", async () => {
    answer({ ok: true, key: "t1", removed: [], kept: [kept({ reasons: ["remove-failed"] })], gone: [] });
    raised().action!.onClick();
    await settle();
    const rows = getRetainedNotifications();
    expect(rows.map((n) => n.title)).toEqual(["Could not remove"]);
    expect(rows[0].detail).toBe("git would not remove it");
    expect(rows[0].tone).toBe("error");
  });
  test("request fails: error notice, original stays for a retry", async () => {
    answer(new Error("offline"));
    raised().action!.onClick();
    await settle();
    expect(titles().sort()).toEqual(["Could not remove the worktrees", "Kept worktree feat-x"].sort());
  });
  test("a second click while one request is in flight is ignored", async () => {
    answer({ ok: true, key: "t1", removed: ["/x"], kept: [], gone: [] });
    const row = raised();
    row.action!.onClick();
    row.action!.onClick();
    await settle();
    expect(calls).toBe(1);
  });
});
