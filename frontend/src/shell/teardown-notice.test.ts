// Archiving removes a task's clean worktrees and KEEPS the rest. The reader is
// told about what was kept, and why, in words: a silent keep is a leaked folder.
import { describe, expect, test } from "bun:test";
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
