// The PARENT's behaviour, not JobRow's own (see JobRow.test.tsx for that half).
//
// PR #785 follow-up: JobRow alone returning null for a "done" job left every
// decision ABOVE it still counting the invisible row — the empty-card gate
// (`jobs.length === 0 && queued === 0`), the header's "N finished" tally, and
// `clearable` (which drew a Clear button for a row nobody could see). A
// successful job has to disappear from ALL of those, not just from its own
// row, or a lone success leaves an empty bordered box with a header and a
// Clear button over nothing — worse than the "done" row this was meant to
// remove.
//
// Rendered through `DownloadManagerView` — the pure, props-in half of this
// card, exported for exactly this file the way `JobRow` is exported for its
// own test — rather than the default-exported `DownloadManager`, which polls
// `/api/jobs` itself. Mounting THAT would mean mocking `@platform/lib/api`,
// which is shared by dozens of other test files; `mock.module` replaces it
// for the whole bun process, not just one file, and that contaminated an
// unrelated suite (`apps/ai_models/playground/client.test.ts`) the first
// time this file tried it. `DownloadManagerView` needs no such thing: no
// polling, no network, no `window`/`document`.
import { describe, expect, it, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";

import {
  DownloadManagerView,
  engineLabel,
  engineDetail,
  engineKind,
  type EnginesSlot,
} from "@platform/ui/DownloadManager";
import { engineDuration, jobAmount, type Job } from "@platform/lib/jobs";
import type { RunningEngine } from "@platform/lib/api";

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

const BASE: Job = {
  id: "sys:ai-image:x",
  title: "a red fox",
  detail: "Saved x.png",
  model: "",
  kind: "task",
  state: "done",
  done: 28,
  total: 28,
  total_scope: "phase",
  unit: "",
  message: "",
  page: "",
  owner: "server",
  cancellable: true,
  cancel_requested: false,
  started_at: 0,
  updated_at: 0,
  finished_at: 0,
  stalled: false,
  waiting_for: "",
};


// D673: the chip is a `.dl-toggle sc` button — no `.dl-dot` circle any more.
// These helpers read its tone, its optional numeral (`.sc-num`, count > 0
// only) and its optional progress line (`.sc-progress-fill`) instead.
function toggleClasses(tree: ReactTestRendererJSON | null): string[] {
  return ((findAll(tree, "dl-toggle")[0]?.props.className as string) ?? "").split(" ");
}

function numeral(tree: ReactTestRendererJSON | null): string | null {
  const nums = findAll(tree, "sc-num");
  return nums.length ? text(nums[0]) : null;
}

function progressFillWidth(tree: ReactTestRendererJSON | null): string | undefined {
  const fills = findAll(tree, "sc-progress-fill");
  return fills.length ? (fills[0].props.style as { width?: string } | undefined)?.width : undefined;
}

// EVERY HARNESS BELOW PASSES `initialCollapsed={false}`: the default is
// COLLAPSED (D595, made unconditional in D603), and these tests are about what
// the PANEL contains and how the fold behaves — not about that default. Saying
// so explicitly is what keeps them from silently inverting the next time the
// default is revisited; the default itself has its own test.
function renderCard(reported: Job[]): ReactTestRendererJSON | null {
  return create(
    <DownloadManagerView
      reported={reported}
      initialCollapsed={false}
      refresh={() => {}}
      patch={() => {}}
    />,
  ).toJSON() as ReactTestRendererJSON | null;
}

// D603: the real default, with no seam passed. It is now UNCONDITIONAL —
// nothing is read from storage, so there is no absent-key case, no private-mode
// case and no stored `"0"` that could restore a panel over the page on reload
// (which is exactly what the user reported: "on page reload the models popover
// auto opens for some reason"). Its own harness so no other test depends on the
// default staying put.
test("a section always starts collapsed, with nothing persisted to say otherwise", () => {
  const running: Job = { ...BASE, id: "sys:ai-image:live", state: "running", stalled: false };
  const tree = create(
    <DownloadManagerView reported={[running]} refresh={() => {}} patch={() => {}} />,
  ).toJSON() as ReactTestRendererJSON | null;
  expect(findAll(tree, "dl-toggle")).toHaveLength(1); // the chip is still there
  expect(findAll(tree, "dl-panel")).toHaveLength(0); // ...and nothing is open
});

// For tests that need to actually PRESS the toggle (collapsing is real
// component state, not a prop) rather than just inspect one static render.
function renderInstance(reported: Job[]): ReactTestRenderer {
  return create(
    <DownloadManagerView
      reported={reported}
      initialCollapsed={false}
      refresh={() => {}}
      patch={() => {}}
    />,
  );
}

test("a jobs list holding only a successful (done) job renders the IDLE chip, not nothing (D565/D573)", () => {
  // The section used to return null here — D565 replaced the whole
  // empty-card gate with an idle readout; D573 moved that readout from a
  // separate `.dl-idle` span into the panel a real, always-clickable chip
  // opens (VS Code/Cursor status-bar idiom — no chevron, hover only).
  const tree = renderCard([{ ...BASE, id: "sys:ai-image:only-done" }]);
  expect(tree).not.toBeNull();
  // `Activity` (status-bar merge) — this chip also carries the
  // Background-tasks section since Engines folded into it (Models made the
  // same trip and then split back out into its own chip, `shell/ModelsDock.tsx`);
  // see DownloadManagerView's own header for why the narrower `Jobs` (D579) no
  // longer names everything it shows.
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Activity");
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No activity");
  // D673: idle draws no numeral and no progress line at all.
  expect(numeral(tree)).toBeNull();
  expect(findAll(tree, "sc-progress")).toHaveLength(0);
  expect(toggleClasses(tree)).toContain("is-idle");
});

test("a done job beside a running one renders exactly one visible row and no Clear button", () => {
  const done = { ...BASE, id: "sys:ai-image:done" };
  const running: Job = {
    ...BASE,
    id: "sys:ai-image:running",
    state: "running",
    detail: "Denoising…",
    done: 2,
    total: 28,
  };
  const tree = renderCard([done, running]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  expect(findAll(tree, "dl-clear")).toHaveLength(0);
});

// SUPERSEDED BY D586, and both halves of the old assertion are now the
// opposite: the success still vanishes (unchanged), and the error row no
// longer draws HERE either — it moved to Notifications, taking its Clear with
// it. So a list of one success plus one failure leaves this section empty.
test("a done job and a failed job together leave this section with nothing to draw", () => {
  const done = { ...BASE, id: "sys:ai-image:done" };
  const errored: Job = {
    ...BASE,
    id: "sys:ai-image:errored",
    state: "error",
    message: "boom",
  };
  const tree = renderCard([done, errored]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(0);
  // No Clear either: with the failure gone there is no terminal row left for
  // it to take, and offering a button that would do nothing is the exact
  // blank-band problem code review finding #3 fixed below.
  expect(findAll(tree, "dl-clear")).toHaveLength(0);
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No activity");
});

test("a scheduled run's own job never draws a row here, in any state (D661)", () => {
  // D661 (user: "a task is not something I even want in the activity. that
  // was added unintentionally"): `jobRows` now excludes every `sys:schedule:*`
  // job unconditionally, so a scheduled run's own row cannot appear here no
  // matter what state it is in — there is no more "exempt only while running"
  // carve-out.
  const scheduleDone: Job = { ...BASE, id: "sys:schedule:entry-1", title: "Nightly digest" };
  const scheduleRunning: Job = { ...BASE, id: "sys:schedule:entry-2", state: "running" };
  const tree = renderCard([scheduleDone, scheduleRunning]);
  expect(findAll(tree, "dl-row")).toHaveLength(0);
});

test("a stalled but still-running job alone offers no Clear button", () => {
  // D558: Clear used to count a stalled row as clearable — mirroring
  // clear_finished's own old bug — which meant pressing it could sweep the
  // RECORD of a job that was still genuinely running. The row itself is
  // still shown (dimmed via is-stalled); only the bulk button's count
  // changed.
  const stalled: Job = { ...BASE, id: "sys:ai-image:stalled", state: "running", stalled: true };
  const tree = renderCard([stalled]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  expect(findAll(tree, "dl-clear")).toHaveLength(0);
});

// Also superseded by D586: the "terminal row" this used to pair with a stalled
// one was an ERROR row, which no longer lives here. A stalled row is still
// `running`, so it is not clearable — `clearableCount`'s own rule, which D586
// did not change — and with the failure gone there is nothing for Clear to
// take. Kept (rather than deleted) because the stalled-row half is still worth
// pinning: a stalled row draws, and it is not swept up by Clear.
test("a stalled running row draws and is NOT clearable, with no failure beside it", () => {
  const stalled: Job = { ...BASE, id: "sys:ai-image:stalled", state: "running", stalled: true };
  const errored = { ...BASE, id: "sys:ai-image:errored", state: "error" as const, message: "boom" };
  const tree = renderCard([stalled, errored]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  expect(findAll(tree, "dl-clear")).toHaveLength(0);
});

test("the panel's head is OMITTED, not a blank band, when nothing to offer — code review finding #3", () => {
  // One job running, nothing terminal, nothing clearable: before this fix
  // `.dl-head` still rendered — an empty ~30px padded band over the row list,
  // since the header used to always hold at least the toggle before the
  // chip/panel split moved the toggle out of it.
  const running: Job = { ...BASE, id: "sys:ai-image:running", state: "running", stalled: false };
  const tree = renderCard([running]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-head")).toHaveLength(0);
  expect(findAll(tree, "dl-row")).toHaveLength(1);
});

// D586 (user: "maybe we can have a flow like running activities are shown in
// jobs and after done, a completed message goes to notifications?"): a failure
// is not work in progress, so it LEAVES this section entirely — rows, count,
// and the failure tint that used to colour this chip. Notifications draws it
// now (RepoUpdatesDock.test.tsx covers the receiving end).
test("a failed job is drawn NOWHERE in this section — not a row, not the numeral, no tint", () => {
  const errored: Job = { ...BASE, id: "sys:ai-image:errored", state: "error", message: "boom" };
  const tree = renderCard([errored]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(0);
  // The chip answers what this section actually holds, so a lone failure
  // leaves it idle with no numeral. A chip that still lit up for a failure
  // would be the same bug the old count version had.
  expect(numeral(tree)).toBeNull();
  expect(toggleClasses(tree)).toContain("is-idle");
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No activity");
  expect(toggleClasses(tree)).not.toContain("is-failure");
});

test("a failure beside live work leaves only the live row, labelled by the row itself — no numeral for one", () => {
  const running: Job = { ...BASE, id: "sys:ai-image:live", state: "running", stalled: false };
  const errored: Job = { ...BASE, id: "sys:ai-image:errored", state: "error", message: "boom" };
  const tree = renderCard([running, errored]);
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  // Exactly one LIVE job: the chip's own label becomes the job's phase verb
  // (`jobTypeLabel`) rather than "Activity N" — a numeral only appears at 2+.
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Working");
  expect(numeral(tree)).toBeNull();
  expect(toggleClasses(tree)).not.toContain("is-idle");
});

// ----------------------------------------- the model-load merge (SPEC §36) —
// a render waiting on a shared model load used to open a second row right
// beside the load's own, both saying the same thing ("Waiting for
// FLUX.2-klein-4B — Loading weights into memory……" next to "Loading weights
// into memory…"). `_wait_ready` (fused_render/ai/supervisor.py) now mirrors
// the load's own progress onto the waiter's row and marks it `waiting_for`;
// `jobs.ts` `mergedRows` (applied in `DownloadManagerView`'s `jobs` computation)
// is what makes the card actually draw one row instead of two.

test("a waiter and the load it is blocked on render as ONE row, carrying the load's detail", () => {
  const waiter: Job = {
    ...BASE,
    id: "sys:ai-image:x",
    title: "a ginger cat in a hand-stitched astronaut suit",
    model: "black-forest-labs/FLUX.2-klein-4B",
    state: "running",
    detail: "Loading weights into memory…",
    done: null,
    total: null,
    waiting_for: "sys:ai-model:black-forest-labs--FLUX.2-klein-4B",
  };
  const load: Job = {
    ...BASE,
    id: "sys:ai-model:black-forest-labs--FLUX.2-klein-4B",
    title: "black-forest-labs/FLUX.2-klein-4B",
    model: "black-forest-labs/FLUX.2-klein-4B",
    kind: "download",
    state: "running",
    detail: "Loading weights into memory…",
    done: null,
    total: null,
  };
  const tree = renderCard([waiter, load]);
  const rows = findAll(tree, "dl-row");
  expect(rows).toHaveLength(1);
  expect(text(findAll(rows[0], "dl-title")[0])).toBe(waiter.title);
  expect(text(findAll(rows[0], "dl-status")[0])).toBe("Loading weights into memory…");
});

test("once the waiter goes terminal, only the load's row is left — the waiter moved to Notifications", () => {
  // D586, broadened by D662: EVERY terminal state (done/error/cancelled), not
  // only `error`, leaves this card for Notifications. So a waiter that goes
  // `cancelled` does not linger here with a stale `waiting_for` pointed at a
  // still-running load — it is simply gone, and the load's own row is all
  // that is left to draw.
  const waiter: Job = {
    ...BASE,
    id: "sys:ai-image:x",
    title: "a ginger cat",
    state: "cancelled",
    waiting_for: "sys:ai-model:m",
  };
  const load: Job = { ...BASE, id: "sys:ai-model:m", title: "org/m", state: "running" };
  const tree = renderCard([waiter, load]);
  const rows = findAll(tree, "dl-row");
  expect(rows).toHaveLength(1);
  expect(text(findAll(rows[0], "dl-title")[0])).toBe(load.title);
});

// -------------------------------------------------- the collapse toggle (D562)
//
// Reversed by user call (2026-08-27): "there should be nothing called a
// 'non foldable card' — everything is foldable, even for the job cards."
// D558/D559's whole premise — SOME rows (the queue's, a live schedule
// stand-in) were exempt from the fold, so the toggle sometimes had nothing
// to hide and was drawn as an inert `<span>` — is gone. The toggle is ALWAYS
// a real `<button>`, and collapsing ALWAYS hides every row.
//
// Collapsed is now a CHIP in the status bar, not a short card (D563): the
// toggle IS the chip, and it carries no controls — `queue.cancelAll` and
// Clear render only in the panel that opens when expanded, so neither
// survives a collapse any more. What the chip itself draws has since been
// stripped to the bone: the label `Jobs` plus ONE circle, outlined when the
// section holds nothing and filled when it holds anything (D588/D590, user:
// "no count. just a circle outlined or filled"). `jobsSummary`'s sentence went
// in D573 (and the function itself is now deleted), the aggregate percentage
// `dl-pct` in D581, the count in D588/D590. A collapsed section says THAT
// something is happening and nothing more; the panel is where the rest lives.

function clickToggle(renderer: ReactTestRenderer) {
  const before = renderer.toJSON() as ReactTestRendererJSON;
  const toggle = findAll(before, "dl-toggle")[0];
  act(() => {
    (toggle.props as { onClick: () => void }).onClick();
  });
}

test("the toggle is a real button even with a lone job to fold, a scheduled run beside it drawing nothing", () => {
  // D661: a scheduled run's own row never draws here, in any state — this
  // pins that its ABSENCE does not also take the toggle down with it, as
  // long as a real job row is still present.
  const liveSchedule: Job = {
    ...BASE,
    id: "sys:schedule:entry-1",
    state: "running",
    stalled: false,
  };
  const running: Job = { ...BASE, id: "sys:ai-image:running", state: "running", stalled: false };
  const tree = renderCard([liveSchedule, running]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  const toggles = findAll(tree, "dl-toggle");
  expect(toggles).toHaveLength(1);
  expect(toggles[0].type).toBe("button");
});

test("collapsing hides every row", () => {
  const running: Job = { ...BASE, id: "sys:ai-image:running", state: "running", stalled: false };
  const renderer = renderInstance([running]);

  const before = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(before, "dl-row")).toHaveLength(1);

  clickToggle(renderer);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-row")).toHaveLength(0);
  expect(findAll(after, "dl-rows")).toHaveLength(0); // no empty box left behind
});

test("the header still names the hidden work once collapsed", () => {
  const running: Job = { ...BASE, id: "sys:ai-image:running", state: "running", stalled: false };
  const renderer = renderInstance([running]);
  const before = text(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-summary")[0]);
  clickToggle(renderer);
  const after = text(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-summary")[0]);
  expect(before).toBe(after);
  expect(after.length).toBeGreaterThan(0);
});

// D581 removed the OLD aggregate percentage — a text readout that appeared
// and disappeared in place, shifting the whole bar. D673 (statusbar redesign)
// brings progress back, but as a thin line along the chip's own bottom edge
// (`StatusChip`'s `progress` prop, `aggregateProgress`), not text — that is a
// fixed-width element the bar's layout never has to reflow around.
test("the chip's progress line reflects a lone running job's fraction, in both fold states", () => {
  const running: Job = {
    ...BASE,
    id: "sys:ai-image:running",
    state: "running",
    done: 5,
    total: 10,
    stalled: false,
  };
  const renderer = renderInstance([running]);

  // Expanded: the row itself still carries both the percentage and the bar...
  const expanded = renderer.toJSON() as ReactTestRendererJSON;
  const row = findAll(expanded, "dl-row")[0];
  expect(text(findAll(row, "dl-pct")[0])).toBe("50%");
  expect(findAll(row, "dl-bar")).toHaveLength(1);
  // ...and the chip itself now draws the same fraction along its own edge.
  expect(progressFillWidth(expanded)).toBe("50%");

  clickToggle(renderer); // collapse

  // Collapsed: the row is gone, but the chip's progress line lives on the
  // chip itself, not inside the panel, so it survives the fold unchanged.
  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-bar")).toHaveLength(0);
  expect(findAll(after, "dl-pct")).toHaveLength(0);
  expect(progressFillWidth(after)).toBe("50%");
});

// ------------------------------- nothing opens or closes on its own (D673) —
//
// The bar used to pop this panel open on a new job's arrival (D574) and slam
// it shut again when the list drained (D580) — both since deleted
// (`lib/statusChip.ts`'s own header has the full reasoning): a background job
// finishing or starting must never throw a floating panel over whatever page
// the user is looking at, uninvited. The chip's own label/numeral/progress
// line is the entire announcement now; the panel opens ONLY via the chip's
// click (`useStatusChip`'s `toggle`), and stays exactly as the user left it.

function updateInstance(renderer: ReactTestRenderer, reported: Job[]) {
  act(() => {
    renderer.update(<DownloadManagerView reported={reported} refresh={() => {}} patch={() => {}} />);
  });
}

test("a new job arriving while collapsed does NOT open the panel — the chip's own numeral is the announcement", () => {
  const first: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  const renderer = renderInstance([first]);
  clickToggle(renderer); // collapse

  const collapsed = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(collapsed, "dl-panel")).toHaveLength(0);

  const second: Job = { ...BASE, id: "sys:ai-image:b", state: "running", stalled: false };
  updateInstance(renderer, [first, second]);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(0);
  // 2+ jobs draws the numeral, even while the panel stays shut.
  expect(numeral(after)).toBe("2");
});

test("a pinned panel stays open and shows the idle sentence once its list drains — no auto-close", () => {
  const job: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  // `renderInstance` mounts with `initialCollapsed={false}` — pinned open.
  const renderer = renderInstance([job]);
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-panel")).toHaveLength(1);

  updateInstance(renderer, []); // the job finished and was cleared

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(1); // still open — nothing auto-closed it
  expect(text(findAll(after, "dl-panel-empty")[0])).toBe("No activity");
  expect(text(findAll(after, "dl-summary")[0])).toBe("Activity");
  expect(toggleClasses(after)).toContain("is-idle");
});

test("collapsing, then an EXISTING job merely changing, opens nothing", () => {
  const job: Job = {
    ...BASE,
    id: "sys:ai-image:a",
    state: "running",
    done: 1,
    total: 10,
    stalled: false,
  };
  const renderer = renderInstance([job]);
  clickToggle(renderer); // collapse

  // Same id, progress ticking (and even finishing) — not a new arrival, and
  // arrivals do not open the panel any more regardless.
  updateInstance(renderer, [{ ...job, done: 9, state: "running" }]);
  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(0);
});

// ---- a running row's Cancel must never be pushed out of the panel --------------
// D569, a measured real bug: a running, cancellable row with a realistic model
// name (`Downloading Qwen2.5-Coder-32B-Instruct-4bit`) pushed `.dl-row-cancel`
// 33px past `.dl-panel`'s right edge, clipped in half by the panel's own
// `overflow: hidden`. `react-test-renderer` has no viewport — it cannot lay
// out flex children or measure a pixel, which is exactly how this shipped
// green in round 2 — so this is a STYLESHEET-LEVEL source pin: it proves the
// row's protected controls stay `flex: 0 0 auto` and that the two elements
// which now share the job of giving way (`.dl-title`, `.dl-model`) both carry
// the full shrink-to-ellipsis contract, not that a browser renders it inside
// the width. The real geometry was verified against a running dev server.
// D598 (user: "why isn't the step count next to denoising?"): the amount is a
// PROGRESS fact, so it joins the other progress facts on the status line
// instead of sitting up beside the title as a number with no context. The three
// shapes below are the ones that can actually occur, and the middle one is the
// regression this had to avoid — amount and status are INDEPENDENTLY present,
// so a download row with no phase text must not lose its byte counts.

test("a task row reads its phase and step count as one sentence", () => {
  const denoising: Job = {
    ...BASE,
    id: "sys:ai-image:flux",
    title: "make it anime styled",
    detail: "Denoising",
    state: "running",
    stalled: false,
    done: 0,
    total: 4,
    unit: "",
  };
  const tree = renderCard([denoising]);
  const row = findAll(tree, "dl-row")[0];
  expect(text(findAll(row, "dl-status")[0])).toBe("Denoising · 0 / 4");
  // The amount is no longer its own element on the head line...
  expect(findAll(findAll(row, "dl-row-head")[0], "dl-amount")).toHaveLength(0);
  // ...but the percentage STAYS there, glanceable and aligned down the list.
  expect(text(findAll(findAll(row, "dl-row-head")[0], "dl-pct")[0])).toBe("0%");
});

test("a download row with NO phase text still shows its byte counts", () => {
  // The regression guard: the old `(failure ?? status) &&` gate would have
  // dropped this row's amount entirely, which is worse than the problem D598
  // set out to fix.
  const download: Job = {
    ...BASE,
    id: "sys:ai-download:x",
    title: "Qwen3-8B",
    detail: "",
    state: "running",
    stalled: false,
    done: 4.2e9,
    total: 10e9,
    unit: "bytes",
  };
  const tree = renderCard([download]);
  const status = findAll(findAll(tree, "dl-row")[0], "dl-status");
  expect(status).toHaveLength(1);
  // Bare amount, no leading separator — `filter(Boolean)` drops the empty part
  // rather than joining onto nothing.
  expect(text(status[0])).toBe(jobAmount(download));
  expect(text(status[0]).startsWith(" · ")).toBe(false);
});

test("a failure takes the whole line and gets no amount appended", () => {
  // Precedence unchanged: the sentence is about the button the user just
  // pressed, and " · 0 / 4" after it would read as progress on the failure.
  const failed: Job = {
    ...BASE,
    id: "sys:ai-image:boom",
    title: "make it anime styled",
    detail: "Denoising",
    state: "error",
    message: "GDAL ran out of memory",
    done: 0,
    total: 4,
  };
  const tree = renderCard([failed]);
  // An `error` row is re-routed to Notifications (D586), so it draws no row
  // here at all — the failure PRECEDENCE is exercised by JobRow.test.tsx's own
  // rejected-cancel tests, which drive `failure` (the local action's) rather
  // than `job.state`.
  expect(findAll(tree, "dl-row")).toHaveLength(0);
});

describe("the row uses LINES, not a shrink ladder (D596)", () => {
  const { readFileSync } = require("node:fs") as typeof import("node:fs");
  const { join } = require("node:path") as typeof import("node:path");
  const CSS = readFileSync(join(import.meta.dir, "../../styles/notifications.css"), "utf8");

  function block(css: string, selector: string): string {
    const at = css.indexOf(selector + " {");
    expect(at).toBeGreaterThan(-1);
    return css.slice(at, css.indexOf("}", at));
  }

  // D596 (user: "we have a ton of free space in the jobs card. why are we
  // truncating stuff instead of placing things elsewhere?"). D571/D577 kept
  // answering "which text loses?" on a single head line; the row was never
  // short of space, it was short of LINES. The old ladder — `.dl-amount` 9999,
  // `.dl-model` 999, `.dl-title` 1 — is RETIRED, so the tests that pinned it
  // are replaced rather than retuned.

  it("wraps the title to two clamped lines instead of ellipsising it on one", () => {
    const rule = block(CSS, ".dl-title");
    expect(rule).toContain("-webkit-line-clamp: 2;");
    expect(rule).toContain("overflow-wrap: anywhere;");
    // The one-line treatment is what produced `Downloa…` and `F…`.
    expect(rule).not.toContain("white-space: nowrap;");
    // ...and the floor that stops a narrow panel mincing it survives (D577).
    expect(rule).toContain("min-width: 15ch;");
  });

  it("leaves the model suffix with no shrink factor at all — it is off the head line", () => {
    const rule = block(CSS, ".dl-model");
    expect(rule).not.toContain("flex:");
    expect(rule).not.toContain("min-width:");
  });

  it("protects EVERY remaining item on the head line, amount included", () => {
    // `.dl-amount` rejoined this group in D596: with the model gone and the
    // title wrapping, there is nothing a shrinking amount would buy, and a
    // byte count minced to one character is not information.
    for (const selector of [".dl-amount", ".dl-pct", ".dl-row-cancel", ".dl-x"]) {
      expect(block(CSS, selector)).toContain("flex: 0 0 auto;");
    }
  });

  // THE ACTUAL RELAYOUT, asserted in the MARKUP rather than in the CSS text —
  // the CSS above is only correct if the element really did move out of the
  // head, and a stylesheet assertion cannot see that.
  it("renders the model OUTSIDE the head line, once, on its own", () => {
    const running: Job = {
      ...BASE,
      id: "sys:ai-image:flux",
      title: "update picture to be ghibli style with more colour",
      model: "mlx-community/FLUX.2-Klein-4B-4bit",
      state: "running",
      stalled: false,
      done: 3,
      total: 4,
    };
    const tree = renderCard([running]);
    const row = findAll(tree, "dl-row")[0];
    const head = findAll(row, "dl-row-head")[0];

    expect(findAll(row, "dl-model")).toHaveLength(1);
    expect(findAll(head, "dl-model")).toHaveLength(0);
    // The title is intact in the head — not competing with the model for it.
    expect(text(findAll(head, "dl-title")[0])).toBe(
      "update picture to be ghibli style with more colour",
    );
    // The head still keeps its protected controls.
    expect(findAll(head, "dl-pct")).toHaveLength(1);
    expect(findAll(head, "dl-row-cancel")).toHaveLength(1);
  });

  it("still suppresses the model when it would just repeat the title", () => {
    const load: Job = {
      ...BASE,
      id: "sys:ai-load:x",
      title: "mlx-community/FLUX.2-Klein-4B-4bit",
      model: "mlx-community/FLUX.2-Klein-4B-4bit",
      state: "running",
      stalled: false,
    };
    const tree = renderCard([load]);
    expect(findAll(tree, "dl-model")).toHaveLength(0);
  });

  // The failure mode the coordinator flagged for the relayout: a SHORT title
  // must not leave a ragged hole. The head is only as tall as its content, so
  // a short row draws no model line at all and no empty second line.
  it("adds no extra line for a short title with no model", () => {
    const short: Job = {
      ...BASE,
      id: "sys:ai-image:short",
      title: "Resize",
      model: "",
      state: "running",
      stalled: false,
    };
    const tree = renderCard([short]);
    const row = findAll(tree, "dl-row")[0];
    expect(findAll(row, "dl-model")).toHaveLength(0);
    expect(text(findAll(row, "dl-title")[0])).toBe("Resize");
  });

  it("closes the panel's rows list to horizontal scroll — a row must fit, not scroll sideways", () => {
    // User, with a screenshot: "the notification cards should not be
    // scrollable" — the panel had scrolled SIDEWAYS to reach the clipped
    // Cancel button, because `overflow-y: auto` alone computes `overflow-x:
    // auto` too (CSS overflow's own "visible becomes auto" rule). Vertical
    // scroll stays — a long job list must still not push its own header
    // off-screen — only the horizontal axis is closed.
    const rows = block(CSS, ".dl-rows");
    expect(rows).toContain("overflow-y: auto;");
    expect(rows).toContain("overflow-x: hidden;");
  });
});

// ============================================================================
// THE ACTIVITY MERGE — the Engines section (status-bar merge). Models made
// this same trip and then moved back out into its own chip
// (`shell/ModelsDock.tsx`, resurrected) in a follow-up revision — its own
// tests live in `shell/ModelsDock.test.tsx` again, not here.
//
// No EnginesDock test file existed pre-merge: `EnginesCardView` is gone as
// its own chip, and the row rendering it owned (`EngineRow`/`engineLabel`)
// moved into this file's own component. What carries over is the row
// structure/behaviour, now exercised through the `engines` prop below.
// ============================================================================

const runningEngine = (over: Partial<RunningEngine> = {}): RunningEngine => ({
  engine_id: "e1",
  pid: 1,
  version: "",
  folder: "",
  module: "",
  uptime_s: 0,
  idle_timeout_s: 0,
  idle_for_s: 0,
  busy: false,
  ...over,
});

function renderActivity(props: {
  reported?: Job[];
  engines?: EnginesSlot;
}): ReactTestRendererJSON | null {
  return create(
    <DownloadManagerView
      reported={props.reported ?? []}
      engines={props.engines}
      initialCollapsed={false}
      refresh={() => {}}
      patch={() => {}}
    />,
  ).toJSON() as ReactTestRendererJSON | null;
}

describe("the Background tasks section (moved off EnginesDock's own chip)", () => {
  test("a running engine keeps the chip un-muted but draws no numeral and no progress line", () => {
    const tree = renderActivity({ engines: { engines: [runningEngine()], onStop: async () => {} } });
    expect(toggleClasses(tree)).not.toContain("is-idle");
    // A running engine is persistent STATE, not work in progress — it un-mutes
    // the chip (this is not "nothing") but is not counted and draws no line,
    // same rule the old dot had for it.
    expect(numeral(tree)).toBeNull();
    expect(findAll(tree, "sc-progress")).toHaveLength(0);
    expect(findAll(tree, "dl-toggle")[0].props["aria-label"]).toBe("Activity, 1 background task");
  });

  test("draws the engine's label, with a Stop button", () => {
    const tree = renderActivity({
      engines: {
        engines: [runningEngine({ engine_id: "e2", folder: "/apps/geotiff" })],
        onStop: async () => {},
      },
    });
    const row = findAll(tree, "dl-row")[0];
    expect(text(findAll(row, "dl-title")[0])).toBe("geotiff");
    expect(text(findAll(row, "dl-row-cancel")[0])).toBe("Stop");
  });

  test("the row says what the daemon is, how long it has been up, and when it retires", () => {
    const tree = renderActivity({
      engines: {
        engines: [
          runningEngine({
            folder: "/apps/geotiff",
            module: "compute.py",
            uptime_s: 725,
            idle_timeout_s: 900,
            idle_for_s: 120,
          }),
        ],
        onStop: async () => {},
      },
    });
    expect(text(findAll(findAll(tree, "dl-row")[0], "dl-status")[0])).toBe(
      "Warm worker · up 12m · retires in 13m if idle",
    );
  });

  test("a resident daemon says so rather than drawing a countdown it will never run", () => {
    // The user's own ask: a timeout is mentioned when there is one, and its
    // ABSENCE is stated too — that is what separates a daemon meant to stay
    // up from one that has been left behind.
    expect(engineDetail(runningEngine({ folder: "/apps/s3-browser", uptime_s: 90 }))).toBe(
      "Background app · up 1m · no idle timeout",
    );
  });

  test("a busy daemon explains the stopped countdown instead of freezing one", () => {
    expect(
      engineDetail(
        runningEngine({
          folder: "/apps/x", module: "m.py", uptime_s: 30,
          idle_timeout_s: 900, idle_for_s: 0, busy: true,
        }),
      ),
    ).toBe("Warm worker · up 30s · in use · idle timeout 15m");
  });

  test("a child already past its timeout reads as retiring, never as a negative countdown", () => {
    expect(
      engineDetail(
        runningEngine({
          folder: "/apps/x", module: "m.py", uptime_s: 3600,
          idle_timeout_s: 900, idle_for_s: 1000,
        }),
      ),
    ).toBe("Warm worker · up 1h · retiring now");
  });

  test("a template engine is named as one — it has no folder to label it", () => {
    expect(engineKind(runningEngine({ engine_id: "map" }))).toBe("template");
    expect(engineDetail(runningEngine({ engine_id: "map", uptime_s: 7565 }))).toBe(
      "Template engine · up 2h 6m · no idle timeout",
    );
  });

  test("a background child with a module is a worker even with no folder recorded", () => {
    // `ensure_background(..., folder="")` defaults `folder`, so a `main =`
    // child can carry a `module` with an empty `folder` — exactly the case
    // `engineLabel` above already falls back to the module for. A template
    // child can never carry a `module` (`ensure()` never sets one), so
    // checking `module` first can never mislabel a template as a worker.
    expect(engineKind(runningEngine({ folder: "", module: "widget.main" }))).toBe("worker");
  });

  test("durations step up a unit rather than counting seconds the poll cannot see", () => {
    expect(engineDuration(0)).toBe("0s");
    expect(engineDuration(59.9)).toBe("59s");
    expect(engineDuration(60)).toBe("1m");
    expect(engineDuration(3600)).toBe("1h");
    expect(engineDuration(-5)).toBe("0s");
  });

  test("a failed Stop replaces the detail line rather than stacking under it", async () => {
    const tree = create(
      <DownloadManagerView
        reported={[]}
        engines={{
          engines: [runningEngine({ folder: "/apps/x" })],
          onStop: async () => {
            throw new Error("no");
          },
        }}
        initialCollapsed={false}
        refresh={() => {}}
        patch={() => {}}
      />,
    );
    await act(async () => {
      findAll(tree.toJSON() as ReactTestRendererJSON, "dl-row-cancel")[0].props.onClick();
    });
    const rows = findAll(tree.toJSON() as ReactTestRendererJSON, "dl-status");
    expect(rows).toHaveLength(1);
    expect(text(rows[0])).toContain("Could not stop");
  });

  test("a fresh poll clears a stuck failure instead of hiding the row's detail forever", async () => {
    // Rows are keyed `key={e.engine_id}` (never remounted while an engine
    // stays up), so `EngineRow`'s own `failure` state survives every 10s
    // poll on its own. A Stop that fails once (the server briefly down) must
    // not paint over the kind/uptime/retire line for the rest of the panel's
    // life once connectivity returns — the next snapshot arriving is exactly
    // the signal that it has.
    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(
        <DownloadManagerView
          reported={[]}
          engines={{
            engines: [runningEngine({ folder: "/apps/x" })],
            onStop: async () => {
              throw new Error("no");
            },
          }}
          initialCollapsed={false}
          refresh={() => {}}
          patch={() => {}}
        />,
      );
    });
    await act(async () => {
      findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-row-cancel")[0].props.onClick();
    });
    expect(text(findAll(renderer!.toJSON() as ReactTestRendererJSON, "dl-status")[0])).toContain(
      "Could not stop",
    );

    // A new poll hands down a fresh (structurally equal but newly-fetched)
    // engine snapshot — connectivity is back, and the row should say so.
    act(() => {
      renderer!.update(
        <DownloadManagerView
          reported={[]}
          engines={{
            engines: [runningEngine({ folder: "/apps/x" })],
            onStop: async () => {},
          }}
          initialCollapsed={false}
          refresh={() => {}}
          patch={() => {}}
        />,
      );
    });
    const status = text(findAll(renderer!.toJSON() as ReactTestRendererJSON, "dl-status")[0]);
    expect(status).not.toContain("Could not stop");
    expect(status).toContain("Background app");
  });

  test("the row carries no wire field beyond what RunningEngine declares", () => {
    // Guards against the class of defect where a fixture supplies a field
    // (`kind`, say) the server no longer sends and the panel silently
    // depends on it: `runningEngine()` has no `as RunningEngine` escape
    // hatch, so an extra property here is a real excess-property error at
    // build time, not just a missing assertion.
    const engine = runningEngine();
    expect(Object.keys(engine).sort()).toEqual(
      [
        "engine_id", "folder", "module", "pid", "version",
        "uptime_s", "idle_timeout_s", "idle_for_s", "busy",
      ].sort(),
    );
  });

  test("falls back to the module when a background engine has no folder recorded", () => {
    expect(engineLabel(runningEngine({ module: "compute.py" }))).toBe(
      "compute.py",
    );
  });

  test("pressing Stop calls onStop with the engine id", async () => {
    const seen: string[] = [];
    const tree = create(
      <DownloadManagerView
        reported={[]}
        engines={{ engines: [runningEngine({ engine_id: "e3" })], onStop: async (id) => { seen.push(id); } }}
        initialCollapsed={false}
        refresh={() => {}}
        patch={() => {}}
      />,
    );
    const button = findAll(tree.toJSON() as ReactTestRendererJSON, "dl-row-cancel")[0];
    await act(async () => {
      (button.props as { onClick: () => void }).onClick();
    });
    expect(seen).toEqual(["e3"]);
  });
});

describe("two sections sharing one Activity panel", () => {
  test("a section heading renders only when 2+ sections are present", () => {
    // ONE non-empty source (engines only): no heading needed to disambiguate.
    const one = renderActivity({
      engines: { engines: [runningEngine()], onStop: async () => {} },
    });
    expect(findAll(one, "dl-section-head")).toHaveLength(0);

    // TWO non-empty sources (running + engines): both get a heading.
    const running: Job = { ...BASE, id: "sys:ai-image:live", state: "running", stalled: false };
    const two = renderActivity({
      reported: [running],
      engines: { engines: [runningEngine()], onStop: async () => {} },
    });
    const heads = findAll(two, "dl-section-head").map(text);
    expect(heads).toEqual(["Running", "Background tasks"]);
  });

  test("everything empty draws the single 'No activity' empty state, nothing else", () => {
    const tree = renderActivity({
      engines: { engines: [], onStop: async () => {} },
    });
    expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No activity");
    expect(findAll(tree, "dl-section")).toHaveLength(0);
    expect((findAll(tree, "dl-toggle")[0].props.className as string).split(" ")).toContain(
      "is-idle",
    );
  });

  test("an engine arriving opens nothing — same as a job arrival (D673, no auto-open for anything)", () => {
    const renderer = create(
      <DownloadManagerView
        reported={[]}
        engines={{ engines: [], onStop: async () => {} }}
        ready
        refresh={() => {}}
        patch={() => {}}
      />,
    );
    expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-panel")).toHaveLength(0);
    act(() => {
      renderer.update(
        <DownloadManagerView
          reported={[]}
          engines={{ engines: [runningEngine()], onStop: async () => {} }}
          ready
          refresh={() => {}}
          patch={() => {}}
        />,
      );
    });
    // Still collapsed — an engine coming up is not news, and neither is a job
    // arriving any more: nothing ever opens this panel but its own click.
    expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-panel")).toHaveLength(0);
  });
});

// ------------------------------------------------------- the chip's aria-label (D673)
describe("the chip's accessible name names what it is announcing", () => {
  test("idle: nothing running", () => {
    const tree = renderCard([]);
    expect(findAll(tree, "dl-toggle")[0].props["aria-label"]).toBe("Activity, nothing running");
  });

  test("one live job: names the job by its title", () => {
    const running: Job = { ...BASE, id: "sys:ai-image:live", title: "a red fox", state: "running" };
    const tree = renderCard([running]);
    expect(findAll(tree, "dl-toggle")[0].props["aria-label"]).toBe("Activity: a red fox");
  });

  test("2+ jobs: a running count, not a list of titles", () => {
    const a: Job = { ...BASE, id: "sys:ai-image:a", state: "running" };
    const b: Job = { ...BASE, id: "sys:ai-image:b", state: "running" };
    const tree = renderCard([a, b]);
    expect(findAll(tree, "dl-toggle")[0].props["aria-label"]).toBe("Activity, 2 running");
  });
});

// ---------------------------------------------- a "waiting" job is live too (D673)
// `jobTypeLabel` special-cases `state: "waiting"` to the word "Waiting" — a
// job parked on a question is not running, but it is still the one thing
// this machine is doing, so it counts as the chip's lone LIVE job exactly
// like a running one does.
test("a lone WAITING job takes the chip's label and counts as the one live job", () => {
  const waiting: Job = { ...BASE, id: "sys:ai-image:w", state: "waiting", message: "waiting for you" };
  const tree = renderCard([waiting]);
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Waiting");
  expect(numeral(tree)).toBeNull();
  expect(toggleClasses(tree)).not.toContain("is-idle");
});

// ---------------------------------------------- 2+ jobs: mean of running fractions
test("2+ jobs draws the numeral and a fill that is the MEAN of the running fractions", () => {
  const a: Job = { ...BASE, id: "sys:ai-image:a", state: "running", done: 2, total: 4 }; // 50%
  const b: Job = { ...BASE, id: "sys:ai-image:b", state: "running", done: 8, total: 10 }; // 80%
  const tree = renderCard([a, b]);
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Activity");
  expect(numeral(tree)).toBe("2");
  expect(progressFillWidth(tree)).toBe("65%");
});
