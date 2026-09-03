// The new repo-updates card's own presentational rules (decisions A-D, SPEC
// §36): a sibling notification card, its own fold that takes EVERY row, and
// per-row dismissal that expires once the server re-checks. Rendered through
// `RepoUpdatesCardView` — the pure, props-in half of this card, exactly the
// split `DownloadManagerView` uses for the jobs card and for the same
// reason: no polling, and no persisted `collapsed` state (both live in the
// default-exported `RepoUpdatesDock` this file never mounts) — so most tests
// here render it directly with a fixed row list and no globals at all. The
// exceptions are noted where they happen: the shared
// `location`/`window`/`history` shim, installed once at file load so
// router.ts's real module can be imported (see the comment just below), and
// a per-test `globalThis.fetch` stub in the one test that presses a row's
// own button.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";
import type { Job } from "@platform/lib/jobs";
import { installDomShim } from "@platform/lib/testDomShim";

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
// installs the SHARED `location`/`window`/`history` shim router.ts's own
// module-init code needs (`installDomShim`, platform/lib/testDomShim.ts) and
// then imports the REAL router.ts — a real, unfrozen, behaviorally correct
// module every other file can also import safely alongside this one.
//
// The shim is shared and process-wide, and it is NOT torn back down here.
// An earlier version of this file hand-rolled its own three globals and
// deleted them again right after the import, reasoning that nothing below
// needs them — but "nothing below in THIS file" is not the scope that
// matters. bun runs every file in one process and does not reset globals
// between them, so the delete landed on the four files that import router.ts
// statically (ActivityDock, StatusBar, DownloadManager, appSeed): a static
// import is hoisted above any shim call of their own, so they depend on
// `location` still standing when their module graph evaluates. See
// testDomShim.ts.
installDomShim();

const { RepoUpdatesCardView, RepoUpdatesDockView } = await import("@shell/RepoUpdatesDock");
const { repoRows } = await import("@shell/repo-updates-lib");
import type { RepoRow, RepoStatus } from "@shell/repo-updates-lib";

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

// A FAILED job, for the D586 rows this section now also draws. Only
// `state: "error"` is re-routed here; running/done/cancelled stay in Jobs.
const failedJob = (over: Partial<Job> = {}): Job => ({
  id: "sys:ai-image:boom",
  title: "Pyramid build",
  detail: "",
  model: "",
  kind: "task",
  state: "error",
  done: null,
  total: null,
  total_scope: "phase",
  unit: "",
  message: "GDAL ran out of memory",
  page: "",
  owner: "server",
  cancellable: false,
  cancel_requested: false,
  started_at: 0,
  updated_at: 0,
  finished_at: 0,
  stalled: false,
  waiting_for: "",
  ...over,
});

// A DONE job — the routing D662 broadened past `error` alone. Every terminal
// state reaches this section now (jobs.ts `isTerminal`/`terminalJobs`), not
// only a failure.
const doneJob = (over: Partial<Job> = {}): Job => ({
  ...failedJob(over),
  id: "sys:ai-image:done",
  state: "done",
  message: "",
  detail: "Saved to Downloads/pyramid.png",
  ...over,
});

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


// D673: the chip is a `.dl-toggle sc` button — no `.dl-dot` circle any more.
// These helpers read its tone, its numeral (`.sc-num`, count from 1 for
// Notifications) and its accessible name instead.
function toggleClasses(tree: ReactTestRendererJSON | null): string[] {
  return ((findAll(tree, "dl-toggle")[0]?.props.className as string) ?? "").split(" ");
}

function numeral(tree: ReactTestRendererJSON | null): string | null {
  const nums = findAll(tree, "sc-num");
  return nums.length ? text(nums[0]) : null;
}

function renderInstance(
  props: Partial<Parameters<typeof RepoUpdatesCardView>[0]> = {},
): ReactTestRenderer {
  const rows = props.rows ?? repoRows([status()]);
  return create(
    <RepoUpdatesCardView
      rows={rows}
      dismissed={props.dismissed ?? {}}
      terminal={props.terminal ?? []}
      collapsed={props.collapsed ?? false}
      onToggle={props.onToggle ?? (() => {})}
      onDismiss={props.onDismiss ?? (() => {})}
      onDismissAll={props.onDismissAll ?? (() => {})}
      onDone={props.onDone ?? (() => {})}
      onTerminalPatch={props.onTerminalPatch}
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
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Notifications");
  expect(findAll(tree, "dl-idle")).toHaveLength(0);
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No notifications");
  // D673: idle draws no numeral at all.
  expect(numeral(tree)).toBeNull();
});

