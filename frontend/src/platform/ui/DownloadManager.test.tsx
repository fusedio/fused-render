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
  memoryBand,
  type EnginesSlot,
  type ModelsSlot,
  type QueueSlot,
} from "@platform/ui/DownloadManager";
import { jobAmount, type Job } from "@platform/lib/jobs";
import type { AiLoadedModel, RunningEngine } from "@platform/lib/api";

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
};


/** D588: one circle per chip — outlined when the section holds nothing, filled
 *  (`.is-on`) when it holds something. Through a helper so both states are
 *  always checked as one element's two forms. */
function circleFilled(tree: ReactTestRendererJSON | null): boolean {
  const dots = findAll(tree, "dl-dot");
  expect(dots).toHaveLength(1);
  return ((dots[0].props.className as string) ?? "").split(" ").includes("is-on");
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

function fullQueue(queue: Partial<QueueSlot>): QueueSlot {
  return {
    waiting: 0,
    running: 0,
    rows: null,
    drawn: [],
    ...queue,
  };
}

function renderCardWithQueue(
  reported: Job[],
  queue: Partial<QueueSlot>,
): ReactTestRendererJSON | null {
  return create(
    <DownloadManagerView
      reported={reported}
      queue={fullQueue(queue)}
      initialCollapsed={false}
      refresh={() => {}}
      patch={() => {}}
    />,
  ).toJSON() as ReactTestRendererJSON | null;
}

// For tests that need to actually PRESS the toggle (collapsing is real
// component state, not a prop) rather than just inspect one static render.
function renderInstance(reported: Job[], queue?: Partial<QueueSlot>): ReactTestRenderer {
  return create(
    <DownloadManagerView
      reported={reported}
      queue={queue ? fullQueue(queue) : undefined}
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
  // `Activity` (status-bar merge) — this chip also carries the Models and
  // Background-tasks sections since Models/Engines folded into it; see
  // DownloadManagerView's own header for why the narrower `Jobs` (D579) no
  // longer names everything it shows.
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Activity");
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No activity");
  // D588: one circle, outlined because this section holds nothing. No count
  // element survives anywhere in the bar.
  expect(findAll(tree, "dl-count")).toHaveLength(0);
  expect(circleFilled(tree)).toBe(false);
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

test("a scheduled run's own terminal row still counts and draws — the exemption is for stand-ins, not for AI rows", () => {
  // A scheduled run's own outcome row (`sys:schedule:*`) deliberately does
  // NOT vanish on success like an ordinary AI row does — "a run appears,
  // works, and vanishes mid-sentence" is the bug that exists to prevent.
  // `isVanishedOnSuccess` must not blanket every "done" job or it would
  // re-break that.
  const scheduleDone: Job = { ...BASE, id: "sys:schedule:entry-1", title: "Nightly digest" };
  const tree = renderCard([scheduleDone]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(1);
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

// D604, THE BOUNDARY for the Jobs footer, both directions. `Clear` is
// dismiss-all, so at exactly one terminal row it duplicates that row's own
// dismiss — and the band cost 32px of an 88px card to say so.
// `queue-dock-lib.ts`'s `showCancelAll` already required two withdrawable rows
// for the same reason, so this aligns the sibling control with a rule the
// codebase had already settled.
test("Clear is absent at one clearable row and present at two", () => {
  const done = (id: string): Job => ({ ...BASE, id, state: "done", detail: "Saved" });
  // `sys:schedule:*` survives success where an ordinary AI row vanishes, which
  // is what makes it usable as a terminal row that actually draws.
  const one = renderCard([done("sys:schedule:a")]);
  expect(findAll(one, "dl-row")).toHaveLength(1);
  expect(findAll(one, "dl-head")).toHaveLength(0);
  expect(findAll(one, "dl-clear")).toHaveLength(0);
  // The row's own dismiss is what covers the single case.
  expect(findAll(one, "dl-x")).toHaveLength(1);

  const two = renderCard([done("sys:schedule:a"), done("sys:schedule:b")]);
  expect(findAll(two, "dl-head")).toHaveLength(1);
  expect(findAll(two, "dl-clear")).toHaveLength(1);
});

// `queue?.cancelAll` is a SEPARATE control with its own threshold, decided by
// the shell (`showCancelAll`, already >= 2). The band renders if EITHER guard
// is true, so a cancelAll node must still bring the footer up on its own even
// with no clearable row at all.
test("a cancelAll node alone still brings the footer up", () => {
  const cancelAll = <button className="q-all">Cancel queued</button>;
  const running: Job = { ...BASE, id: "sys:ai-image:live", state: "running", stalled: false };
  const tree = renderCardWithQueue([running], { waiting: 2, cancelAll });
  expect(findAll(tree, "dl-head")).toHaveLength(1);
  expect(findAll(tree, "q-all")).toHaveLength(1);
  // ...and no Clear beside it, since nothing is clearable.
  expect(findAll(tree, "dl-clear")).toHaveLength(0);
});

test("the panel's head is OMITTED, not a blank band, when nothing to offer — code review finding #3", () => {
  // One job running, nothing queued, nothing terminal: `queue?.cancelAll`
  // is undefined (no queue slot at all here) and `clearable` is 0, so
  // before this fix `.dl-head` still rendered — an empty ~30px padded band
  // over the row list, since the header used to always hold at least the
  // toggle before the chip/panel split moved the toggle out of it.
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
test("a failed job is drawn NOWHERE in this section — not a row, not the count, no tint", () => {
  const errored: Job = { ...BASE, id: "sys:ai-image:errored", state: "error", message: "boom" };
  const tree = renderCard([errored]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(0);
  // The circle answers what this section actually holds, so a lone failure
  // leaves it OUTLINED. A circle that still filled for a failure would be the
  // same bug the count version had.
  expect(circleFilled(tree)).toBe(false);
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No activity");
  const toggle = findAll(tree, "dl-toggle")[0];
  expect((toggle.props.className as string).split(" ")).not.toContain("is-failure");
});

test("a failure beside live work leaves only the live row and a count of one", () => {
  const running: Job = { ...BASE, id: "sys:ai-image:live", state: "running", stalled: false };
  const errored: Job = { ...BASE, id: "sys:ai-image:errored", state: "error", message: "boom" };
  const tree = renderCard([running, errored]);
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  expect(circleFilled(tree)).toBe(true);
});

test("cancelled and done rows do NOT move — only failures did", () => {
  // The scope of D586 is `state === "error"` and nothing else: a cancel is
  // user-initiated and ages out on its own, and a success has its artefact on
  // disk. `sys:schedule:*` survives success where an ordinary AI row vanishes,
  // which is what makes it usable as the "still here" case.
  const done: Job = { ...BASE, id: "sys:schedule:entry-1", title: "Nightly digest" };
  const tree = renderCard([done]);
  expect(findAll(tree, "dl-row")).toHaveLength(1);
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

test("the toggle is a real button even with only queue rows to fold", () => {
  const tree = renderCardWithQueue([], { waiting: 1, running: 0 });
  expect(tree).not.toBeNull();
  const toggles = findAll(tree, "dl-toggle");
  expect(toggles).toHaveLength(1);
  expect(toggles[0].type).toBe("button");
});

test("the toggle is a real button even with only a lone scheduled stand-in row", () => {
  const liveSchedule: Job = {
    ...BASE,
    id: "sys:schedule:entry-1",
    state: "running",
    stalled: false,
  };
  const tree = renderCard([liveSchedule]);
  expect(tree).not.toBeNull();
  const toggles = findAll(tree, "dl-toggle");
  expect(toggles).toHaveLength(1);
  expect(toggles[0].type).toBe("button");
});

test("collapsing hides every row — queue rows and job rows alike, no exemption", () => {
  const running: Job = { ...BASE, id: "sys:ai-image:running", state: "running", stalled: false };
  const renderer = renderInstance([running], {
    waiting: 0,
    running: 0,
    rows: <div className="q-row">a queued message</div>,
  });

  const before = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(before, "dl-row")).toHaveLength(1);
  expect(findAll(before, "q-row")).toHaveLength(1);

  clickToggle(renderer);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-row")).toHaveLength(0);
  expect(findAll(after, "q-row")).toHaveLength(0);
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

// D581 REMOVES the aggregate percentage from the chip (it appeared and
// disappeared in place, shifting the whole bar, and reserving ~4ch for it
// permanently would leave obvious dead space in a 22px bar). It was already
// the third telling of the same fact: the panel draws a percentage AND a
// progress bar on every row, which is where per-job progress belongs.
test("the collapsed chip carries NO aggregate percentage — per-row progress lives in the panel", () => {
  const running: Job = {
    ...BASE,
    id: "sys:ai-image:running",
    state: "running",
    done: 5,
    total: 10,
    stalled: false,
  };
  const renderer = renderInstance([running]);

  // Expanded: the row itself still carries both the percentage and the bar.
  const expanded = renderer.toJSON() as ReactTestRendererJSON;
  const row = findAll(expanded, "dl-row")[0];
  expect(text(findAll(row, "dl-pct")[0])).toBe("50%");
  expect(findAll(row, "dl-bar")).toHaveLength(1);

  clickToggle(renderer); // collapse

  // Collapsed: the chip is all that is left, and it holds neither. Its one
  // circle is the same element and the same width in both states (D588), so
  // there is nothing left in this chip that can change width at all.
  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-bar")).toHaveLength(0);
  expect(findAll(after, "dl-pct")).toHaveLength(0);
  expect(circleFilled(after)).toBe(true);
});

test("Cancel queued renders only inside the expanded panel — the collapsed chip carries no controls (D563)", () => {
  // The old behaviour this replaces (D562) dropped `showCancelAll`'s
  // threshold to one row once collapsed, because the header — and this
  // button with it — stayed on screen folded. Now the button is a plain,
  // pre-decided node (queue-dock-lib.ts `showCancelAll` no longer takes
  // `collapsed` at all) that this card only ever places inside the panel,
  // which does not exist while collapsed.
  const cancelAll = <button className="q-all">Cancel queued</button>;
  const renderer = renderInstance([], { waiting: 1, running: 0, cancelAll });

  const before = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(before, "q-all")).toHaveLength(1);

  clickToggle(renderer); // collapse

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "q-all")).toHaveLength(0);
});

// ------------------------------------- a quiet dot on a new arrival (D567)
//
// "we can make the notifications 'un collapse' when a new one comes" (D562
// follow-up) USED TO force the panel open here — code review finding #4
// caught that this recreates the exact complaint the whole status-bar
// redesign exists to fix: a background job popping a floating panel over
// whatever page the user is looking at, uninvited, and PERSISTING the
// expansion to localStorage so it survives a reload. `useAutoExpandOnNew`
// no longer touches `collapsed` at all (its own doc has the full
// reasoning) — it only answers whether something arrived unacknowledged,
// drawn here as `.dl-new-dot`. The shared decision (`trackSeenIds`) is
// tested on its own in jobs.test.ts; these pin the CARD actually wiring it
// in.

function updateInstance(renderer: ReactTestRenderer, reported: Job[], queue?: Partial<QueueSlot>) {
  act(() => {
    renderer.update(
      <DownloadManagerView
        reported={reported}
        queue={queue ? fullQueue(queue) : undefined}
        refresh={() => {}}
        patch={() => {}}
      />,
    );
  });
}

// D574 REVERSES D567 (user: "when we have something new, always show the
// notification. don't keep no activity displayed") — a new job arriving into
// a collapsed section OPENS that section's panel, and the dot is suppressed
// while it is open, since a dot pointing at a panel the user is already
// looking at announces nothing.
test("a genuinely new job id arriving OPENS the collapsed panel, and shows no dot beside it", () => {
  const first: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  const renderer = renderInstance([first]);
  clickToggle(renderer); // collapse

  const collapsed = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(collapsed, "dl-row")).toHaveLength(0);
  expect(findAll(collapsed, "dl-new-dot")).toHaveLength(0);

  const second: Job = { ...BASE, id: "sys:ai-image:b", state: "running", stalled: false };
  updateInstance(renderer, [first, second]);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-row")).toHaveLength(2);
  expect(findAll(after, "dl-new-dot")).toHaveLength(0);
});

test("the chip's own click dismisses an auto-opened panel, leaving no dot behind", () => {
  const first: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  const renderer = renderInstance([first]);
  clickToggle(renderer); // collapse
  const second: Job = { ...BASE, id: "sys:ai-image:b", state: "running", stalled: false };
  updateInstance(renderer, [first, second]);
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-row")).toHaveLength(2);

  clickToggle(renderer); // dismiss the auto-opened panel

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-row")).toHaveLength(0);
  expect(findAll(after, "dl-new-dot")).toHaveLength(0);
});

// D580, the mirror of the above (user: "after a job finishes, ensure we close
// the jobs popover if no jobs left"): the list draining to empty closes the
// panel, so an auto-opened section cannot be left sitting on screen showing
// `No activity`. Fires only on the non-empty -> empty EDGE.
test("the list draining to empty closes the panel instead of leaving it showing 'No activity'", () => {
  const job: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  const renderer = renderInstance([job]);
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-panel")).toHaveLength(1);

  updateInstance(renderer, []); // the job finished and was cleared

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(0);
  // The chip itself stays — the bar's sections are always present (D565) —
  // and reads its idle label.
  expect(text(findAll(after, "dl-summary")[0])).toBe("Activity");
  expect(circleFilled(after)).toBe(false);
});

// D584 REVIEW FINDING 1, the regression this branch shipped and this test
// exists to keep out: the arrival branch used to gate on the PERSISTED
// `collapsed` rather than on effective visibility. On a default install there
// is no stored key, so `collapsed === false` — and once a drain had set the
// transient `"closed"` override, the next arrival matched neither `collapsed`
// nor anything that clears the override. The section went permanently deaf:
// no panel and no dot for the rest of the session. The previous test stopped
// at the drain, which is exactly why this went unnoticed, so this one polls a
// NEW job in afterwards.
test("a new job AFTER a drain re-opens the panel — a closed section never goes deaf", () => {
  const first: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  const renderer = renderInstance([first]);
  updateInstance(renderer, []); // drains -> auto-closes
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-panel")).toHaveLength(0);

  const second: Job = { ...BASE, id: "sys:ai-image:b", state: "running", stalled: false };
  updateInstance(renderer, [second]);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(1);
  expect(findAll(after, "dl-row")).toHaveLength(1);
});

// The same defect's other reachable shape: a section force-closed by D582's
// one-panel-at-a-time arbiter (not by a drain) must also still hear the next
// arrival. Both paths set the identical `"closed"` override, so this pins that
// the fix is in the override handling rather than special-cased to draining.
test("a section closed while EMPTY still auto-opens on its first arrival", () => {
  const renderer = renderInstance([]); // starts idle, panel open by default
  const job: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  updateInstance(renderer, [job]);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(1);
  expect(findAll(after, "dl-row")).toHaveLength(1);
});

// CODE REVIEW 2026-08-28, FINDING 1 — the drain gate was fed the JOB ids only,
// but this panel draws the scheduled queue's rows above them. So one live
// scheduled message plus one download meant the download finishing took the
// hook's list to `[]` and slammed the panel shut over the queue rows still
// rendered inside it — including that live turn's only ✕, the one control the
// user needs at exactly that moment. `queue.drawn` now goes in as `alsoDrawn`,
// so "empty" means the panel is empty rather than one of its two sources being.
test("a job draining does NOT close the panel while the queue is still drawing rows", () => {
  const job: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  const queue = {
    waiting: 0,
    running: 1,
    drawn: ["entry-1"],
    rows: <div className="q-row">a live turn</div>,
  };
  const renderer = renderInstance([job], queue);
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "q-row")).toHaveLength(1);

  updateInstance(renderer, [], queue); // the download finished; the turn has not

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(1);
  expect(findAll(after, "q-row")).toHaveLength(1);
});

// The mirror, so the fix above cannot be "never close again": once the queue
// half lets go too, the panel does close — whichever of the two sources emptied
// last.
test("the panel closes once the queue rows drain as well", () => {
  const job: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  const queue = {
    waiting: 0,
    running: 1,
    drawn: ["entry-1"],
    rows: <div className="q-row">a live turn</div>,
  };
  const renderer = renderInstance([job], queue);
  updateInstance(renderer, [], queue); // ids drain, panel stays (above)
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-panel")).toHaveLength(1);

  updateInstance(renderer, [], { waiting: 0, running: 0, drawn: [], rows: null });

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(0);
});

test("one job finishing while another still runs closes nothing", () => {
  const a: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  const b: Job = { ...BASE, id: "sys:ai-image:b", state: "running", stalled: false };
  const renderer = renderInstance([a, b]);
  updateInstance(renderer, [b]);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(1);
  expect(findAll(after, "dl-row")).toHaveLength(1);
});

test("collapsing, then an EXISTING job merely changing, sets no dot", () => {
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

  // Same id, progress ticking (and even finishing) — not a new arrival.
  updateInstance(renderer, [{ ...job, done: 9, state: "running" }]);
  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-row")).toHaveLength(0);
  expect(findAll(after, "dl-new-dot")).toHaveLength(0);
});

test("re-collapsing while the same ids are still present sets no dot either", () => {
  // Rule: an id already in the seen set never re-triggers.
  const job: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  const renderer = renderInstance([job]);
  updateInstance(renderer, [job]); // a poll re-reports the same id
  clickToggle(renderer); // collapse

  updateInstance(renderer, [job]); // another poll, still the same id
  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-row")).toHaveLength(0);
  expect(findAll(after, "dl-new-dot")).toHaveLength(0);
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
// THE ACTIVITY MERGE — Models and Engines sections (status-bar merge).
//
// Moved from the now-deleted `shell/ModelsDock.test.tsx` (no EnginesDock test
// existed): `ModelsCardView`/`EnginesCardView` are gone as their own chips,
// and the row rendering they owned (`ModelRow`/`MemoryCell`/`memoryBand`,
// `EngineRow`/`engineLabel`) moved into this file's own component. The
// standalone-chip assertions those files made (their own toggle/idle/circle)
// no longer describe anything real, so they are not reproduced here — see
// DECISIONS.md. What DOES carry over: the pure `memoryBand` rule and the row
// structure/behaviour tests, now exercised through the `models`/`engines`
// props below.
// ============================================================================

const aiModel = (over: Partial<AiLoadedModel> = {}): AiLoadedModel => ({
  model: "mlx-community/Qwen3-8B-MLX-4bit",
  capability: "text",
  runner: "mlx",
  state: "ready",
  detail: null,
  error: null,
  residentBytes: 4_000_000_000,
  osFootprintBytes: 4_000_000_000,
  footprintBytes: null,
  footprintBasis: null,
  device: "mps",
  loadedAt: 0,
  startedAt: 0,
  jobId: "",
  idleSeconds: 0,
  unloadsInSeconds: null,
  ...over,
});

const runningEngine = (over: Partial<RunningEngine> = {}): RunningEngine => ({
  engine_id: "e1",
  kind: "template",
  pid: 1,
  version: "",
  folder: "",
  module: "",
  ...over,
} as RunningEngine);

function renderActivity(props: {
  reported?: Job[];
  queue?: Partial<QueueSlot>;
  engines?: EnginesSlot;
  models?: ModelsSlot;
}): ReactTestRendererJSON | null {
  return create(
    <DownloadManagerView
      reported={props.reported ?? []}
      queue={props.queue ? fullQueue(props.queue) : undefined}
      engines={props.engines}
      models={props.models}
      initialCollapsed={false}
      refresh={() => {}}
      patch={() => {}}
    />,
  ).toJSON() as ReactTestRendererJSON | null;
}

describe("the Models section (moved off ModelsDock's own chip)", () => {
  test("no models loaded is not what makes the panel idle by itself — an empty models slot alongside no jobs still shows 'No activity'", () => {
    const tree = renderActivity({ models: { models: [], ceilingBytes: null, onUnload: async () => {} } });
    expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No activity");
  });

  test("a resident model keeps the chip un-muted (idle=false) but its dot unfilled — resident state is not 'work right now'", () => {
    const tree = renderActivity({
      models: { models: [aiModel()], ceilingBytes: null, onUnload: async () => {} },
    });
    expect((findAll(tree, "dl-toggle")[0].props.className as string).split(" ")).not.toContain(
      "is-idle",
    );
    expect(circleFilled(tree)).toBe(false);
  });

  test("a running job alongside a resident model still fills the dot — from the job, not the model", () => {
    const running: Job = { ...BASE, id: "sys:ai-image:live", state: "running", stalled: false };
    const tree = renderActivity({
      reported: [running],
      models: { models: [aiModel()], ceilingBytes: null, onUnload: async () => {} },
    });
    expect(circleFilled(tree)).toBe(true);
  });

  test("expanded draws one row per model — its name, its memory figures, an Unload button, no gauge", () => {
    const tree = renderActivity({
      models: {
        models: [aiModel({ model: "mlx-community/Qwen3-8B-MLX-4bit", residentBytes: null, osFootprintBytes: 4_200_000_000 })],
        ceilingBytes: null,
        onUnload: async () => {},
      },
    });
    const row = findAll(tree, "dl-row")[0];
    expect(text(findAll(row, "dl-title")[0])).toBe("Qwen3-8B-MLX-4bit");
    expect(findAll(row, "dl-title")[0].props.title).toBe("mlx-community/Qwen3-8B-MLX-4bit");
    expect(text(findAll(row, "dl-amount")[0])).toBe("3.9 GB held");
    expect(text(findAll(row, "dl-row-cancel")[0])).toBe("Unload");
    expect(findAll(tree, "dl-bar")).toHaveLength(0);
  });

  test("pressing Unload calls onUnload with the model id and shows Unloading… mid-flight", async () => {
    const pending: Array<() => void> = [];
    const seen: string[] = [];
    const onUnload = (id: string) => {
      seen.push(id);
      return new Promise<void>((resolve) => pending.push(resolve));
    };
    const renderer = create(
      <DownloadManagerView
        reported={[]}
        models={{ models: [aiModel()], ceilingBytes: null, onUnload }}
        initialCollapsed={false}
        refresh={() => {}}
        patch={() => {}}
      />,
    );
    const before = renderer.toJSON() as ReactTestRendererJSON;
    const button = findAll(before, "dl-row-cancel")[0];
    act(() => {
      (button.props as { onClick: () => void }).onClick();
    });
    expect(seen).toEqual(["mlx-community/Qwen3-8B-MLX-4bit"]);
    const mid = renderer.toJSON() as ReactTestRendererJSON;
    expect(text(findAll(mid, "dl-row-cancel")[0])).toBe("Unloading…");
    await act(async () => {
      pending.pop()?.();
    });
  });

  // The memory-band rule itself, at the unit — D594/D600's argument (see the
  // (former) ModelsDock.tsx history) still holds: the HELD figure, never the
  // raw footprint, is what gets banded.
  test("memoryBand: easy/tight/no against the ceiling, null with nothing to judge", () => {
    expect(memoryBand(500_000_000, 25_769_803_776)).toBe("easy");
    expect(memoryBand(20_000_000_000, 25_769_803_776)).toBe("tight");
    expect(memoryBand(30_000_000_000, 25_769_803_776)).toBe("no");
    expect(memoryBand(null, 25_769_803_776)).toBe(null);
    expect(memoryBand(500_000_000, null)).toBe(null);
  });
});

describe("the Background tasks section (moved off EnginesDock's own chip)", () => {
  test("a running engine keeps the chip un-muted but its dot unfilled", () => {
    const tree = renderActivity({ engines: { engines: [runningEngine()], onStop: async () => {} } });
    expect((findAll(tree, "dl-toggle")[0].props.className as string).split(" ")).not.toContain(
      "is-idle",
    );
    expect(circleFilled(tree)).toBe(false);
  });

  test("draws the engine's label and kind, with a Stop button", () => {
    const tree = renderActivity({
      engines: {
        engines: [runningEngine({ engine_id: "e2", kind: "background", folder: "/apps/geotiff" })],
        onStop: async () => {},
      },
    });
    const row = findAll(tree, "dl-row")[0];
    expect(text(findAll(row, "dl-title")[0])).toBe("geotiff");
    expect(text(findAll(row, "dl-amount")[0])).toBe("background");
    expect(text(findAll(row, "dl-row-cancel")[0])).toBe("Stop");
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

describe("three sections sharing one Activity panel", () => {
  test("a section heading renders only when 2+ sections are present", () => {
    // ONE non-empty source (models only): no heading needed to disambiguate.
    const one = renderActivity({
      models: { models: [aiModel()], ceilingBytes: null, onUnload: async () => {} },
    });
    expect(findAll(one, "dl-section-head")).toHaveLength(0);

    // TWO non-empty sources (models + engines): both get a heading.
    const two = renderActivity({
      models: { models: [aiModel()], ceilingBytes: null, onUnload: async () => {} },
      engines: { engines: [runningEngine()], onStop: async () => {} },
    });
    const heads = findAll(two, "dl-section-head").map(text);
    expect(heads).toEqual(["Background tasks", "Models"]);
  });

  test("Running, Background tasks, then Models — in that order — when all three are present", () => {
    const running: Job = { ...BASE, id: "sys:ai-image:live", state: "running", stalled: false };
    const tree = renderActivity({
      reported: [running],
      engines: { engines: [runningEngine()], onStop: async () => {} },
      models: { models: [aiModel()], ceilingBytes: null, onUnload: async () => {} },
    });
    const heads = findAll(tree, "dl-section-head").map(text);
    expect(heads).toEqual(["Running", "Background tasks", "Models"]);
  });

  test("everything empty draws the single 'No activity' empty state, nothing else", () => {
    const tree = renderActivity({
      engines: { engines: [], onStop: async () => {} },
      models: { models: [], ceilingBytes: null, onUnload: async () => {} },
    });
    expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No activity");
    expect(findAll(tree, "dl-section")).toHaveLength(0);
    expect((findAll(tree, "dl-toggle")[0].props.className as string).split(" ")).toContain(
      "is-idle",
    );
  });

  test("a model or engine arriving does not open the panel — only a job arrival does", () => {
    // Mirrors the (deleted) Models/Engines chips' own `neverOpen` contract,
    // now expressed through Activity's `alsoDrawn` wiring.
    const renderer = create(
      <DownloadManagerView
        reported={[]}
        models={{ models: [], ceilingBytes: null, onUnload: async () => {} }}
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
          models={{ models: [aiModel()], ceilingBytes: null, onUnload: async () => {} }}
          ready
          refresh={() => {}}
          patch={() => {}}
        />,
      );
    });
    // Still collapsed — a model becoming resident is not news.
    expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-panel")).toHaveLength(0);
  });
});
