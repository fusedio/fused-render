// The new repo-updates card's own presentational rules (decisions A-D, SPEC
// §36): a sibling notification card, its own fold that takes EVERY row, and
// per-row dismissal that expires once the server re-checks. Rendered through
// `RepoUpdatesCardView` — the pure, props-in half of this card, exactly the
// split `DownloadManagerView` uses for the jobs card and for the same
// reason: no polling, and no persisted `collapsed` state (both live in the
// default-exported `RepoUpdatesDock` this file never mounts) — so most tests
// here render it directly with a fixed row list and no globals at all. The
// exceptions are noted where they happen: a `location`/`window`/`history`
// stub, installed and torn down once at file load so router.ts's real
// module can be imported (see the comment just below), and a per-test
// `globalThis.fetch` stub in the one test that presses a row's own button.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";

// NEITHER "@platform/lib/router" NOR "@platform/lib/api" is `mock.module`d
// here — found the hard way, live: an earlier version of this file DID mock
// router.ts (`{navigate: () => {}}`), which broke TWO unrelated files
// (useWalkSearch.render.test.ts, FilesHome.render.test.tsx) the moment all
// three ran in the same `bun test` invocation. `mock.module` replaces a
// specifier for the WHOLE process, not just this file — first-registration
// wins, so a stub written for THIS file's needs quietly became the module
// every OTHER file's import resolved against too. Making the stub "complete"
// (every export name router.ts has) only fixed the crash; FilesHome still
// broke, because its assertions depend on `navigate`'s REAL behavior
// (`history.pushState` + `window.dispatchEvent`), not just its presence.
// FilesHome.render.test.tsx's own header comment already documents this
// exact lesson for `@platform/lib/api` ("a real ES module namespace export
// is frozen... this stubs `globalThis.fetch` instead") — the same principle
// applies to router.ts. So instead of mocking either module, this file
// installs the minimal `location`/`window`/`history` globals router.ts's
// own module-init code touches (mirroring `hook-harness.ts`'s `Clock`) and
// then imports the REAL router.ts — a real, unfrozen, behaviorally correct
// module every other file can also import safely alongside this one.
(globalThis as Record<string, unknown>).location = { pathname: "/x", search: "" };
(globalThis as Record<string, unknown>).window = {
  parent: undefined,
  top: undefined,
  dispatchEvent: () => true,
};
(globalThis as Record<string, unknown>).history = {
  state: null,
  replaceState: () => {},
  pushState: () => {},
};

const { RepoUpdatesCardView, RepoUpdatesDockView } = await import("@shell/RepoUpdatesDock");
const { repoRows } = await import("@shell/repo-updates-lib");
import type { RepoRow, RepoStatus } from "@shell/repo-updates-lib";

// The globals above exist only to get router.ts's module-init code through
// ITS one-time evaluation above (triggered by the dynamic import) — nothing
// in this file's own tests touches `window`/`location`/`history` again, so
// they are torn back down immediately rather than left standing for
// whichever file happens to run next in the same process (mirroring
// `hook-harness.ts` Clock's own install/restore discipline, just inlined
// since this file only ever needs the install once, at load).
delete (globalThis as Record<string, unknown>).location;
delete (globalThis as Record<string, unknown>).window;
delete (globalThis as Record<string, unknown>).history;

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

function renderInstance(
  props: Partial<Parameters<typeof RepoUpdatesCardView>[0]> = {},
): ReactTestRenderer {
  const rows = props.rows ?? repoRows([status()]);
  return create(
    <RepoUpdatesCardView
      rows={rows}
      dismissed={props.dismissed ?? {}}
      collapsed={props.collapsed ?? false}
      hasNew={props.hasNew ?? false}
      onToggle={props.onToggle ?? (() => {})}
      onDismiss={props.onDismiss ?? (() => {})}
      onDismissAll={props.onDismissAll ?? (() => {})}
      onDone={props.onDone ?? (() => {})}
    />,
  );
}

function renderView(
  props: Partial<Parameters<typeof RepoUpdatesCardView>[0]> = {},
): ReactTestRendererJSON | null {
  return renderInstance(props).toJSON() as ReactTestRendererJSON | null;
}

// D573 (user: "lets have simpler stuff like models (x count) | notifications
// | downloads etc and the no xyz part in the popover thing that opens", then
// "the chevron doesn't belong to the status bar. lets follow vscode/cursor
// for inspiration"): the chip is now ALWAYS a real, clickable button — idle
// included, VS Code/Cursor style — and the idle sentence moved into the
// panel it opens.
test("renders a real, clickable chip when there are no rows — the idle sentence lives in its panel (D565/D573)", () => {
  const tree = renderView({ rows: [] });
  expect(tree).not.toBeNull();
  const toggles = findAll(tree, "dl-toggle");
  expect(toggles).toHaveLength(1);
  expect(toggles[0].type).toBe("button");
  expect((toggles[0].props.className as string).split(" ")).toContain("is-idle");
  // D579: `Updates` -> `Notifications` (user: "git updates does not make
  // sense out of an app. it belongs to 'notifications'").
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Notifications0"); // label + always-rendered zero (D583)
  expect(findAll(tree, "dl-idle")).toHaveLength(0);
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No notifications");
  // D583: the count is ALWAYS rendered, zero included (VS Code's own
  // errors/warnings item reads `0` rather than going blank).
  expect(findAll(tree, "dl-count")).toHaveLength(1);
  expect(text(findAll(tree, "dl-count")[0])).toBe("0");
});