test("renders the IDLE chip and panel sentence when every row is dismissed", () => {
  const rows = repoRows([status({ root: "/a/one", checked_at: 1000 })]);
  const tree = renderView({ rows, dismissed: { "/a/one": "main@3" } });
  expect(tree).not.toBeNull();
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Notifications");
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No notifications");
  expect(numeral(tree)).toBeNull();
  expect(toggleClasses(tree)).toContain("is-idle");
});

// D673 (supersedes D588's outlined/filled circle with no digits): the chip's
// numeral now IS the count, from 1 — two rows and twelve rows read
// differently from the bar, unlike the old circle which made them identical.
test("the chip's numeral is the total count, from one — not a mere filled/outlined mark", () => {
  const rows = repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]);
  const tree = renderView({ rows });
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Notifications");
  expect(numeral(tree)).toBe("2");
  expect(toggleClasses(tree)).not.toContain("is-idle");

  const twelveRows = repoRows(
    Array.from({ length: 12 }, (_, i) => status({ root: `/a/${i}` })),
  );
  const twelve = renderView({ rows: twelveRows });
  expect(numeral(twelve)).toBe("12");
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
  // THREE rows, one dismissed, so TWO remain visible: since D604 the footer
  // needs a plurality to render at all, and this test is about WHICH rows
  // Clear passes on — not about the threshold.
  const rows = repoRows([
    status({ root: "/a/one", checked_at: 1000 }),
    status({ root: "/a/two", checked_at: 1000 }),
    status({ root: "/a/three", checked_at: 1000 }),
  ]);
  let seen: unknown = null;
  const tree = renderView({
    rows,
    dismissed: { "/a/one": "main@3" },
    onDismissAll: (visible) => {
      seen = visible;
    },
  });
  const clear = findAll(tree, "dl-clear")[0];
  clear.props.onClick();
  expect((seen as { repo: RepoStatus }[]).map((r) => r.repo.root)).toEqual([
    "/a/two",
    "/a/three",
  ]);
});

// D584 finding 3: the ✕ reports the row's POSITION signature, not its
// `checked_at` — a dismissal has to survive a re-check that moved nothing.
test("the ✕ dismisses only its own row, with that row's own position signature", () => {
  const rows = repoRows([status({ root: "/a/one", branch: "feature", behind: 4 })]);
  let seen: unknown = null;
  const tree = renderView({
    rows,
    onDismiss: (root, signature) => {
      seen = [root, signature];
    },
  });
  const x = findAll(tree, "dl-x")[0];
  x.props.onClick();
  expect(seen).toEqual(["/a/one", "feature@4"]);
});

test("collapsed hides every row — not a class flag, the rows are actually gone", () => {
  // A className check alone (e.g. asserting `.dl-rows` gets `is-folded`)
  // would pass even if every row still rendered underneath it — which is
  // exactly the bug review caught (task 8): the class was applied, nothing
  // was actually hidden. This asserts the OBSERVABLE content instead.
  const rows = repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]);
  const tree = renderView({ rows, collapsed: true });
  expect(findAll(tree, "dl-row")).toHaveLength(0);
  expect(findAll(tree, "dl-rows")).toHaveLength(0); // no empty box left behind either
});

test("expanded shows every row", () => {
  const rows = repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]);
  const tree = renderView({ rows, collapsed: false });
  expect(findAll(tree, "dl-row")).toHaveLength(2);
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

