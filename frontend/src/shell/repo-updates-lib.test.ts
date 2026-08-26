// Row shaping and the branch-dependent action choice for repo-update rows —
// the pure half of RepoUpdatesDock.tsx.
import { describe, expect, it } from "bun:test";
import {
  repoActionLabel,
  repoFixPrompt,
  repoName,
  repoRows,
  repoStatusText,
  type RepoStatus,
} from "./repo-updates-lib";

const status = (over: Partial<RepoStatus> = {}): RepoStatus => ({
  root: "/Users/me/Work/widget",
  branch: "main",
  default_branch: "main",
  on_default: true,
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

  it("picks rebase as the primary action off the default branch", () => {
    const [row] = repoRows([status({ on_default: false, branch: "feature" })]);
    expect(row.primaryAction).toBe("rebase");
  });

  it("decides the action from branch shape alone, never from the behind count", () => {
    // A feature branch one commit behind is still a feature branch — the
    // action must not flip to update just because the count is small.
    const [row] = repoRows([status({ on_default: false, behind: 1 })]);
    expect(row.primaryAction).toBe("rebase");
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
});

describe("repoFixPrompt", () => {
  it("carries the error, the branch, ahead/behind and the repo root", () => {
    const [row] = repoRows([
      status({ root: "/a/widget", branch: "main", default_branch: "main", behind: 3 }),
    ]);
    const prompt = repoFixPrompt(row, "not possible to fast-forward");
    expect(prompt).toContain("not possible to fast-forward");
    expect(prompt).toContain("branch main");
    expect(prompt).toContain("tracking origin/main");
    expect(prompt).toContain("3 behind");
    expect(prompt).toContain("/a/widget");
  });

  it("names a detached HEAD instead of a branch", () => {
    const [row] = repoRows([status({ branch: null })]);
    expect(repoFixPrompt(row, "err")).toContain("branch (detached)");
  });

  it("reports the working tree as dirty only for a dirty refusal", () => {
    const [row] = repoRows([status()]);
    expect(repoFixPrompt(row, "err", "dirty")).toContain("working tree dirty");
    expect(repoFixPrompt(row, "err", "git-failed")).toContain("working tree clean");
  });
});
