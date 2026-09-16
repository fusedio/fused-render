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
import { expect, mock, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";
import type { Job } from "@platform/lib/jobs";

// NEITHER "@platform/lib/router" NOR "@platform/lib/api" is `mock.module`d
// here — found the hard way, live: an earlier version of this file DID mock
// router.ts (`{navigate: () => {}}`), which broke TWO unrelated files
// (useListingSearch.render.test.ts, FilesHome.render.test.tsx) the moment all
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

// A rowClick's `onClick` calls `navigateUrl`, which touches `history` and
// `window` — both deleted right after the import below, the same as
// `location` (see the block comment above). Most tests never press a
// rowClick, so they never need these back; the few that do restore them only
// for the press itself, via this helper, so nothing here leaks between tests.
function withNav<T>(run: (pushed: string[]) => T): T {
  const pushed: string[] = [];
  const realHistory = (globalThis as Record<string, unknown>).history;
  const realWindow = (globalThis as Record<string, unknown>).window;
  (globalThis as Record<string, unknown>).history = {
    state: null,
    replaceState: () => {},
    pushState: (_s: unknown, _t: string, u: string) => pushed.push(u),
  };
  (globalThis as Record<string, unknown>).window = { dispatchEvent: () => true };
  try {
    return run(pushed);
  } finally {
    (globalThis as Record<string, unknown>).history = realHistory;
    (globalThis as Record<string, unknown>).window = realWindow;
  }
}

const { RepoUpdatesCardView, RepoUpdatesDockView } = await import("@shell/RepoUpdatesDock");
const { repoRows } = await import("@shell/repo-updates-lib");
import type { RepoRow, RepoStatus } from "@shell/repo-updates-lib";
import type { AttentionRow } from "@shell/tasks-lib";
// notifications.ts (and its router.ts import) is already evaluated by the
// dynamic import above — RepoUpdatesDock.tsx imports it — so this second
// `await import` just reads the cached module; it does NOT re-run
// router.ts's module-init `location` read, and is safe after the
// `location`/`window`/`history` globals above are deleted. Used only by the
// "messages" tests below, to drive the real store the way `MessageRowView`'s
// dismiss button does (it calls `dismissNotification` directly, not through
// a prop — see RepoUpdatesDock.tsx's own header comment on that row kind).
const { notify, getRetainedNotifications, _resetNotificationsForTest } = await import(
  "@platform/lib/notifications"
);
import type { StoredNotification } from "@platform/lib/notifications";

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

// A FAILED job, for the D586 rows this section now also draws. Only
// `state: "error"` is re-routed here; running/done/cancelled stay in Jobs.
//
// `group` defaults to THIS job's own id (via the local `id`, not
// `over.id` alone) so two bare calls to `failedJob()`/`doneJob()` — which
// have DIFFERENT default ids — never accidentally land in the same
// `(page, group)` group and get folded into one `GroupJobRow` by §3's
// grouping. `doneJob` below takes care to thread its OWN default id into
// this function for the same reason, rather than letting this function's
// own default id leak through.
const failedJob = (over: Partial<Job> = {}): Job => {
  const id = over.id ?? "sys:ai-image:boom";
  return {
    id,
    title: "Pyramid build",
    detail: "",
    model: "",
    kind: "task",
    state: "error",
    done: null,
    total: null,
    total_scope: "phase",
    total_estimated: false,
    unit: "",
    message: "GDAL ran out of memory",
    page: "",
    source: "",
    origin: "",
    owner: "server",
    cancellable: false,
    cancel_requested: false,
    started_at: 0,
    updated_at: 0,
    finished_at: 0,
    stalled: false,
    waiting_for: "",
    tier: "trail",
    group: id,
    ...over,
  };
};

// A DONE job — the routing D662 broadened past `error` alone. Every terminal
// state reaches this section now (jobs.ts `isTerminal`/`terminalJobs`), not
// only a failure.
const doneJob = (over: Partial<Job> = {}): Job => ({
  ...failedJob({ ...over, id: over.id ?? "sys:ai-image:done" }),
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
      pairings={props.pairings ?? []}
      attention={props.attention ?? []}
      attentionDismissed={props.attentionDismissed ?? {}}
      onAttentionDismiss={props.onAttentionDismiss}
      messages={props.messages ?? []}
      recent={props.recent ?? []}
      collapsed={props.collapsed ?? false}
      onToggle={props.onToggle ?? (() => {})}
      onDismiss={props.onDismiss ?? (() => {})}
      onDismissAll={props.onDismissAll ?? (() => {})}
      onDone={props.onDone ?? (() => {})}
      onTerminalPatch={props.onTerminalPatch}
      onRecentPatch={props.onRecentPatch}
      onPairingGone={props.onPairingGone}
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

// ------------------------------------- One "Clear all"
//
// D663 keeps every terminal job until dismissed, and Activity's own `Clear`
// button was deleted in the same PR (D661) — so once a job's own ✕ has been
// missed, `POST /api/jobs/clear` needs a reachable UI. One "Clear all" button
// covers both repo rows and terminal jobs, present once their COMBINED count
// passes one — mirroring D604's plurality rule (at exactly one row of either
// kind, that row's own ✕ already does the identical thing) — scored across
// both kinds together rather than per kind.
test("Clear all is absent at one terminal job and present at two", () => {
  const one = renderView({ rows: [], terminal: [doneJob({ id: "a" })] });
  expect(findAll(one, "dl-clear")).toHaveLength(0);

  const two = renderView({ rows: [], terminal: [doneJob({ id: "a" }), doneJob({ id: "b" })] });
  expect(findAll(two, "dl-clear")).toHaveLength(1);
});

test("Clear all is present for one repo row plus one failure — the combined count, not either alone", () => {
  const oneEach = renderView({ rows: repoRows([status()]), terminal: [failedJob()] });
  expect(findAll(oneEach, "dl-clear")).toHaveLength(1);
});

test("pressing Clear all dismisses the visible repo rows and clears the terminal jobs together", async () => {
  const originalFetch = globalThis.fetch;
  const pendingFetches: Array<(v: Response) => void> = [];
  globalThis.fetch = (() =>
    new Promise<Response>((resolve) => pendingFetches.push(resolve))) as unknown as typeof fetch;

  try {
    let patched: ((jobs: Job[]) => Job[]) | null = null;
    let dismissedAll: unknown = null;
    const terminal = [doneJob({ id: "a" }), doneJob({ id: "b" })];
    const rows = repoRows([status({ root: "/a/one" })]);
    const renderer = renderInstance({
      rows,
      terminal,
      onDismissAll: (visible) => {
        dismissedAll = visible;
      },
      onTerminalPatch: (fn) => {
        patched = fn;
      },
    });

    const before = renderer.toJSON() as ReactTestRendererJSON;
    const clear = findAll(before, "dl-clear")[0];
    act(() => {
      (clear.props as { onClick: () => void }).onClick();
    });

    // The repo dismissal fires synchronously, with no request behind it.
    expect((dismissedAll as { repo: RepoStatus }[]).map((r) => r.repo.root)).toEqual(["/a/one"]);

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

// ------------------------------------------------- the volume cap
//
// Only TERMINAL jobs ever fold — a repo row, a pairing and a waiting task
// are always drawn in full below, uncounted by the cap, because none of
// them pile up the way a machine that has finished many jobs does.
test("five or fewer terminal jobs draw with no fold row at all", () => {
  const terminal = Array.from({ length: 5 }, (_, i) => doneJob({ id: `j${i}` }));
  const tree = renderView({ rows: [], terminal });
  expect(findAll(tree, "dl-row").length).toBe(5);
  expect(findAll(tree, "dl-panel-more")).toHaveLength(0);
});

test("the fold keeps the NEWEST five, not the oldest — `terminal` arrives oldest-first", () => {
  // j0 is the oldest job, j6 the newest (jobs.py's `list_jobs` order). The
  // visible five must be j2..j6, in that same oldest-first reading order —
  // j0 and j1 are what the fold hides.
  const terminal = Array.from({ length: 7 }, (_, i) =>
    doneJob({ id: `j${i}`, detail: `job ${i}` })
  );
  const tree = renderView({ rows: [], terminal });
  const rows = findAll(tree, "dl-row");
  expect(rows.map((r) => text(r))).toEqual([
    expect.stringContaining("job 2"),
    expect.stringContaining("job 3"),
    expect.stringContaining("job 4"),
    expect.stringContaining("job 5"),
    expect.stringContaining("job 6"),
  ]);
});

test("a 6th terminal job folds behind an 'N older notifications' row — nothing is dropped", () => {
  const terminal = Array.from({ length: 7 }, (_, i) => doneJob({ id: `j${i}` }));
  const tree = renderView({ rows: [], terminal });
  expect(findAll(tree, "dl-row").length).toBe(5);
  const more = findAll(tree, "dl-panel-more");
  expect(more).toHaveLength(1);
  expect(text(more[0])).toBe("2 older notifications");
  // Nothing was deleted — the chip's own count still reads every one of them.
  expect(numeral(tree)).toBe("7");
});

test("the fold row sits above `.dl-rows`, not inside it — `.dl-rows` scrolls the terminal rows alone", () => {
  const terminal = Array.from({ length: 7 }, (_, i) => doneJob({ id: `j${i}` }));
  const tree = renderView({ rows: [], terminal });
  const dlRows = findAll(tree, "dl-rows")[0];
  expect(findAll(dlRows, "dl-panel-more")).toHaveLength(0);
  expect(findAll(dlRows, "dl-row")).toHaveLength(5);
});

test("clicking the fold row reveals every job", () => {
  const terminal = Array.from({ length: 7 }, (_, i) => doneJob({ id: `j${i}` }));
  const renderer = renderInstance({ rows: [], terminal });
  const before = renderer.toJSON() as ReactTestRendererJSON;
  const more = findAll(before, "dl-panel-more")[0];
  act(() => {
    (more.props as { onClick: () => void }).onClick();
  });
  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-row").length).toBe(7);
  expect(findAll(after, "dl-panel-more")).toHaveLength(0);
});

test("repo rows, pairings and a waiting task are never folded, however many terminal jobs there are", () => {
  const terminal = Array.from({ length: 8 }, (_, i) => doneJob({ id: `j${i}` }));
  const rows = repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]);
  const tree = renderView({
    rows,
    terminal,
    pairings: [{ id: "p1", name: "Suryas iPhone", at: 1000 }],
    attention: [asking()],
  });
  // 2 repo rows + 1 pairing + 1 attention row + 5 shown terminal jobs.
  expect(findAll(tree, "dl-row").length).toBe(9);
  expect(text(findAll(tree, "dl-panel-more")[0])).toBe("3 older notifications");
});

// Finding 4 (code review 2026-09-16): TERMINAL_VISIBLE_CAP used to slice the
// flat `terminalTrail` job list. A multi-member group's members are still
// contiguous in that flat list (groupJobs' own ordering keeps them
// together), but a cap boundary landing INSIDE that run split the group in
// half — the folded-away members were simply gone from the row `groupJobs`
// re-derives from the sliced list, so the row's own "N of M done" undercounted
// and its dismiss-all only ever reached the members that survived the slice.
// The cap must bound ROWS (one per group, however many members), not jobs.
// Finding 7 (code review 2026-09-16): `GroupJobRow` drew no `rowClick` at
// all, so once a family had 2+ members and folded into one row (§3), that
// row lost its destination outright — every other row kind (`JobRow`,
// pairings, waiting tasks) is clickable, this one silently was not.
test("finding 7: a folded group row is clickable and opens the oldest member's page", () => {
  withNav((pushed) => {
    const older = doneJob({
      id: "g-a",
      group: "burst",
      page: "/ai-models/local",
      started_at: 0,
      finished_at: 100,
    });
    const newer = doneJob({
      id: "g-b",
      group: "burst",
      page: "/ai-models/local",
      started_at: 200,
      finished_at: 300,
    });
    const tree = renderView({ rows: [], terminal: [older, newer] });
    const rows = findAll(tree, "dl-row");
    expect(rows).toHaveLength(1);
    expect(findAll(tree, "dl-row-open")).toHaveLength(1);
    act(() => {
      (rows[0].props as { onClick: () => void }).onClick();
    });
    expect(pushed).toContain("/ai-models/local");
  });
});

test("finding 4: a group straddling the cap boundary renders as one complete row, never a partial one", () => {
  const single = doneJob({ id: "solo", group: "solo", started_at: 0, finished_at: 100 });
  const burst = Array.from({ length: 6 }, (_, i) =>
    doneJob({
      id: `burst${i}`,
      group: "burst",
      page: "/x",
      started_at: 1000 + i * 1000,
      finished_at: 1000 + i * 1000 + 100,
    }),
  );
  const terminal = [single, ...burst];
  const tree = renderView({ rows: [], terminal });
  // Only 2 ROWS exist (the solo job, and the one burst group) — well under
  // the cap of 5 rows — so nothing should fold at all, and the burst group's
  // row must report every one of its 6 members, not 5.
  expect(findAll(tree, "dl-panel-more")).toHaveLength(0);
  const rows = findAll(tree, "dl-row");
  expect(rows).toHaveLength(2);
  const secondary = findAll(tree, "dl-model").map((n) => text(n));
  expect(secondary).toContain("6 of 6 done");
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

test("Clear all is absent at exactly one failure — the row's own ✕ already does it", () => {
  const failuresOnly = renderView({ rows: [], terminal: [failedJob()] });
  expect(findAll(failuresOnly, "dl-clear")).toHaveLength(0);
  // The row still carries its own dismiss control.
  expect(findAll(failuresOnly, "dl-x")).toHaveLength(1);

  // TWO repo rows plus a failure: well past the plurality threshold.
  const withRepos = renderView({
    rows: repoRows([status({ root: "/a/one" }), status({ root: "/a/two" })]),
    terminal: [failedJob()],
  });
  expect(findAll(withRepos, "dl-clear")).toHaveLength(1);
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

test("a failure comes before an ordinary repo row — Needs you precedes Worth keeping (item 3)", () => {
  // Both row kinds share `.dl-row` now (status-bar merge, brief item 4), so
  // ordering is asserted by what each kind carries rather than by class name:
  // a repo row's own action button is `.q-all` (kept — see this row's own
  // header comment for why it did not migrate to `.dl-row-cancel`), which a
  // terminal-job row (`JobRow`) never renders. `failedJob()`'s `state: "error"`
  // makes its `effectiveTier` "attention" regardless of its declared tier, so
  // it lands in "Needs you" — the section item 3 draws FIRST — ahead of the
  // ordinary repo row in "Worth keeping", reversing what used to be true when
  // every terminal job shared one flat list with the repo rows.
  const tree = renderView({ rows: repoRows([status({ root: "/a/one" })]), terminal: [failedJob()] });
  const rows = findAll(tree, "dl-row");
  expect(rows).toHaveLength(2);
  expect(findAll(rows[0], "q-all")).toHaveLength(0);
  expect(findAll(rows[1], "q-all")).toHaveLength(1);
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

// ------------------------------------------- 2026-09-03: the waiting-task row
//
// Akshil: "in bottom right we have notifications. When the task was blocked I
// did not see any notifications in there … there should be notifications with
// blocked tasks as well." The row's CONTENT and destination are decided in
// `tasks-lib.attentionRows` and tested there; what is asserted here is what the
// panel does with them.

const asking = (over: Partial<AttentionRow> = {}): AttentionRow => ({
  key: "sess-7",
  taskId: "TASK-097",
  title: "Pull today's news",
  href: "/explorer/view/Users/me/proj?_side=claude&session_id=sess-7",
  origin: "",
  ...over,
});

test("a waiting task draws a row that names the task and says what it wants", () => {
  const tree = renderView({ rows: [], attention: [asking()] });
  const rows = findAll(tree, "dl-row");
  expect(rows).toHaveLength(1);
  expect(text(rows[0])).toContain("T097 needs your input");
  expect(text(rows[0])).toContain("Pull today's news");
});

test("the whole row is a click target, reachable by keyboard, and it also has a dismiss", () => {
  // One action besides dismissing, so the row itself is the control rather
  // than a small target inside a large one — `NotificationCard`'s `rowClick`
  // draws it as `role="button"` (not a real <button>, since the row also
  // nests a real <button> for the ✕, and a button cannot nest in a button).
  const tree = renderView({ rows: [], attention: [asking()] });
  const row = findAll(tree, "dl-row")[0];
  expect(row.type).toBe("div");
  expect(row.props.role).toBe("button");
  expect(row.props.tabIndex).toBe(0);
  expect(findAll(tree, "dl-row-open")).toHaveLength(1);
  // The ✕ dismisses the ROW, not the question — the task stays exactly as
  // parked either way, and the sidebar's Tasks dot is unaffected.
  expect(findAll(tree, "dl-x")).toHaveLength(1);
});

test("dismissing a waiting-task row calls the attention-dismiss callback with its key and signature", () => {
  const onAttentionDismiss = mock(() => {});
  const tree = renderView({ rows: [], attention: [asking()], onAttentionDismiss });
  const x = findAll(tree, "dl-x")[0];
  x.props.onClick();
  expect(onAttentionDismiss).toHaveBeenCalledWith("sess-7", "Pull today's news");
});

test("a dismissed waiting-task row disappears, and does not count toward the numeral", () => {
  const tree = renderView({
    rows: [],
    attention: [asking()],
    attentionDismissed: { "sess-7": "Pull today's news" },
  });
  expect(findAll(tree, "dl-row")).toHaveLength(0);
  expect(numeral(tree)).toBe(null);
  expect(toggleClasses(tree)).toContain("is-idle");
});

test("a dismissed waiting-task row comes back once the question changes", () => {
  const tree = renderView({
    rows: [],
    attention: [asking({ title: "A brand new question" })],
    attentionDismissed: { "sess-7": "Pull today's news" },
  });
  expect(findAll(tree, "dl-row")).toHaveLength(1);
});

test("clicking a pairing row opens LAN preferences and clears the row", () => {
  withNav((pushed) => {
    const onPairingGone = mock(() => {});
    const tree = renderInstance({
      rows: [],
      pairings: [{ id: "p1", name: "Suryas iPhone", at: 1000 }],
      onPairingGone,
    });
    const row = findAll(tree.toJSON() as ReactTestRendererJSON, "dl-row")[0];
    expect(findAll(tree.toJSON() as ReactTestRendererJSON, "dl-row-open")).toHaveLength(1);
    act(() => {
      (row.props as { onClick: () => void }).onClick();
    });
    expect(pushed).toContain("/preferences?tab=lan");
    expect(onPairingGone).toHaveBeenCalledWith("p1");
  });
});

test("a task naming no folder still opens as a row — its door is /tasks itself", () => {
  // `attentionRows` falls back to "/tasks" when a task names no folder at all
  // (tasks-lib.ts) — every row here is clickable now, so there is no more
  // inert case to draw around.
  withNav((pushed) => {
    const tree = renderView({ rows: [], attention: [asking({ href: "/tasks" })] });
    const row = findAll(tree, "dl-row")[0];
    expect(findAll(tree, "dl-row-open")).toHaveLength(1);
    act(() => {
      (row.props as { onClick: () => void }).onClick();
    });
    expect(pushed).toContain("/tasks");
  });
});

test("waiting tasks fill the numeral like every other source, and end the idle state", () => {
  // EVERY SOURCE DECIDES EVERY DERIVED NUMBER: the count, the idle predicate and
  // the empty sentence all read one total, so a new row kind that forgot to join
  // it would be invisible from the bar.
  const alone = renderView({ rows: [], attention: [asking()] });
  expect(numeral(alone)).toBe("1");
  expect(findAll(alone, "dl-panel-empty")).toHaveLength(0);
  expect(toggleClasses(alone)).not.toContain("is-idle");

  const withOthers = renderView({
    rows: repoRows([status()]),
    terminal: [failedJob()],
    attention: [asking()],
  });
  expect(numeral(withOthers)).toBe("3");
});

test("a waiting task tints the chip red, like a failure does", () => {
  // The first cut left the pill neutral on the argument that the sidebar's dot
  // was already red; the user did not see it (Akshil, 2026-09-03: "not
  // prominent enough"). This corner is where a reader looks for what wants
  // them, so the two surfaces now agree.
  const tree = renderView({ rows: [], attention: [asking()] });
  expect(toggleClasses(tree)).toContain("is-failure");
  const quiet = renderView({ rows: repoRows([status()]) });
  expect(toggleClasses(quiet)).not.toContain("is-failure");
});

test("the waiting row goes above every other kind", () => {
  // It is the only row here whose subject has not finished happening: a repo is
  // behind, a device paired, a job ended — all still true in ten minutes. A
  // parked run is a person being waited on.
  const tree = renderView({
    rows: repoRows([status()]),
    terminal: [failedJob()],
    attention: [asking()],
  });
  const rows = findAll(tree, "dl-row");
  expect(text(rows[0])).toContain("T097 needs your input");
});

test("Clear never counts a waiting row — there is nothing there to clear", () => {
  // The footer's two buttons act on repo rows and on finished jobs, and D604
  // asks for a PLURALITY of the kind a button actually clears. One repo row plus
  // two waiting tasks is still one repo row.
  const tree = renderView({
    rows: repoRows([status()]),
    attention: [asking(), asking({ key: "sess-8", taskId: "TASK-098" })],
  });
  expect(findAll(tree, "dl-head")).toHaveLength(0);
  expect(findAll(tree, "dl-clear")).toHaveLength(0);
});

// ---------------------------------------------------------- item 3: two sections, one chip

test("rows split into 'Needs you' and 'Worth keeping', each drawn only when non-empty", () => {
  // A waiting task and a failed job both land in "Needs you"; a repo row
  // lands in "Worth keeping" — the two sections never mix. Both headings show
  // here because both sections are actually present at once — the same
  // "2+ sections" rule ActivityDock's own Running/Background split follows.
  const tree = renderView({
    rows: repoRows([status({ root: "/a/one" })]),
    terminal: [failedJob()],
    attention: [asking()],
  });
  const titles = findAll(tree, "dl-section-head").map((n) => text(n));
  expect(titles).toEqual(["Needs you", "Worth keeping"]);
});

test("a lone section draws no heading at all — nothing here needs disambiguating", () => {
  // Same "PLURALITY, NOT PRESENCE" rule this file already follows for the
  // Clear-all footer and ActivityDock follows for its own section headings:
  // with only "Worth keeping" ever populated, a label distinguishing it from
  // an empty sibling is a redundant header.
  const onlyTrail = renderView({ rows: repoRows([status()]) });
  expect(findAll(onlyTrail, "dl-section-head")).toHaveLength(0);
  expect(findAll(onlyTrail, "dl-row")).toHaveLength(1);

  const onlyAttention = renderView({ rows: [], attention: [asking()] });
  expect(findAll(onlyAttention, "dl-section-head")).toHaveLength(0);
  expect(findAll(onlyAttention, "dl-row")).toHaveLength(1);
});

test("an attention-tier terminal job never folds behind the trail cap, however many trail jobs there are", () => {
  // 8 ordinary (done, trail-tier) jobs plus 1 failed (attention-tier) job:
  // TERMINAL_VISIBLE_CAP (5) folds the trail jobs down to 5, with 3 folded —
  // but the failed job is never part of that count at all, because it never
  // reaches `terminalTrail` in the first place.
  const trail = Array.from({ length: 8 }, (_, i) => doneJob({ id: `j${i}` }));
  const tree = renderView({ rows: [], terminal: [...trail, failedJob()] });
  const rows = findAll(tree, "dl-row");
  // 5 shown trail jobs + 1 attention job, never folded.
  expect(rows).toHaveLength(6);
  expect(text(rows[0])).toContain("Pyramid build"); // attention section first
  expect(text(findAll(tree, "dl-panel-more")[0])).toBe("3 older notifications");
});

test("the chip reads 'N needs you' and turns loud the moment anything needs a look", () => {
  const idle = renderView({ rows: repoRows([status()]) });
  expect(text(findAll(idle, "dl-summary")[0])).toBe("Notifications");
  expect(toggleClasses(idle)).not.toContain("is-failure");

  const oneNeedsYou = renderView({ rows: [], terminal: [failedJob()] });
  expect(text(findAll(oneNeedsYou, "dl-summary")[0])).toBe("1 needs you");
  expect(toggleClasses(oneNeedsYou)).toContain("is-failure");

  const twoNeedYou = renderView({
    rows: [],
    terminal: [failedJob()],
    attention: [asking()],
  });
  expect(text(findAll(twoNeedYou, "dl-summary")[0])).toBe("2 needs you");
});

test("'N needs you' counts a waiting task and an attention-tier job together, not just one source", () => {
  const tree = renderView({
    rows: repoRows([status()]), // a repo row must never count toward "needs you"
    terminal: [failedJob(), doneJob()], // one attention-tier, one trail-tier
    attention: [asking()],
  });
  expect(text(findAll(tree, "dl-summary")[0])).toBe("2 needs you");
});

test("a done (trail-tier) job alone never turns the label loud — only attention rows do", () => {
  const tree = renderView({ rows: [], terminal: [doneJob()] });
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Notifications");
  expect(toggleClasses(tree)).not.toContain("is-failure");
});

// ---- messages (SPEC-toasts-become-notifications.md §3) ---------------------
//
// A 5th row source: client-raised notifications retained by
// `@platform/lib/notifications`, split the same way `terminal` already is —
// `attention` into "Needs you", everything else that made it into `messages`
// into "Worth keeping". Retention narrowed (user: "don't keep this in the
// list. just show popup. anything non actionable or error doesn't belong in
// the list") from "attention or trail" to "attention, or carries an
// action/page" — `trail` is no longer even a type a client call site can
// pass (`ClientNotificationTier` in notifications.ts), so a real
// "Worth keeping" message today resolves to `tier: "transient"` while still
// being retained, because it carries an action/page. These tests still build
// mock `StoredNotification`s with `tier: "trail"` for the "not attention"
// half of the split — that continues to work (the dock's own split is just
// "attention vs. not"), but the more important, more regression-prone case
// is the plain-transient-but-actionable one, covered separately below with a
// message built by the REAL store rather than a hand-built mock. A message
// the store would never retain (no tone: "error", no action, no page) never
// reaches this component at all — the store itself refuses to retain it
// (notifications.ts) — so there is nothing to test here for that case.
let messageId = 0;
const message = (over: Partial<StoredNotification> = {}): StoredNotification => ({
  id: ++messageId,
  title: "Could not save",
  tier: "attention",
  recent: false,
  leaving: false,
  ...over,
});

test("an attention-tier message fills the numeral and the needs-you count, like a failure does", () => {
  const tree = renderView({ rows: [], messages: [message({ tier: "attention" })] });
  expect(numeral(tree)).toBe("1");
  expect(text(findAll(tree, "dl-summary")[0])).toBe("1 needs you");
});

test("a trail-tier message fills the numeral but not the needs-you count", () => {
  const tree = renderView({ rows: [], messages: [message({ tier: "trail", title: "Moved 3 items" })] });
  expect(numeral(tree)).toBe("1");
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Notifications");
});

test("an attention message draws in 'Needs you', a trail message in 'Worth keeping'", () => {
  const tree = renderView({
    rows: [],
    messages: [
      message({ tier: "attention", title: "Could not save" }),
      message({ tier: "trail", title: "Moved 3 items" }),
    ],
  });
  const headings = findAll(tree, "dl-section-head").map((h) => text(h));
  expect(headings).toEqual(["Needs you", "Worth keeping"]);
  const rows = findAll(tree, "dl-row").map((r) => text(r));
  expect(rows[0]).toContain("Could not save");
  expect(rows[1]).toContain("Moved 3 items");
});

// The half most likely to regress: a `tone: "info"` message with NO error
// and NO explicit `tier` at all — it resolves to `tier: "transient"` — is
// still retained (and lands in "Worth keeping") purely because it carries a
// `page`. Built through the REAL store (`notify`), not the hand-rolled
// `message()` mock above, so this exercises `isRetained` end to end rather
// than assuming the dock trusts whatever mock tier a test hands it.
test("a tone: info, non-error message with a page is retained and drawn in 'Worth keeping', not dropped", () => {
  _resetNotificationsForTest();
  try {
    notify({ title: "Could not save", tone: "error" }); // gives "Needs you" a row too
    notify({ title: "Export ready", tone: "info", page: "/tasks/42" });
    const stored = getRetainedNotifications();
    expect(stored.map((n) => n.tier)).toEqual(["attention", "transient"]);

    const tree = renderView({ rows: [], messages: stored });
    const headings = findAll(tree, "dl-section-head").map((h) => text(h));
    expect(headings).toEqual(["Needs you", "Worth keeping"]);
    const rows = findAll(tree, "dl-row").map((r) => text(r));
    expect(rows[0]).toContain("Could not save");
    expect(rows[1]).toContain("Export ready");
  } finally {
    _resetNotificationsForTest();
  }
});

test("a message row draws with its detail, like a terminal job's failure message", () => {
  const tree = renderView({ rows: [], messages: [message({ title: "Could not save", detail: "Disk full" })] });
  const row = findAll(tree, "dl-row")[0];
  expect(text(row)).toContain("Could not save");
  expect(text(row)).toContain("Disk full");
});

// Code review finding on PR #1104: `terminal` was passed off `tone` with no
// `status`, which (pre-fix) never rendered the glyph, and this row also
// carried no `role`, losing the deleted `Toast.tsx`'s own
// `role={tone === "info" ? "status" : "alert"}` distinction.
test("an error-tone message row gets role=alert and the terminal glyph; a non-error one gets role=status and no glyph", () => {
  const errorTree = renderView({
    rows: [],
    messages: [message({ title: "Could not save", tone: "error" })],
  });
  const errorRow = findAll(errorTree, "dl-row")[0];
  expect(errorRow.props.role).toBe("alert");
  expect(findAll(errorTree, "dl-status")).toHaveLength(1);

  const infoTree = renderView({
    rows: [],
    messages: [message({ title: "Moved 3 items", tier: "trail", tone: "info" })],
  });
  const infoRow = findAll(infoTree, "dl-row")[0];
  expect(infoRow.props.role).toBe("status");
  expect(findAll(infoTree, "dl-status")).toHaveLength(0);
});

test("a message with a page is a click target that navigates", () => {
  withNav((pushed) => {
    const m = message({ page: "/tasks/42", title: "Export ready" });
    const tree = renderView({ rows: [], messages: [m] });
    const row = findAll(tree, "dl-row")[0];
    expect(findAll(tree, "dl-row-open")).toHaveLength(1);
    act(() => {
      (row.props as { onClick: () => void }).onClick();
    });
    expect(pushed).toContain("/tasks/42");
  });
});

test("a message with no page draws no row-open marker — nothing to click through to", () => {
  const tree = renderView({ rows: [], messages: [message({ title: "Could not save" })] });
  expect(findAll(tree, "dl-row-open")).toHaveLength(0);
});

// `MessageRowView` calls the real `dismissNotification` directly (it is not
// plumbed through a prop, unlike every other row kind's dismiss — see
// RepoUpdatesDock.tsx's header comment on this row), so this test drives the
// real store instead of a mock: seed it with a real retained notification,
// render that SAME `StoredNotification`, press its ✕, and check the store's
// own retained list rather than a callback.
test("pressing a message row's ✕ dismisses it from the real notification store", () => {
  _resetNotificationsForTest();
  try {
    const id = notify({ title: "Could not save", tone: "error" });
    const [stored] = getRetainedNotifications();
    const tree = renderView({ rows: [], messages: [stored] });
    const x = findAll(tree, "dl-x")[0];
    expect(x).toBeDefined();
    act(() => {
      (x.props as { onClick: () => void }).onClick();
    });
    expect(getRetainedNotifications().map((n) => n.id)).not.toContain(id);
  } finally {
    _resetNotificationsForTest();
  }
});

test("Clear all's threshold and count include messages alongside repo rows and terminal jobs", () => {
  const one = renderView({ rows: [], terminal: [], messages: [message({ tier: "trail" })] });
  expect(findAll(one, "dl-clear")).toHaveLength(0);

  const two = renderView({
    rows: [],
    terminal: [doneJob()],
    messages: [message({ tier: "trail" })],
  });
  expect(findAll(two, "dl-clear")).toHaveLength(1);
});

// ---- Recent (SPEC-quiet-notifications.md §4) --------------------------------
//
// D-B: a job `jobRows`/`terminalNotifications` drops for presence reasons
// (`jobs.ts`'s `isRecentOnly`) does not vanish — it folds into its OWN third
// section, "Recent", below "Needs you"/"Worth keeping". Fed here through a
// dedicated `recent` prop (mirroring `terminal`), the way `ActivityDock.tsx`
// hands `RepoUpdatesDock` the complementary `recentNotifications(...)` array
// alongside `terminalNotifications(...)`. Collapsed by default (its own local
// toggle, nothing persisted — same D603 rule as everything else in this
// panel), with a count in its own heading, and excluded from both `total`
// (the chip's numeral) and `attentionCount` ("N needs you") — a suppressed
// success is not news, so it must not make the chip look busier than "Needs
// you"/"Worth keeping" alone would.
test("a Recent row draws only when its section is expanded, and starts collapsed", () => {
  const tree = renderView({ rows: [], recent: [doneJob({ id: "r1" })] });
  // Collapsed: the section heading (with its count) shows, but no row yet.
  expect(findAll(tree, "dl-row")).toHaveLength(0);
  const toggle = findAll(tree, "dl-recent-toggle");
  expect(toggle).toHaveLength(1);
  expect(text(toggle[0])).toBe("Recent (1)");
});

test("a two-member group in Recent counts as ONE row in the section heading, not two", () => {
  // Counts rows, not raw jobs (user decision, verbatim: "yes we should count
  // rows") — two members that fold into one `GroupJobRow` must read "Recent
  // (1)", the number of rows actually rendered, not "Recent (2)".
  const r1 = doneJob({ id: "sys:g:r1", group: "g" });
  const r2 = doneJob({ id: "sys:g:r2", group: "g" });
  const tree = renderView({ rows: [], recent: [r1, r2] });
  const toggle = findAll(tree, "dl-recent-toggle");
  expect(text(toggle[0])).toBe("Recent (1)");
});

test("clicking the Recent toggle reveals its rows; clicking again re-collapses", () => {
  const instance = renderInstance({ rows: [], recent: [doneJob({ id: "r1" })] });
  const toggle = () => findAll(instance.toJSON() as ReactTestRendererJSON, "dl-recent-toggle")[0];
  act(() => {
    (toggle().props as { onClick: () => void }).onClick();
  });
  expect(findAll(instance.toJSON() as ReactTestRendererJSON, "dl-row")).toHaveLength(1);
  act(() => {
    (toggle().props as { onClick: () => void }).onClick();
  });
  expect(findAll(instance.toJSON() as ReactTestRendererJSON, "dl-row")).toHaveLength(0);
});

test("Recent is excluded from the chip's total and from the needs-you count", () => {
  const tree = renderView({ rows: [], recent: [doneJob({ id: "r1" }), doneJob({ id: "r2" })] });
  // No other source at all — the chip must still read idle/"Notifications",
  // not "2", and the panel must not draw the empty sentence either (Recent
  // itself is a real section, just not counted the same way as the other two).
  expect(numeral(tree)).toBeNull();
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Notifications");
  expect(findAll(tree, "dl-panel-empty")).toHaveLength(0);
});

test("Recent never turns the chip loud, even with a would-be-attention job in it", () => {
  // isRecentOnly (jobs.ts) never lets an attention-tier job through in the
  // first place, but this pins the DOCK side of that guarantee too: even if
  // a caller handed one in, Recent must never feed `attentionCount`.
  const tree = renderView({ rows: [], recent: [failedJob({ id: "r1" })] });
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Notifications");
  expect(toggleClasses(tree)).not.toContain("is-failure");
});

test("a lone Recent section still needs no disambiguating heading — 'Needs you'/'Worth keeping' rules are untouched", () => {
  const tree = renderView({ rows: [], recent: [doneJob({ id: "r1" })] });
  expect(findAll(tree, "dl-section-head")).toHaveLength(0);
});

test("Recent sits below 'Needs you' and 'Worth keeping' when both are present", () => {
  const tree = renderView({
    rows: repoRows([status({ root: "/a/one" })]),
    terminal: [failedJob()],
    recent: [doneJob({ id: "r1" })],
  });
  const heads = findAll(tree, "dl-section-head").map((n) => text(n));
  expect(heads).toEqual(["Needs you", "Worth keeping"]);
  const toggle = findAll(tree, "dl-recent-toggle");
  expect(toggle).toHaveLength(1);
});

test("dismissing a Recent row patches the recent list, not the terminal one", async () => {
  const patchedRecent: Array<(jobs: Job[]) => Job[]> = [];
  const patchedTerminal: Array<(jobs: Job[]) => Job[]> = [];
  const realFetch = globalThis.fetch;
  (globalThis as { fetch?: unknown }).fetch = mock(() =>
    Promise.resolve(new Response(JSON.stringify({ dismissed: "r1" }))),
  );
  const instance = renderInstance({
    rows: [],
    recent: [doneJob({ id: "r1" })],
    onRecentPatch: (fn) => patchedRecent.push(fn),
    onTerminalPatch: (fn) => patchedTerminal.push(fn),
  });
  act(() => {
    const toggle = findAll(
      instance.toJSON() as ReactTestRendererJSON,
      "dl-recent-toggle",
    )[0];
    (toggle.props as { onClick: () => void }).onClick();
  });
  const dismissBtn = findAll(instance.toJSON() as ReactTestRendererJSON, "dl-x")[0];
  await act(async () => {
    (dismissBtn.props as { onClick: () => void }).onClick();
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(patchedRecent.length).toBeGreaterThan(0);
  expect(patchedTerminal).toHaveLength(0);
  globalThis.fetch = realFetch;
});

test("Clear all also reaches Recent — the same server-side clear, patched into both lists", async () => {
  const realFetch = globalThis.fetch;
  (globalThis as { fetch?: unknown }).fetch = mock(() =>
    Promise.resolve(new Response(JSON.stringify({ cleared: 1 }))),
  );
  const patchedRecent: Array<(jobs: Job[]) => Job[]> = [];
  const instance = renderInstance({
    rows: [],
    terminal: [doneJob({ id: "t1" })],
    recent: [doneJob({ id: "r1" }), doneJob({ id: "r2" })],
    onRecentPatch: (fn) => patchedRecent.push(fn),
  });
  const clearBtn = findAll(instance.toJSON() as ReactTestRendererJSON, "dl-clear")[0];
  await act(async () => {
    (clearBtn.props as { onClick: () => void }).onClick();
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(patchedRecent.length).toBeGreaterThan(0);
  globalThis.fetch = realFetch;
});

// §3 (SPEC-quiet-notifications.md): the multi-member group row UI.

test("a two-member group renders as ONE row, with an 'N of M done' subline", () => {
  const g1 = doneJob({ id: "sys:g:a", group: "g" });
  const g2 = doneJob({ id: "sys:g:b", group: "g" });
  const tree = renderView({ rows: [], terminal: [g1, g2] });
  // One row for the whole group, not two.
  const rows = findAll(tree, "dl-row");
  expect(rows).toHaveLength(1);
  // The oldest (first-arrival) member's own title represents the group.
  expect(text(findAll(tree, "dl-title")[0])).toBe(g1.title);
  expect(text(findAll(tree, "dl-model")[0])).toBe("2 of 2 done");
  expect(findAll(tree, "dl-row-group-attention")).toHaveLength(0);
});

test("a two-member group with one failing member gets the attention stripe and counts as ONE row toward 'needs you'", () => {
  const ok = doneJob({ id: "sys:g:a", group: "g" });
  const bad = failedJob({ id: "sys:g:b", group: "g" });
  const tree = renderView({ rows: [], terminal: [ok, bad] });
  // One row, not split across "Needs you"/"Worth keeping" — D-C's "one
  // failing member keeps the whole group visible" rule, at the row level.
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  expect(findAll(tree, "dl-row-group-attention")).toHaveLength(1);
  expect(text(findAll(tree, "dl-model")[0])).toBe("1 of 2 done");
  // Row counts, not raw job counts (user decision, verbatim: "yes we should
  // count rows") — the whole two-member group is ONE row on screen, so it
  // counts once toward the badge, the same as any other single row.
  expect(text(findAll(tree, "dl-summary")[0])).toBe("1 needs you");
});

test("a two-member group counts as ONE row toward the chip's total, not two", () => {
  // Counts rows, not raw jobs (user decision, verbatim: "yes we should count
  // rows") — the chip's numeral must read "1", the number of rows on screen,
  // even though two jobs are folded into it.
  const g1 = doneJob({ id: "sys:g:a", group: "g" });
  const g2 = doneJob({ id: "sys:g:b", group: "g" });
  const tree = renderView({ rows: [], terminal: [g1, g2] });
  expect(numeral(tree)).toBe("1");
});

test("a single-member group renders unchanged via JobRow — the regression trap this task named by number", () => {
  const tree = renderView({ rows: [], terminal: [failedJob()] });
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  expect(findAll(tree, "dl-row-group-attention")).toHaveLength(0);
  // No group subline — JobRow's own status line, not `GroupJobRow`'s
  // "N of M done" one.
  expect(text(findAll(tree, "dl-model")[0] ?? "")).not.toContain("of");
  expect(findAll(tree, "dl-x")).toHaveLength(1);
});

test("dismissing a group's row dismisses every member at once, and removes all of them from state", async () => {
  const dismissed: string[] = [];
  const realFetch = globalThis.fetch;
  (globalThis as { fetch?: unknown }).fetch = mock((url: string) => {
    const match = /\/api\/jobs\/([^/]+)\/dismiss/.exec(url);
    const id = match ? decodeURIComponent(match[1]) : "";
    dismissed.push(id);
    return Promise.resolve(new Response(JSON.stringify({ dismissed: id })));
  });
  const patchedTerminal: Array<(jobs: Job[]) => Job[]> = [];
  const g1 = doneJob({ id: "sys:g:a", group: "g" });
  const g2 = doneJob({ id: "sys:g:b", group: "g" });
  const instance = renderInstance({
    rows: [],
    terminal: [g1, g2],
    onTerminalPatch: (fn) => patchedTerminal.push(fn),
  });
  const dismissBtn = findAll(instance.toJSON() as ReactTestRendererJSON, "dl-x")[0];
  await act(async () => {
    (dismissBtn.props as { onClick: () => void }).onClick();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(patchedTerminal.length).toBeGreaterThan(0);
  const remaining = patchedTerminal.reduce((jobs, fn) => fn(jobs), [g1, g2] as Job[]);
  expect(remaining).toHaveLength(0);
  globalThis.fetch = realFetch;
});

// ---- CHANGE 1: every notification names who raised it (`.dl-origin`) -------
//
// User, from a screenshot: "every notification imo should have a top
// row/section for the 'emitting page' context" — a toast reading only
// "Public link token flash finished" gave no clue which project it came
// from. `Job.origin`/a message's `origin` (notifications.ts's
// `labelForSource`) already carry that fact; this section pins that every
// row TYPE actually draws it, not just jobs.

test("a job row draws its origin caption; a job with no origin draws no line at all", () => {
  const withOrigin = renderView({ rows: [], terminal: [failedJob({ origin: "Playground" })] });
  const caption = findAll(withOrigin, "dl-origin");
  expect(caption).toHaveLength(1);
  expect(text(caption[0])).toBe("Playground");

  const without = renderView({ rows: [], terminal: [failedJob({ origin: "" })] });
  expect(findAll(without, "dl-origin")).toHaveLength(0);
});

test("a folded group row draws the oldest member's origin, not one per member", () => {
  const g1 = doneJob({ id: "sys:g:a", group: "g", origin: "Local models" });
  const g2 = doneJob({ id: "sys:g:b", group: "g", origin: "Benchmark" });
  const tree = renderView({ rows: [], terminal: [g1, g2] });
  const caption = findAll(tree, "dl-origin");
  expect(caption).toHaveLength(1);
  expect(text(caption[0])).toBe("Local models");
});

test("a message row draws its origin caption when the notify() call carried a source", () => {
  const withSource = renderView({
    rows: [],
    messages: [message({ tier: "attention", title: "Could not save", origin: "my-app" })],
  });
  const caption = findAll(withSource, "dl-origin");
  expect(caption).toHaveLength(1);
  expect(text(caption[0])).toBe("my-app");

  const noSource = renderView({
    rows: [],
    messages: [message({ tier: "attention", title: "Could not save" })],
  });
  expect(findAll(noSource, "dl-origin")).toHaveLength(0);
});

test("a waiting-task row draws its origin caption from the task's own target/project", () => {
  const withOrigin = renderView({ rows: [], attention: [asking({ origin: "my-project" })] });
  const caption = findAll(withOrigin, "dl-origin");
  expect(caption).toHaveLength(1);
  expect(text(caption[0])).toBe("my-project");

  const without = renderView({ rows: [], attention: [asking({ origin: "" })] });
  expect(findAll(without, "dl-origin")).toHaveLength(0);
});

// ---- CHANGE 2: a finished task is retained, clickable, and folded ----------
//
// task-status-notify.ts's `in_progress -> done` now sets `page` (retained,
// clickable) and `recent: true` (folded into §4's "Recent" rather than
// shouting at the top of "Worth keeping") — this pins where it actually
// lands, per `NotificationInput.recent`/`StoredNotification.recent`.

test("a `recent` message lands in the folded Recent section, not 'Worth keeping'", () => {
  const tree = renderView({
    rows: [],
    messages: [message({ tier: "transient", title: "Task finished", page: "/tasks", recent: true })],
  });
  // Collapsed by default — same as a job's own Recent row: the heading
  // shows, but the row itself does not draw until opened.
  expect(findAll(tree, "dl-row")).toHaveLength(0);
  const toggle = findAll(tree, "dl-recent-toggle");
  expect(toggle).toHaveLength(1);
  expect(text(toggle[0])).toBe("Recent (1)");
  expect(findAll(tree, "dl-section-head").map((n) => text(n))).not.toContain("Worth keeping");
});

test("a `recent` message is excluded from the chip's total, like a job's own Recent row", () => {
  const tree = renderView({
    rows: [],
    messages: [message({ tier: "transient", title: "Task finished", page: "/tasks", recent: true })],
  });
  expect(numeral(tree)).toBeNull();
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Notifications");
});

test("clicking the Recent toggle reveals a folded message row, clickable via its own page", () => {
  const instance = renderInstance({
    rows: [],
    messages: [message({ tier: "transient", title: "Task finished", page: "/tasks", recent: true })],
  });
  act(() => {
    const toggle = findAll(
      instance.toJSON() as ReactTestRendererJSON,
      "dl-recent-toggle",
    )[0];
    (toggle.props as { onClick: () => void }).onClick();
  });
  const rows = findAll(instance.toJSON() as ReactTestRendererJSON, "dl-row");
  expect(rows).toHaveLength(1);
  // `MessageRowView` sets an explicit `role` ("status"/"alert") for its
  // content, which wins over `rowClick`'s own implicit `role="button"`
  // (NotificationCard.tsx) — clickability is `dl-row-open` + a real
  // `onClick`, not the ARIA role.
  expect(rows[0].props.className).toContain("dl-row-open");
  expect(typeof (rows[0].props as { onClick?: () => void }).onClick).toBe("function");
});

test("a non-recent, retained message still lands in 'Worth keeping', unfolded (unchanged behaviour)", () => {
  const tree = renderView({
    rows: [],
    messages: [message({ tier: "transient", title: "Moved 3 items", page: "/tasks" })],
  });
  expect(findAll(tree, "dl-recent-toggle")).toHaveLength(0);
  expect(findAll(tree, "dl-row").map((n) => text(n))).toEqual(
    expect.arrayContaining([expect.stringContaining("Moved 3 items")]),
  );
});
