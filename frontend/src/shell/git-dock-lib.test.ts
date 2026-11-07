// The Git chip's dot (owner, 2026-11-07: "the green dot, yellow dot and red
// dot we have for git should still pop up no matter what") and its words.
import { describe, expect, test } from "bun:test";
import type { RailGitRepo } from "@platform/lib/api";
import { gitBranch, gitChipDot, gitCounts } from "./git-dock-lib";

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