test("renders the IDLE chip and panel sentence when every row is dismissed", () => {
  const rows = repoRows([status({ root: "/a/one", checked_at: 1000 })]);
  const tree = renderView({ rows, dismissed: { "/a/one": 1000 } });
  expect(tree).not.toBeNull();
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Notifications0"); // label + always-rendered zero (D583)
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No notifications");
  expect(text(findAll(tree, "dl-count")[0])).toBe("0");
});

test("the chip names the category, and the count lives in its own reserved slot", () => {
  const rows = repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]);
  const tree = renderView({ rows });
  // Asserted as label + slot rather than one joined string (D581): the count
  // is a separate element now, and the space between them is CSS padding on
  // the slot, so a joined assertion would read `Notifications2` and pin the
  // absence of a space that is not the markup's job.
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Notifications2");
  expect(text(findAll(tree, "dl-count")[0])).toBe("2");
});

test("a row on the default branch offers Update as the only button, plus dismiss", () => {
  const rows = repoRows([status({ on_default: true })]);
  const tree = renderView({ rows });
  const buttons = findAll(tree, "q-all").map((n) => text(n));
  expect(buttons).toEqual(["Update"]);
  expect(findAll(tree, "dl-x")).toHaveLength(1);
});

test("a row off the default branch offers Switch as the only button, plus dismiss", () => {
  const rows = repoRows([status({ on_default: false, branch: "feature", default_branch: "main" })]);
  const tree = renderView({ rows });
  const buttons = findAll(tree, "q-all").map((n) => text(n));
  expect(buttons).toEqual(["Switch to main"]);
  expect(findAll(tree, "dl-x")).toHaveLength(1);
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

test("pressing a row's action shows Working… on that row's own button, mid-flight", async () => {
  // task 12's regression (code review, 2026-08-27) needed TWO buttons on one
  // row to reproduce: a shared `busy` boolean covered both, with only the
  // primary swapping its label, so pressing the secondary (a Rebase button,
  // since removed as too dangerous to offer — D555 amendment) made the
  // primary read "Working…" for an action the user never pressed. A row
  // offers exactly one button now, so that exact two-button mix-up is no
  // longer reachable — this keeps only what's still true: the pressed
  // button reads "Working…" while its own request is in flight, tracked by
  // WHICH action is running rather than a plain boolean.
  //
  // `fetch` (not `@platform/lib/api`) is what gets stubbed, and only for
  // this one test — see the file header comment on why a shared module mock
  // is the wrong tool here. Restored in `finally` so a later test in this
  // same file (or process) never sees the stub.
  const originalFetch = globalThis.fetch;
  const pendingFetches: Array<(v: Response) => void> = [];
  globalThis.fetch = (() =>
    new Promise<Response>((resolve) => pendingFetches.push(resolve))) as unknown as typeof fetch;

  try {
    const rows = repoRows([status({ on_default: false, branch: "feature", default_branch: "main" })]);
    const renderer = renderInstance({ rows });

    const before = renderer.toJSON() as ReactTestRendererJSON;
    const switchBtn = findAll(before, "q-all").find((n) => text(n) === "Switch to main");
    expect(switchBtn).toBeDefined();

    // `run`'s `setBusyAction(action)` happens synchronously before its first
    // `await`, so a plain (non-async) act() flushes it — the fetch itself
    // stays pending, which is exactly the mid-flight state under test.
    act(() => {
      (switchBtn as ReactTestRendererJSON).props.onClick();
    });

    const mid = renderer.toJSON() as ReactTestRendererJSON;
    const buttons = findAll(mid, "q-all").map((n) => text(n));
    expect(buttons).toEqual(["Working…"]);

    // Settle the pending fetch with a real Response-shaped object (postJson
    // calls `res.json()` then reads `res.ok`) so the test doesn't leak an
    // unresolved promise / dangling act() warning — awaited so the `finally`
    // block's `setBusyAction(null)`, a microtask chain after this resolve,
    // is flushed inside act() rather than after it.
    await act(async () => {
      pendingFetches.pop()?.({
        ok: true,
        status: 200,
        json: async () => ({ ok: true, op: "switch", root: rows[0].repo.root }),
      } as unknown as Response);
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ------------------------------------- a quiet dot on a new arrival (D567)
//
// "we can make the notifications 'un collapse' when a new one comes" (D562
// follow-up) USED TO force the panel open — code review finding #4 caught
// that this recreates the complaint the whole status-bar redesign exists to
// fix (a background arrival popping a floating panel over the page,
// uninvited, and persisting the expansion). `useAutoExpandOnNew` no longer
// touches `collapsed` (its own doc has the reasoning); these pin
// `RepoUpdatesDockView` — the stateful half that owns collapse for this
// card — wiring the DOT in instead. Assertions are on the rendered rows
// (`q-row`) themselves, never a class name alone: a className-only check is
// exactly how an earlier fold bug on this same card shipped green while the
// rows kept rendering underneath it (see the "collapsed hides every row"
// test above).

function renderDockInstance(
  rows: RepoRow[],
  dismissed: Record<string, number> = {},
): ReactTestRenderer {
  return create(
    <RepoUpdatesDockView
      rows={rows}
      dismissed={dismissed}
      onDismiss={() => {}}
      onDismissAll={() => {}}
      onDone={() => {}}
    />,
  );
}

function updateDockInstance(
  renderer: ReactTestRenderer,
  rows: RepoRow[],
  dismissed: Record<string, number> = {},
) {
  act(() => {
    renderer.update(
      <RepoUpdatesDockView
        rows={rows}
        dismissed={dismissed}
        onDismiss={() => {}}
        onDismissAll={() => {}}
        onDone={() => {}}
      />,
    );
  });
}

function clickDockToggle(renderer: ReactTestRenderer) {
  const before = renderer.toJSON() as ReactTestRendererJSON;
  const toggle = findAll(before, "dl-toggle")[0];
  act(() => {
    (toggle.props as { onClick: () => void }).onClick();
  });
}

// D574 REVERSES D567 (user: "when we have something new, always show the
// notification. don't keep no activity displayed") — a new row arriving into
// a collapsed section OPENS that section's panel, and the dot is suppressed
// while it is open, because a dot pointing at a panel the user is already
// looking at announces nothing.
test("a genuinely new repo row arriving OPENS the collapsed panel, and shows no dot beside it", () => {
  const one = repoRows([status({ root: "/a/one" })])[0];
  const renderer = renderDockInstance([one]);
  clickDockToggle(renderer); // collapse

  const collapsed = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(collapsed, "q-row")).toHaveLength(0);
  expect(findAll(collapsed, "dl-new-dot")).toHaveLength(0);

  const two = repoRows([status({ root: "/a/two" })])[0];
  updateDockInstance(renderer, [one, two]);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "q-row")).toHaveLength(2);
  expect(findAll(after, "dl-new-dot")).toHaveLength(0);
});

test("the chip's own click dismisses an auto-opened panel, leaving no dot behind", () => {
  const one = repoRows([status({ root: "/a/one" })])[0];
  const renderer = renderDockInstance([one]);
  clickDockToggle(renderer); // collapse
  const two = repoRows([status({ root: "/a/two" })])[0];
  updateDockInstance(renderer, [one, two]);
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "q-row")).toHaveLength(2);

  clickDockToggle(renderer); // dismiss the auto-opened panel

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "q-row")).toHaveLength(0);
  expect(findAll(after, "dl-new-dot")).toHaveLength(0);
});

