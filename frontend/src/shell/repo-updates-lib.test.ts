// Row shaping and the branch-dependent action choice for repo-update rows —
// the pure half of RepoUpdatesDock.tsx.
import { describe, expect, it } from "bun:test";
import {
  repoActionLabel,
  repoFixPrompt,
  repoName,
  repoRows,
  repoStatusText,
  repoUpdatesSummary,
  visibleRepoRows,
  type RepoStatus,
} from "./repo-updates-lib";

const status = (over: Partial<RepoStatus> = {}): RepoStatus => ({
  root: "/Users/me/Work/widget",
  branch: "main",
  default_branch: "main",
  on_default: true,
  ahead: 0,
  behind: 3,
  checked_at: 1000,
  ...over,
});

describe("repoName", () => {
  it("takes the last path segment", () => {
    expect(repoName("/Users/me/Work/widget")).toBe("widget");
  });

  it("strips a trailing slash before taking the segment", () => {
    expect(repoName("/Users/me/Work/widget/")).toBe("widget");
  });

  it("normalizes backslashes", () => {
    expect(repoName("C:\\Users\\me\\widget")).toBe("widget");
  });

  it("is the whole string when there is no separator", () => {
    expect(repoName("widget")).toBe("widget");
  });
});

describe("repoRows", () => {
  it("builds one row per repo, in the order the server gave them", () => {
    const rows = repoRows([
      status({ root: "/a/one" }),
      status({ root: "/a/two", on_default: false }),
    ]);
    expect(rows.map((r) => r.name)).toEqual(["one", "two"]);
  });

  it("is empty for an empty or missing list", () => {
    expect(repoRows([])).toEqual([]);
    expect(repoRows(undefined)).toEqual([]);
  });

  it("picks update as the primary action on the default branch", () => {
    const [row] = repoRows([status({ on_default: true, branch: "main" })]);
    expect(row.primaryAction).toBe("update");
  });

  it("picks switch as the primary action off the default branch", () => {
    const [row] = repoRows([status({ on_default: false, branch: "feature" })]);
    expect(row.primaryAction).toBe("switch");
  });

  it("decides the action from branch shape alone, never from the behind count", () => {
    // A feature branch one commit behind is still a feature branch — the
    // action must not flip to update just because the count is small.
    const [row] = repoRows([status({ on_default: false, behind: 1 })]);
    expect(row.primaryAction).toBe("switch");
  });

  it("demotes rebase to secondary off the default branch", () => {
    const [row] = repoRows([status({ on_default: false, branch: "feature" })]);
    expect(row.secondaryAction).toBe("rebase");
  });

  it("has no secondary action on the default branch", () => {
    const [row] = repoRows([status({ on_default: true })]);
    expect(row.secondaryAction).toBeNull();
  });
});

describe("repoStatusText", () => {
  it("names the default branch and the count on the default branch", () => {
    const [row] = repoRows([status({ on_default: true, default_branch: "main", behind: 3 })]);
    expect(repoStatusText(row)).toBe("origin/main is 3 commits ahead");
  });

  it("singularizes one commit", () => {
    const [row] = repoRows([status({ on_default: true, behind: 1 })]);
    expect(repoStatusText(row)).toBe("origin/main is 1 commit ahead");
  });

  it("names the current branch too when off the default branch", () => {
    const [row] = repoRows([
      status({ on_default: false, branch: "feature", default_branch: "main", behind: 2 }),
    ]);
    expect(repoStatusText(row)).toBe("origin/main is 2 commits ahead of feature");
  });

  it("falls back to a generic label for a detached HEAD", () => {
    const [row] = repoRows([status({ on_default: false, branch: null })]);
    expect(repoStatusText(row)).toContain("ahead of this branch");
  });
});

describe("repoActionLabel", () => {
  it("labels each action", () => {
    expect(repoActionLabel("update")).toBe("Update");
    expect(repoActionLabel("rebase")).toBe("Rebase");
  });

  it("names the repo's actual default branch for switch, never a literal main", () => {
    expect(repoActionLabel("switch", "trunk")).toBe("Switch to trunk");
  });
});

