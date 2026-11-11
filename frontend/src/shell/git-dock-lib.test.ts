// The Git door's dots (owner, 2026-11-07: "the green dot, yellow dot and red
// dot we have for git should still pop up no matter what") and its words.
import { describe, expect, test } from "bun:test";
import type { RailGitRepo } from "@platform/lib/api";
import { gitBranch, gitChipDot, gitCounts, gitDoorDots, gitDoorTitle, gitPullCount, gitPushCount, gitSyncWords } from "./git-dock-lib";

function repo(over: Partial<RailGitRepo> = {}): RailGitRepo {
  return {
    root: "/r",
    branch: "feature",
    has_remote: true,
    default_branch: "main",
    ahead: 1,
    behind: 2,
    commits: [],
    ...over,
  };
}

describe("gitChipDot: red > yellow > green", () => {
  test("nothing to say: no dot", () => {
    expect(gitChipDot({ checking: false, checked: null, behind: 0 })).toBeNull();
    expect(gitChipDot({ checking: false, checked: true, behind: null })).toBeNull();
  });
  test("behind the remote: green", () => {
    expect(gitChipDot({ checking: false, checked: true, behind: 3 })).toBe("unread");
    expect(gitChipDot({ checking: false, checked: null, behind: 1 })).toBe("unread");
  });
  test("a check out: yellow, over green", () => {
    expect(gitChipDot({ checking: true, checked: null, behind: 3 })).toBe("running");
    expect(gitChipDot({ checking: true, checked: true, behind: 0 })).toBe("running");
  });
  test("the last check failed: red, over yellow and green", () => {
    expect(gitChipDot({ checking: false, checked: false, behind: 0 })).toBe("attention");
    expect(gitChipDot({ checking: false, checked: false, behind: 4 })).toBe("attention");
    expect(gitChipDot({ checking: true, checked: false, behind: 4 })).toBe("attention");
  });
});

describe("gitCounts / gitBranch: the rail's own words", () => {
  test("ahead and behind the default branch", () => {
    expect(gitCounts(repo())).toBe("1 ahead · 2 behind main");
    expect(gitCounts(repo({ ahead: null, behind: null }))).toBe("0 ahead · 0 behind main");
  });
  test("no remote", () => {
    expect(gitCounts(repo({ has_remote: false }))).toBe("No remote");
  });
  test("default branch not known yet", () => {
    expect(gitCounts(repo({ default_branch: null }))).toBe("Default branch not known yet — check to find out");
  });
  test("a detached HEAD", () => {
    expect(gitBranch(repo({ branch: null }))).toBe("detached HEAD");
    expect(gitBranch(repo())).toBe("feature");
  });
});

describe("the Git door's push / pull (owner, 2026-11-10)", () => {
  test("only the default branch behind (upstream even, or none): the green dot and its words agree (CodeRabbit on #2236)", () => {
    for (const upstream of [{ ahead: 0, behind: 0 }, null]) {
      const r = repo({ branch: "main", ahead: 0, behind: 3, upstream });
      expect(gitDoorDots({ checking: false, checked: null, repo: r }).top).toBe("unread");
      expect(gitSyncWords(r)).toEqual(["3 to pull"]);
    }
  });
  test("push: the upstream's ahead, else an unpublished non-default branch's ahead of default", () => {
    expect(gitPushCount(repo({ upstream: { ahead: 2, behind: 0 } }))).toBe(2);
    expect(gitPushCount(repo({ upstream: { ahead: 0, behind: 0 }, ahead: 5 }))).toBe(0);
    expect(gitPushCount(repo({ upstream: null, ahead: 3 }))).toBe(3);
    expect(gitPushCount(repo({ ahead: 3 }))).toBe(3);
    expect(gitPushCount(repo({ upstream: null, branch: "main", ahead: 3 }))).toBe(0);
    expect(gitPushCount(repo({ upstream: null, branch: null, ahead: 3 }))).toBe(0);
  });
  test("pull: the upstream's behind or the default branch's", () => {
    expect(gitPullCount(repo({ upstream: { ahead: 0, behind: 4 }, behind: 0 }))).toBe(4);
    expect(gitPullCount(repo({ upstream: null, behind: 2 }))).toBe(2);
    expect(gitPullCount(repo({ upstream: { ahead: 0, behind: 0 }, behind: 0 }))).toBe(0);
  });
  test("top dot: red > yellow > green > blue; second dot blue only when the top is not push", () => {
    const quiet = repo({ ahead: 0, behind: 0, upstream: { ahead: 0, behind: 0 } });
    expect(gitDoorDots({ checking: false, checked: null, repo: quiet })).toEqual({ top: null, second: null });
    const push = repo({ ahead: 0, behind: 0, upstream: { ahead: 1, behind: 0 } });
    expect(gitDoorDots({ checking: false, checked: true, repo: push })).toEqual({ top: "push", second: null });
    expect(gitDoorDots({ checking: true, checked: true, repo: push })).toEqual({ top: "running", second: "push" });
    expect(gitDoorDots({ checking: false, checked: false, repo: push })).toEqual({ top: "attention", second: "push" });
    const both = repo({ ahead: 0, behind: 0, upstream: { ahead: 1, behind: 2 } });
    expect(gitDoorDots({ checking: false, checked: null, repo: both })).toEqual({ top: "unread", second: "push" });
    const pull = repo({ ahead: 0, behind: 3, upstream: null, branch: "main" });
    expect(gitDoorDots({ checking: false, checked: null, repo: pull })).toEqual({ top: "unread", second: null });
  });
  test("the words and the tooltip", () => {
    expect(gitSyncWords(repo({ upstream: { ahead: 2, behind: 1 }, behind: 0 }))).toEqual(["2 to push", "1 to pull"]);
    expect(gitSyncWords(repo({ upstream: null, ahead: 3, behind: 0 }))).toEqual(["3 not pushed yet"]);
    expect(gitSyncWords(repo({ upstream: { ahead: 0, behind: 0 }, behind: 0 }))).toEqual([]);
    expect(gitDoorTitle(repo({ upstream: { ahead: 2, behind: 0 } }))).toBe("Git — on feature · 1 ahead · 2 behind main · 2 to push · 2 to pull");
    expect(gitDoorTitle(repo({ has_remote: false, ahead: null, behind: null }))).toBe("Git — on feature · No remote");
  });
});