// ------------------------------------- Part A item 2: a jobs Clear (D663)
//
// D663 keeps every terminal job until dismissed, and Activity's own `Clear`
// button was deleted in the same PR (D661) — so once a job's own ✕ has been
// missed, `POST /api/jobs/clear` was reachable by no UI at all. "Until
// dismissed" is only a defensible lifetime if dismissing is possible, so
// this section gets its own bulk clear, scoped to the terminal jobs it
// draws — mirroring the repo Clear's own plurality rule (D604): at exactly
// one row, that row's own ✕ already does the identical thing.
test("a jobs Clear is absent at one terminal job and present at two, separate from the repo Clear", () => {
  const one = renderView({ rows: [], terminal: [doneJob({ id: "a" })] });
  expect(findAll(one, "dl-jobs-clear")).toHaveLength(0);

  const two = renderView({ rows: [], terminal: [doneJob({ id: "a" }), doneJob({ id: "b" })] });
  expect(findAll(two, "dl-jobs-clear")).toHaveLength(1);
  // Distinct from the repo Clear — clearing jobs must never also promise to
  // clear repo rows, or vice versa (the same reasoning the repo-only Clear
  // test above states for the other direction).
  expect(findAll(two, "dl-clear")).toHaveLength(0);
});

test("pressing the jobs Clear calls POST /api/jobs/clear and patches the terminal list to empty", async () => {
  const originalFetch = globalThis.fetch;
  const pendingFetches: Array<(v: Response) => void> = [];
  globalThis.fetch = (() =>
    new Promise<Response>((resolve) => pendingFetches.push(resolve))) as unknown as typeof fetch;

  try {
    let patched: ((jobs: Job[]) => Job[]) | null = null;
    const terminal = [doneJob({ id: "a" }), doneJob({ id: "b" })];
    const renderer = renderInstance({
      rows: [],
      terminal,
      onTerminalPatch: (fn) => {
        patched = fn;
      },
    });

    const before = renderer.toJSON() as ReactTestRendererJSON;
    const clear = findAll(before, "dl-jobs-clear")[0];
    act(() => {
      (clear.props as { onClick: () => void }).onClick();
    });

    await act(async () => {
      pendingFetches.pop()?.({
        ok: true,
        status: 200,
        json: async () => ({ cleared: 2 }),
      } as unknown as Response);
    });

    const fn = patched as unknown as ((jobs: Job[]) => Job[]) | null;
    expect(fn).not.toBeNull();
    // `jobsAfterClear` (jobs.ts) — every row Clear would NOT take, i.e. every
    // still-running job. Every job here is terminal, so the patch empties
    // the list, the same server-confirmed-without-waiting-for-a-poll pattern
    // `JobRow`'s own dismiss uses.
    expect((fn as (jobs: Job[]) => Job[])(terminal)).toEqual([]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// -------------------------------- nothing opens or closes on its own (D673)
//
// "we can make the notifications 'un collapse' when a new one comes" (D562
// follow-up) USED TO force the panel open, and D580 used to force it shut
// again once the list drained — both since deleted (`lib/statusChip.ts`'s
// own header has the full reasoning): a background arrival, of ANY kind
// (repo row, terminal job, pairing), must never pop a floating panel over
// the page the user is looking at, uninvited, and a panel the user pinned
// open must not vanish out from under them just because its list emptied.
// The chip's own numeral is the entire announcement now; the panel opens and
// closes ONLY via the chip's own click (`useStatusChip`'s `toggle`).

// BOTH DOCK HARNESSES PASS `initialCollapsed={false}`: the default is
// COLLAPSED (D595, unconditional since D603), and these tests are about the
// fold and the auto-open/auto-close overrides — not about that default.
// `updateDockInstance` passes it for symmetry with the mount, NOT because a
// re-render would reset anything — a `useState` initializer runs once, so the
// fold survives every update on its own. Left in so the two call sites read
// alike and neither looks like the odd one out.
function renderDockInstance(
  rows: RepoRow[],
  dismissed: Record<string, string> = {},
  terminal: Job[] = [],
): ReactTestRenderer {
  return create(
    <RepoUpdatesDockView
      rows={rows}
      dismissed={dismissed}
      terminal={terminal}
      initialCollapsed={false}
      onDismiss={() => {}}
      onDismissAll={() => {}}
      onDone={() => {}}
    />,
  );
}

/** Deliberately WITHOUT `initialCollapsed` — the only harness here that
 *  exercises the real, unconditional default (D603). */
function renderDockInstanceDefaultFold(rows: RepoRow[]): ReactTestRenderer {
  return create(
    <RepoUpdatesDockView
      rows={rows}
      dismissed={{}}
      onDismiss={() => {}}
      onDismissAll={() => {}}
      onDone={() => {}}
    />,
  );
}

function updateDockInstance(
  renderer: ReactTestRenderer,
  rows: RepoRow[],
  dismissed: Record<string, string> = {},
  terminal: Job[] = [],
) {
  act(() => {
    renderer.update(
      <RepoUpdatesDockView
        rows={rows}
        dismissed={dismissed}
        terminal={terminal}
        initialCollapsed={false}
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

// THE DEFAULT ITSELF (D595, made unconditional in D603 — user: "on page reload
// the models popover auto opens for some reason", which was a stored `"0"`
// being faithfully restored). With no `initialCollapsed` there is nothing to
// consult: a section starts collapsed on every load, full stop.
test("a section always starts collapsed, with nothing persisted to say otherwise", () => {
  const renderer = renderDockInstanceDefaultFold([
    repoRows([status({ root: "/a/one" })])[0],
  ]);
  const tree = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(tree, "dl-toggle")).toHaveLength(1); // the chip is still there
  expect(findAll(tree, "dl-panel")).toHaveLength(0); // ...and nothing is open
});

test("a genuinely new repo row arriving while collapsed does NOT open the panel", () => {
  const one = repoRows([status({ root: "/a/one" })])[0];
  const renderer = renderDockInstance([one]);
  clickDockToggle(renderer); // collapse

  const collapsed = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(collapsed, "dl-row")).toHaveLength(0);
  expect(findAll(collapsed, "dl-panel")).toHaveLength(0);

  const two = repoRows([status({ root: "/a/two" })])[0];
  updateDockInstance(renderer, [one, two]);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(0);
  // The arrival is still ANNOUNCED — just by the chip's own numeral, not a
  // panel thrown open uninvited.
  expect(numeral(after)).toBe("2");
});

test("the chip's own click is what opens the panel — a collapsed one only opens on click", () => {
  const one = repoRows([status({ root: "/a/one" })])[0];
  const two = repoRows([status({ root: "/a/two" })])[0];
  const renderer = renderDockInstance([one]);
  clickDockToggle(renderer); // collapse
  updateDockInstance(renderer, [one, two]); // an arrival — still shut
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-panel")).toHaveLength(0);

  clickDockToggle(renderer); // the user's own click opens it

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(1);
  expect(findAll(after, "dl-row")).toHaveLength(2);
});

test("collapsing, then an EXISTING row merely changing (behind count ticking), opens nothing", () => {
  const one = repoRows([status({ root: "/a/one", behind: 1 })])[0];
  const renderer = renderDockInstance([one]);
  clickDockToggle(renderer); // collapse

  const changed = repoRows([status({ root: "/a/one", behind: 5 })])[0];
  updateDockInstance(renderer, [changed]);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(0);
});

test("a dismissed row that goes FURTHER behind still opens nothing on its own", () => {
  const first = repoRows([status({ root: "/a/one", branch: "main", behind: 3 })])[0];
  const renderer = renderDockInstance([first]);
  clickDockToggle(renderer); // collapse

  // Dismiss it — visible drops to zero even though `rows` still holds it.
  updateDockInstance(renderer, [first], { "/a/one": "main@3" });
  const dismissed = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(dismissed, "dl-row")).toHaveLength(0);

  // Upstream actually MOVED (behind 3 -> 9), so the dismissal's signature no
  // longer covers this row and it comes back — but no arrival, genuine or
  // not, opens this panel any more (D673).
  const again = repoRows([status({ root: "/a/one", branch: "main", behind: 9 })])[0];
  updateDockInstance(renderer, [again], { "/a/one": "main@3" });

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(0);
  expect(numeral(after)).toBe("1");
});

// D584 finding 3 at the dock level: the throttled re-check that moved nothing
// must not resurrect a dismissed row — a dismissed repo reappearing every
// CHECK_TTL_S, forever, on a branch that is permanently behind, was the
// user-visible bug.
test("a re-check that moved NOTHING leaves a dismissed row dismissed", () => {
  const row = repoRows([status({ root: "/a/one", branch: "main", behind: 3 })])[0];
  const renderer = renderDockInstance([row]);
  clickDockToggle(renderer); // collapse
  updateDockInstance(renderer, [row], { "/a/one": "main@3" });
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-row")).toHaveLength(0);

  // Same position, new timestamp — exactly what `check_repo` produces every
  // CHECK_TTL_S whether or not anything happened.
  const rechecked = repoRows([
    status({ root: "/a/one", branch: "main", behind: 3, checked_at: 999_999 }),
  ])[0];
  updateDockInstance(renderer, [rechecked], { "/a/one": "main@3" });

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-row")).toHaveLength(0);
  expect(numeral(after)).toBeNull();
});

// ---------------------------------------------------------------- D586: failures land here
//
// User: "maybe we can have a flow like running activities are shown in jobs and
// after done, a completed message goes to notifications?" — the cheap version,
// with no notification store: `fused_render/jobs.py`'s `_sweep` already keeps
// `error` rows until they are explicitly dismissed, so this is a client-side
// re-route of rows that already exist.

test("a terminal job draws as a row here, with its failure message", () => {
  const tree = renderView({ rows: [], terminal: [failedJob()] });
  expect(tree).not.toBeNull();
  const rows = findAll(tree, "dl-row");
  expect(rows).toHaveLength(1);
  expect(text(rows[0])).toContain("Pyramid build");
  expect(text(rows[0])).toContain("GDAL ran out of memory");
});

// The numeral answers "is there anything here" across BOTH sources — the
// combined count. Each source alone must fill it (count = visible repo rows +
// terminal + pairings), or one of them would be invisible from the bar.
test("either source fills the numeral, and neither alone leaves it empty", () => {
  const repoOnly = renderView({ rows: repoRows([status()]), terminal: [] });
  expect(numeral(repoOnly)).toBe("1");

  const failureOnly = renderView({ rows: [], terminal: [failedJob()] });
  expect(numeral(failureOnly)).toBe("1");

  const both = renderView({ rows: repoRows([status()]), terminal: [failedJob()] });
  expect(numeral(both)).toBe("2");
});

test("failures alone still make the section non-idle", () => {
  const tree = renderView({ rows: [], terminal: [failedJob()] });
  expect(findAll(tree, "dl-panel-empty")).toHaveLength(0);
  expect(numeral(tree)).toBe("1");
  expect(toggleClasses(tree)).not.toContain("is-idle");
});

test("both sources empty is what draws the one empty sentence", () => {
  const tree = renderView({ rows: [], terminal: [] });
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No notifications");
  expect(numeral(tree)).toBeNull();
  expect(toggleClasses(tree)).toContain("is-idle");
});

test("a failure colours the chip — the tint moved here from Jobs (D586)", () => {
  const withFailure = renderView({ rows: [], terminal: [failedJob()] });
  expect(
    (findAll(withFailure, "dl-toggle")[0].props.className as string).split(" "),
  ).toContain("is-failure");

  const repoOnly = renderView({ rows: repoRows([status()]), terminal: [] });
  expect((findAll(repoOnly, "dl-toggle")[0].props.className as string).split(" ")).not.toContain(
    "is-failure",
  );
});

test("a done job draws a visible, dismissable row here too (C1)", () => {
  // D662 routes every terminal state, not only `error`, to this section.
  // `JobRow` used to return null for `state: "done"` — a leftover from when
  // only failures ever reached this component — which left a done job
  // filling this chip's numeral and its panel's total while drawing nothing:
  // no row, no ✕, unclearable once D663 stopped sweeping it.
  const tree = renderView({ rows: [], terminal: [doneJob()] });
  expect(numeral(tree)).toBe("1");
  expect(findAll(tree, "dl-row").length).toBeGreaterThan(0);
  expect(findAll(tree, "dl-x")).toHaveLength(1);
  expect(text(tree)).toContain("Saved to Downloads/pyramid.png");
});

test("Clear is offered for repo rows only — a failure is dismissed by its own row", () => {
  // Two dismissal models, deliberately NOT unified (D586): a repo dismissal is
  // client-side and expires when the repo moves (D585 finding 3), while a
  // failure's dismissal is server-side and permanent. One Clear cannot honestly
  // promise both.
  const failuresOnly = renderView({ rows: [], terminal: [failedJob()] });
  expect(findAll(failuresOnly, "dl-clear")).toHaveLength(0);
  // The row still carries its own dismiss control.
  expect(findAll(failuresOnly, "dl-x")).toHaveLength(1);

  // TWO repo rows, because a single one no longer earns the footer (D604) —
  // the point here is that failures do not count toward it either way.
  const withRepos = renderView({
    rows: repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]),
    terminal: [failedJob()],
  });
  expect(findAll(withRepos, "dl-clear")).toHaveLength(1);

  // ...and a plurality made up of one repo row plus one failure does NOT earn
  // it: Clear only ever acts on repo rows, so only those may be counted.
  const oneEach = renderView({ rows: repoRows([status()]), terminal: [failedJob()] });
  expect(findAll(oneEach, "dl-clear")).toHaveLength(0);
});

// D604, THE BOUNDARY, asserted in both directions: the whole band — hairline
// included — is absent at one row and present at two. One row cost 32px of an
// 88px card for a button its own ✕ already duplicates.
test("the footer is absent at one repo row and present at two", () => {
  const one = renderView({ rows: repoRows([status({ root: "/a/one" })]) });
  expect(findAll(one, "dl-head")).toHaveLength(0);
  expect(findAll(one, "dl-clear")).toHaveLength(0);
  // The row's own dismiss is what covers the single case.
  expect(findAll(one, "dl-x")).toHaveLength(1);

  const two = renderView({
    rows: repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]),
  });
  expect(findAll(two, "dl-head")).toHaveLength(1);
  expect(findAll(two, "dl-clear")).toHaveLength(1);
});

test("repo rows come before failures — the actionable rows first", () => {
  // Both row kinds share `.dl-row` now (status-bar merge, brief item 4), so
  // ordering is asserted by what each kind carries rather than by class name:
  // a repo row's own action button is `.q-all` (kept — see this row's own
  // header comment for why it did not migrate to `.dl-row-cancel`), which a
  // terminal-job row (`JobRow`) never renders.
  const tree = renderView({ rows: repoRows([status({ root: "/a/one" })]), terminal: [failedJob()] });
  const rows = findAll(tree, "dl-row");
  expect(rows).toHaveLength(2);
  expect(findAll(rows[0], "q-all")).toHaveLength(1);
  expect(findAll(rows[1], "q-all")).toHaveLength(0);
});

// D673 (supersedes D574/D586's "repo arrivals auto-open, failures are
// announce-only" split): NEITHER kind of arrival opens the panel any more —
// a background build failing and a repo falling behind are both announced by
// the chip's own numeral/tint alone.
test("a failure arriving opens nothing — only fills the numeral and tints the chip", () => {
  const renderer = renderDockInstance([]);
  clickDockToggle(renderer); // collapse
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-panel")).toHaveLength(0);

  act(() => {
    renderer.update(
      <RepoUpdatesDockView
        rows={[]}
        dismissed={{}}
        terminal={[failedJob()]}
        onDismiss={() => {}}
        onDismissAll={() => {}}
        onDone={() => {}}
      />,
    );
  });

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(0);
  expect(numeral(after)).toBe("1");
  expect(toggleClasses(after)).toContain("is-failure");
});

test("a repo row arriving opens nothing either — the same rule as a failure now", () => {
  const renderer = renderDockInstance([]);
  clickDockToggle(renderer); // collapse
  updateDockInstance(renderer, repoRows([status({ root: "/a/one" })]));

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(0);
  expect(numeral(after)).toBe("1");
});

// A PINNED panel never auto-closes (D673, supersedes D580): the last repo row
// going does not slam it shut over a failure row the user is reading, and —
// unlike the old rule — that is now true even once EVERYTHING has drained: a
// pinned panel shows the idle sentence rather than disappearing.
test("a pinned panel outlives every row draining, showing the idle sentence at the end", () => {
  const failure = failedJob();
  // `renderDockInstance` mounts with `initialCollapsed={false}` — pinned open.
  const renderer = renderDockInstance(
    repoRows([status({ root: "/a/one" })]),
    {},
    [failure],
  );
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-panel")).toHaveLength(1);

  // The repo row is gone (Updated, or dismissed) — the failure is not.
  updateDockInstance(renderer, [], {}, [failure]);
  const midDrain = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(midDrain, "dl-panel")).toHaveLength(1);
  expect(findAll(midDrain, "dl-row")).toHaveLength(1);

  // And now the failure is dismissed too — genuinely nothing left.
  updateDockInstance(renderer, [], {}, []);
  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(1); // still open — no auto-close
  expect(text(findAll(after, "dl-panel-empty")[0])).toBe("No notifications");
});
