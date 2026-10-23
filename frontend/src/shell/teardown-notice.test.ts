// Archiving removes a task's clean worktrees and KEEPS the rest. The reader is
// told about what was kept, and why, in words: a silent keep is a leaked folder.
import { describe, expect, test } from "bun:test";
import type { TeardownKept, TeardownReport } from "@platform/lib/api";
import { batchKeptNotice, keptWorktreeNotice, keptReasonText } from "@shell/teardown-notice";

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
