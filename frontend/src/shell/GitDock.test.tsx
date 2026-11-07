// The Git chip's panel (GitDock; owner, 2026-11-07): the chip going — a path
// change clears the page rail's repo — closes its panel, so coming back to a
// repo never reopens it on its own (Bugbot on #2178).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { RailGitRepo } from "@platform/lib/api";

const { publishCanvasRailPeek, resetCanvasRailPeekFlagForTests } = await import("./claude-rail/rail-peek-flag");
const { publishRailGit, registerRailGit, resetRailGitStoreForTests } = await import("./claude-rail/rail-git-store");
const { default: GitDock } = await import("./GitDock");

const repo: RailGitRepo = {
  root: "/r",
  branch: "feature",
  has_remote: true,
  default_branch: "main",
  ahead: 0,
  behind: 0,
  commits: [],
};

afterEach(() => {
  resetRailGitStoreForTests();
  resetCanvasRailPeekFlagForTests();
});

describe("GitDock", () => {
  test("the chip going closes its panel: back on a repo, the panel stays shut", () => {
    publishCanvasRailPeek({ enabled: true });
    const token = {};
    registerRailGit(token);
    const door = { repo, checking: false, checked: null, check: () => {} };
    act(() => publishRailGit(token, door));
    let r!: ReactTestRenderer;
    act(() => {
      r = create(<GitDock />);
    });
    const panel = () => r.root.findAll((n) => n.props.role === "dialog" && n.props["aria-label"] === "Git").length;
    const chip = r.root.find((n) => typeof n.props.onClick === "function" && n.props.label === "Git");
    act(() => chip.props.onClick());
    expect(panel()).toBe(1);
    act(() => publishRailGit(token, null));
    expect(r.toJSON()).toBeNull();
    act(() => publishRailGit(token, door));
    expect(panel()).toBe(0);
    act(() => r.unmount());
  });
});
