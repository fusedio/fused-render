// The Git card's body (GitPanelBody; owner, 2026-11-10 — the status bar
// chip's panel, moved onto the rail's Git door): the branch and its tags,
// Check for updates, the warn line, and the commits — each the Git panel's
// door where the page has one, plain rows where not, a note where none.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import type { RailGitRepo } from "@platform/lib/api";
import type { RailGitDoor } from "./claude-rail/rail-git-store";

const { GitPanelBody } = await import("./GitPanelBody");

const repo = (over: Partial<RailGitRepo> = {}): RailGitRepo => ({
  root: "/r",
  branch: "feature",
  has_remote: true,
  default_branch: "main",
  ahead: 1,
  behind: 2,
  upstream: { ahead: 3, behind: 4 },
  commits: [
    { sha: "a1", short: "a1", subject: "First", author: "me", when: 1 },
    { sha: "b2", short: "b2", subject: "Second", author: "me", when: 2 },
  ],
  ...over,
});

function mount(git: Partial<RailGitDoor>, onTake?: () => void): ReactTestRenderer {
  const door: RailGitDoor = { repo: repo(), checking: false, checked: null, check: () => {}, ...git };
  let r!: ReactTestRenderer;
  act(() => {
    r = create(<GitPanelBody git={door} {...(onTake ? { onTake } : {})} />);
  });
  return r;
}
const byClass = (r: ReactTestRenderer, c: string): ReactTestInstance[] =>
  r.root.findAll((n) => typeof n.type === "string" && String(n.props.className ?? "").split(" ").includes(c));
const text = (n: ReactTestInstance): string =>
  n.children.map((c) => (typeof c === "string" ? c : text(c))).join("");

describe("GitPanelBody", () => {
  test("the branch, ↑/↓ against the default branch, then to push / to pull", () => {
    const r = mount({});
    expect(text(byClass(r, "git-panel-branch-name")[0]!)).toBe("feature");
    expect(byClass(r, "git-panel-tag").map((t) => t.props["aria-label"])).toEqual([
      "1 ahead of main",
      "2 behind main",
      "3 to push",
      "4 to pull",
    ]);
    // Nothing to say: no tags at all.
    const quiet = mount({ repo: repo({ ahead: 0, behind: 0, upstream: { ahead: 0, behind: 0 } }) });
    expect(byClass(quiet, "git-panel-tag")).toHaveLength(0);
    // On the default branch itself: only the upstream pair (the same facts).
    const onMain = mount({ repo: repo({ branch: "main", ahead: 1, behind: 1, upstream: { ahead: 1, behind: 1 } }) });
    expect(byClass(onMain, "git-panel-tag").map((t) => t.props["aria-label"])).toEqual(["1 to push", "1 to pull"]);
    // Elsewhere the vs-default pair's hint says so.
    expect(byClass(r, "git-panel-tag")[0]!.props["data-hint"]).toBe("1 ahead, vs main");
    // An unpublished branch: its commits ahead of the default branch are the push.
    const fresh = mount({ repo: repo({ ahead: 2, behind: 0, upstream: null }) });
    expect(byClass(fresh, "is-push")[0]!.props["aria-label"]).toBe("2 not pushed yet");
  });

  test("Check for updates: runs the check; disabled with no remote or while one is out", () => {
    let checks = 0;
    const r = mount({ check: () => checks++ });
    const btn = () => byClass(r, "git-panel-check")[0]!;
    expect(btn().props.disabled).toBe(false);
    act(() => btn().props.onClick());
    expect(checks).toBe(1);
    expect(byClass(mount({ repo: repo({ has_remote: false }) }), "git-panel-check")[0]!.props.disabled).toBe(true);
    const busy = mount({ checking: true });
    expect(byClass(busy, "git-panel-check")[0]!.props.disabled).toBe(true);
    expect(byClass(busy, "git-panel-check")[0]!.props["aria-label"]).toBe("Checking for updates…");
  });

  test("the warn line only after a check that did not answer, and not while another is out", () => {
    expect(byClass(mount({ checked: false }), "git-panel-warn")).toHaveLength(1);
    expect(byClass(mount({ checked: false, checking: true }), "git-panel-warn")).toHaveLength(0);
    expect(byClass(mount({ checked: true }), "git-panel-warn")).toHaveLength(0);
  });

  test("commits: the Git panel's door where there is one, plain rows where not, a note where none", () => {
    let took = 0;
    const r = mount({}, () => took++);
    const rows = r.root.findAll((n) => n.type === "button" && n.props.className === "git-commit");
    expect(rows).toHaveLength(2);
    act(() => rows[1]!.props.onClick());
    expect(took).toBe(1);
    const plain = mount({});
    expect(plain.root.findAll((n) => n.type === "button" && n.props.className === "git-commit")).toHaveLength(0);
    expect(plain.root.findAll((n) => n.type === "div" && n.props.className === "git-commit")).toHaveLength(2);
    const none = mount({ repo: repo({ commits: [] }) });
    expect(byClass(none, "git-panel-commits")).toHaveLength(0);
    expect(byClass(none, "git-panel-empty")).toHaveLength(1);
  });
});