describe("visibleRepoRows", () => {
  it("shows every row when nothing is dismissed", () => {
    const rows = repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]);
    expect(visibleRepoRows(rows, {})).toEqual(rows);
  });

  it("hides a row dismissed at or after its own checked_at", () => {
    const rows = repoRows([status({ root: "/a/one", checked_at: 1000 })]);
    expect(visibleRepoRows(rows, { "/a/one": 1000 })).toEqual([]);
    expect(visibleRepoRows(rows, { "/a/one": 1500 })).toEqual([]);
  });

  it("shows a row again once checked_at has advanced past its dismissal", () => {
    // The server re-checked (CHECK_TTL_S elapsed) and produced a NEWER
    // checked_at than the dismissal recorded — the throttle window this
    // row was dismissed for has passed, so it returns.
    const rows = repoRows([status({ root: "/a/one", checked_at: 2000 })]);
    expect(visibleRepoRows(rows, { "/a/one": 1000 })).toEqual(rows);
  });

  it("only affects the dismissed repo's own row", () => {
    const rows = repoRows([
      status({ root: "/a/one", checked_at: 1000 }),
      status({ root: "/a/two", checked_at: 1000 }),
    ]);
    const visible = visibleRepoRows(rows, { "/a/one": 1000 });
    expect(visible.map((r) => r.repo.root)).toEqual(["/a/two"]);
  });
});

describe("repoUpdatesSummary", () => {
  it("singularizes one update", () => {
    const rows = repoRows([status()]);
    expect(repoUpdatesSummary(rows)).toBe("1 update available");
  });

  it("pluralizes more than one", () => {
    const rows = repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]);
    expect(repoUpdatesSummary(rows)).toBe("2 updates available");
  });
});

describe("repoFixPrompt", () => {
  it("carries the error, the branch, real ahead/behind and the repo root", () => {
    const [row] = repoRows([
      status({ root: "/a/widget", branch: "main", default_branch: "main", ahead: 1, behind: 3 }),
    ]);
    const prompt = repoFixPrompt(row, "not possible to fast-forward");
    expect(prompt).toContain("not possible to fast-forward");
    expect(prompt).toContain("branch main");
    expect(prompt).toContain("tracking origin/main");
    expect(prompt).toContain("1 ahead / 3 behind");
    expect(prompt).toContain("/a/widget");
  });

  it("names a detached HEAD instead of a branch", () => {
    const [row] = repoRows([status({ branch: null })]);
    expect(repoFixPrompt(row, "err")).toContain("branch (detached)");
  });

  it("reports the working tree as dirty for a dirty refusal", () => {
    const [row] = repoRows([status()]);
    expect(repoFixPrompt(row, "err", "dirty")).toContain("working tree dirty");
  });

  it("never claims a clean tree for a rebase-conflict-shaped git-failed refusal", () => {
    // The rebase path exists because the tree may already be conflicted by
    // the time this prompt is built — asserting "clean" here would be
    // handing Claude a false fact in exactly the case most likely to have
    // caused the refusal.
    const [row] = repoRows([status()]);
    const prompt = repoFixPrompt(row, "err", "git-failed");
    expect(prompt).not.toContain("working tree clean");
    expect(prompt).toContain("working tree state unknown");
  });

  it("names an in-progress operation rather than calling it dirty", () => {
    const [row] = repoRows([status()]);
    const prompt = repoFixPrompt(row, "err", "in-progress");
    expect(prompt).toContain("mid-operation");
    expect(prompt).not.toContain("working tree dirty");
  });

  it("makes no working-tree claim at all for a reason that never got that far", () => {
    const [row] = repoRows([status()]);
    for (const reason of ["missing", "mount", "detached", "no-remote", "unknown-repo", undefined]) {
      const prompt = repoFixPrompt(row, "err", reason);
      expect(prompt).not.toContain("working tree");
    }
  });
});
