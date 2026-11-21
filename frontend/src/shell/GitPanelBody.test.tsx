// The Git card's body (GitPanelBody; owner, 2026-11-10 — the status bar
// chip's panel, moved onto the rail's Git door): the branch and its tags,
// Check for updates, the Git view's sync button, the warn line, and the commits — each the Git panel's
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
  test("the branch and ↑/↓ against the default branch — information, never the push / pull", () => {
    const r = mount({});
    expect(text(byClass(r, "git-panel-branch-name")[0]!)).toBe("feature");
    expect(byClass(r, "git-panel-tag").map((t) => t.props["aria-label"])).toEqual(["1 ahead of main", "2 behind main"]);
    expect(byClass(r, "git-panel-tag")[0]!.props["data-hint"]).toBe("1 ahead, vs main");
    // Nothing to say: no tags at all.
    const quiet = mount({ repo: repo({ ahead: 0, behind: 0, upstream: { ahead: 0, behind: 0 } }) });
    expect(byClass(quiet, "git-panel-tag")).toHaveLength(0);
    // On the default branch itself the pair is the button's facts: no tags.
    const onMain = mount({ repo: repo({ branch: "main", ahead: 1, behind: 1, upstream: { ahead: 1, behind: 1 } }) });
    expect(byClass(onMain, "git-panel-tag")).toHaveLength(0);
  });

  test("the pane's sync button: Get latest, else Send, else Publish branch — one at a time, with its count", () => {
    const ops: string[] = [];
    const sync = (op: string) => ops.push(op);
    const label = (r: ReactTestRenderer) => {
      const b = byClass(r, "git-panel-sync")[0];
      return b ? text(b) : null;
    };
    // Behind first (a push cannot land over unpulled history).
    const both = mount({ sync });
    expect(label(both)).toBe("Get latest4");
    act(() => byClass(both, "git-panel-sync")[0]!.props.onClick());
    expect(ops).toEqual(["pull"]);
    const ahead = mount({ sync, repo: repo({ upstream: { ahead: 2, behind: 0 } }) });
    expect(label(ahead)).toBe("Send2");
    expect(byClass(ahead, "git-panel-sync")[0]!.props["aria-label"]).toBe("Send 2 commits");
    act(() => byClass(ahead, "git-panel-sync")[0]!.props.onClick());
    expect(ops).toEqual(["pull", "push"]);
    // A branch the remote has never seen: Publish branch, no count.
    expect(label(mount({ sync, repo: repo({ upstream: null }) }))).toBe("Publish branch");
    // Even: no button, the fetch icon stands alone.
    expect(label(mount({ sync, repo: repo({ upstream: { ahead: 0, behind: 0 } }) }))).toBeNull();
    // No remote, detached, or a door with no sync: none.
    expect(label(mount({ sync, repo: repo({ has_remote: false }) }))).toBeNull();
    expect(label(mount({ sync, repo: repo({ branch: null }) }))).toBeNull();
    expect(label(mount({}))).toBeNull();
    // A copy of someone else's app: the pane routes its push / pull.
    expect(label(mount({ sync, repo: repo({ guarded: true }) }))).toBeNull();
  });

  test("a sync in flight: spinner on the button, both buttons disabled; its refusal is the warn line", () => {
    let calls = 0;
    const busy = mount({ sync: () => calls++, syncing: "pull" });
    // aria-disabled, not disabled: the pressed button keeps focus; a press does nothing.
    expect(byClass(busy, "git-panel-sync")[0]!.props["aria-disabled"]).toBe(true);
    expect(byClass(busy, "git-panel-sync")[0]!.props["aria-busy"]).toBe(true);
    act(() => byClass(busy, "git-panel-sync")[0]!.props.onClick());
    expect(calls).toBe(0);
    const live = () => busy.root.findAll((n) => n.props.role === "status" && n.props.className === "sr-only");
    expect(text(live()[0]!)).toBe("Getting the latest…");
    const done = mount({ sync: () => {}, syncDone: "Pushed to origin/main." });
    expect(text(done.root.find((n) => n.props.role === "status" && n.props.className === "sr-only"))).toBe("Pushed to origin/main.");
    expect(byClass(busy, "git-panel-check")[0]!.props.disabled).toBe(true);
    const checking = mount({ sync: () => {}, checking: true });
    expect(byClass(checking, "git-panel-sync")[0]!.props["aria-disabled"]).toBe(true);
    const refused = mount({ sync: () => {}, syncNote: "Your branch and its upstream have diverged" });
    expect(byClass(refused, "git-panel-warn").map(text)).toEqual(["Your branch and its upstream have diverged"]);
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