test("collapsing, then an EXISTING row merely changing (behind count ticking), sets no dot", () => {
  const one = repoRows([status({ root: "/a/one", behind: 1 })])[0];
  const renderer = renderDockInstance([one]);
  clickDockToggle(renderer); // collapse

  const changed = repoRows([status({ root: "/a/one", behind: 5 })])[0];
  updateDockInstance(renderer, [changed]);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "q-row")).toHaveLength(0);
  expect(findAll(after, "dl-new-dot")).toHaveLength(0);
});

test("a dismissed row that reappears later (a fresh checked_at) counts as new again", () => {
  const first = repoRows([status({ root: "/a/one", checked_at: 1000 })])[0];
  const renderer = renderDockInstance([first]);
  clickDockToggle(renderer); // collapse

  // Dismiss it — visible drops to zero even though `rows` still holds it.
  // The card is idle now (no rows), so there is no toggle/dot to inspect —
  // only that it stays idle.
  updateDockInstance(renderer, [first], { "/a/one": 1000 });
  const dismissed = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(dismissed, "q-row")).toHaveLength(0);

  // Server re-checks and it's behind again: a newer checked_at makes it
  // visible again (repo-updates-lib.ts `visibleRepoRows`), and since it had
  // fallen out of the seen set on dismissal this is a genuine re-arrival —
  // so it auto-opens the panel exactly like any other new row (D574).
  const again = repoRows([status({ root: "/a/one", checked_at: 2000 })])[0];
  updateDockInstance(renderer, [again], { "/a/one": 1000 });

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "q-row")).toHaveLength(1);
  expect(findAll(after, "dl-new-dot")).toHaveLength(0);
});
