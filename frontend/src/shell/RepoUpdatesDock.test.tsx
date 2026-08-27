// The new repo-updates card's own presentational rules (decisions A-D, SPEC
// §36): a sibling notification card, its own fold that takes EVERY row, and
// per-row dismissal that expires once the server re-checks. Rendered through
// `RepoUpdatesCardView` — the pure, props-in half of this card, exactly the
// split `DownloadManagerView` uses for the jobs card and for the same
// reason: no polling, no network, no `window`/`document`, so this file can
// render it directly with a fixed row list.
import { expect, mock, test } from "bun:test";
import { create, type ReactTestRendererJSON } from "react-test-renderer";

// router.ts touches `location` at module init (rewriteLegacyPath) — dead in a
// DOM-less bun test. Mocked before the component import, the same way
// useListingSelection.render.test.ts does it for the same module; none of
// this file's tests exercise `navigate` (that only fires from a row's own
// "Fix with Claude" click, never triggered here).
mock.module("@platform/lib/router", () => ({
  navigate: () => {},
}));

const { RepoUpdatesCardView } = await import("@shell/RepoUpdatesDock");
const { repoRows } = await import("@shell/repo-updates-lib");
import type { RepoStatus } from "@shell/repo-updates-lib";

function findAll(node: ReactTestRendererJSON | null, className: string): ReactTestRendererJSON[] {
  if (node === null || typeof node === "string") return [];
  const hits: ReactTestRendererJSON[] = [];
  if (typeof node.props?.className === "string" && node.props.className.split(" ").includes(className)) {
    hits.push(node);
  }
  for (const child of node.children ?? []) {
    if (typeof child !== "string") hits.push(...findAll(child, className));
  }
  return hits;
}

function text(node: ReactTestRendererJSON | null): string {
  if (node === null) return "";
  if (typeof node === "string") return node;
  return (node.children ?? []).map((c) => text(c as ReactTestRendererJSON)).join("");
}

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

function renderView(props: Partial<Parameters<typeof RepoUpdatesCardView>[0]> = {}) {
  const rows = props.rows ?? repoRows([status()]);
  return create(
    <RepoUpdatesCardView
      rows={rows}
      dismissed={props.dismissed ?? {}}
      collapsed={props.collapsed ?? false}
      onToggle={props.onToggle ?? (() => {})}
      onDismiss={props.onDismiss ?? (() => {})}
      onDismissAll={props.onDismissAll ?? (() => {})}
      onDone={props.onDone ?? (() => {})}
    />,
  ).toJSON() as ReactTestRendererJSON | null;
}

test("renders no card at all when there are no rows", () => {
  expect(renderView({ rows: [] })).toBeNull();
});

test("renders no card at all when every row is dismissed", () => {
  const rows = repoRows([status({ root: "/a/one", checked_at: 1000 })]);
  const tree = renderView({ rows, dismissed: { "/a/one": 1000 } });
  expect(tree).toBeNull();
});

test("the header names how many updates are visible", () => {
  const rows = repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]);
  const tree = renderView({ rows });
  expect(text(findAll(tree, "dl-summary")[0])).toBe("2 updates available");
});

test("a row on the default branch offers Update as the only button, plus dismiss", () => {
  const rows = repoRows([status({ on_default: true })]);
  const tree = renderView({ rows });
  const buttons = findAll(tree, "q-all").map((n) => text(n));
  expect(buttons).toEqual(["Update"]);
  expect(findAll(tree, "dl-x")).toHaveLength(1);
});

test("a row off the default branch offers Switch primary and Rebase secondary", () => {
  const rows = repoRows([status({ on_default: false, branch: "feature", default_branch: "main" })]);
  const tree = renderView({ rows });
  const buttons = findAll(tree, "q-all").map((n) => text(n));
  expect(buttons).toEqual(["Switch to main", "Rebase"]);
});

test("Clear calls onDismissAll with exactly the visible rows", () => {
  const rows = repoRows([
    status({ root: "/a/one", checked_at: 1000 }),
    status({ root: "/a/two", checked_at: 1000 }),
  ]);
  let seen: unknown = null;
  const tree = renderView({
    rows,
    dismissed: { "/a/one": 1000 },
    onDismissAll: (visible) => {
      seen = visible;
    },
  });
  const clear = findAll(tree, "dl-clear")[0];
  clear.props.onClick();
  expect((seen as { repo: RepoStatus }[]).map((r) => r.repo.root)).toEqual(["/a/two"]);
});

test("the ✕ dismisses only its own row, with that row's own checked_at", () => {
  const rows = repoRows([status({ root: "/a/one", checked_at: 1234 })]);
  let seen: unknown = null;
  const tree = renderView({
    rows,
    onDismiss: (root, checkedAt) => {
      seen = [root, checkedAt];
    },
  });
  const x = findAll(tree, "dl-x")[0];
  x.props.onClick();
  expect(seen).toEqual(["/a/one", 1234]);
});

test("collapsed hides every row — not a class flag, the rows are actually gone", () => {
  // A className check alone (e.g. asserting `.dl-rows` gets `is-folded`)
  // would pass even if every row still rendered underneath it — which is
  // exactly the bug review caught (task 8): the class was applied, nothing
  // was actually hidden. This asserts the OBSERVABLE content instead.
  const rows = repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]);
  const tree = renderView({ rows, collapsed: true });
  expect(findAll(tree, "q-row")).toHaveLength(0);
  expect(findAll(tree, "dl-rows")).toHaveLength(0); // no empty box left behind either
});

test("expanded shows every row", () => {
  const rows = repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]);
  const tree = renderView({ rows, collapsed: false });
  expect(findAll(tree, "q-row")).toHaveLength(2);
});
