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

import { DownloadManagerView, type QueueSlot } from "@platform/ui/DownloadManager";
import type { Job } from "@platform/lib/jobs";

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

function renderCard(reported: Job[]): ReactTestRendererJSON | null {
  return create(
    <DownloadManagerView reported={reported} refresh={() => {}} patch={() => {}} />,
  ).toJSON() as ReactTestRendererJSON | null;
}

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
  // D579: `Activity` -> `Jobs` (user: "what about jobs?") — this codebase's
  // own word for exactly this set (`fused_render/jobs.py`, `/api/jobs`).
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Jobs");
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No jobs");
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
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No jobs");
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
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No jobs");
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
// survives a collapse any more. What DOES survive is what the chip itself
// draws: `jobsSummary` names what is hidden, and `dl-pct` keeps naming the
// aggregate percentage (replacing the old collapsed-state progress bar,
// which had no home once the header shrank to one line).

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
// `No jobs`. Fires only on the non-empty -> empty EDGE.
test("the list draining to empty closes the panel instead of leaving it showing 'No jobs'", () => {
  const job: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  const renderer = renderInstance([job]);
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-panel")).toHaveLength(1);

  updateInstance(renderer, []); // the job finished and was cleared

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-panel")).toHaveLength(0);
  // The chip itself stays — the bar's three sections are always present
  // (D565) — and reads its idle label.
  expect(text(findAll(after, "dl-summary")[0])).toBe("Jobs");
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
describe("a running row's protected controls never give up their width (D569)", () => {
  const { readFileSync } = require("node:fs") as typeof import("node:fs");
  const { join } = require("node:path") as typeof import("node:path");
  const CSS = readFileSync(join(import.meta.dir, "../../styles/notifications.css"), "utf8");

  function block(css: string, selector: string): string {
    const at = css.indexOf(selector + " {");
    expect(at).toBeGreaterThan(-1);
    return css.slice(at, css.indexOf("}", at));
  }

  // D577 (user: "the UI is not readable") INVERTS what gives way. The row had
  // rendered `Downloa…  2100000000 / 4600000000  46%  [Cancel]`: with a `0`
  // floor on the title and every other element refusing to shrink at all,
  // "the title gives way" meant it surrendered EVERYTHING before anything
  // else surrendered anything. The title now holds a readable floor and the
  // AMOUNT is what collapses — it is the most redundant thing in the row,
  // since the bar and `.dl-pct` each already say how far along the job is.
  it("gives the title a readable floor instead of letting it vanish", () => {
    const rule = block(CSS, ".dl-title");
    expect(rule).toContain("min-width: 15ch;");
    expect(rule).not.toContain("min-width: 0;");
    expect(rule).toContain("overflow: hidden;");
    expect(rule).toContain("text-overflow: ellipsis;");
    expect(rule).toContain("white-space: nowrap;");
  });

  it("still lets the model suffix and the amount shrink to nothing and ellipsise", () => {
    for (const selector of [".dl-model", ".dl-amount"]) {
      const rule = block(CSS, selector);
      expect(rule).toContain("min-width: 0;");
      expect(rule).toContain("overflow: hidden;");
      expect(rule).toContain("text-overflow: ellipsis;");
      expect(rule).toContain("white-space: nowrap;");
    }
  });

  // The ORDER is the subtle part and the whole point of D577, so it is pinned
  // explicitly rather than left implied by three separate `flex` assertions:
  // amount collapses first, then the model suffix, and the title only starts
  // giving up characters once there is nothing left of either to take.
  it("shrinks amount FIRST, then the model suffix, and the title LAST", () => {
    const shrink = (selector: string): number => {
      const m = block(CSS, selector).match(/flex:\s*\d+\s+(\d+)\s+auto;/);
      expect(m).not.toBeNull();
      return Number(m![1]);
    };
    const amount = shrink(".dl-amount");
    const model = shrink(".dl-model");
    const title = shrink(".dl-title");
    expect(amount).toBeGreaterThan(model);
    expect(model).toBeGreaterThan(title);
  });

  // D571 follow-up: proportional shrink cut BOTH identifying fields to
  // uselessness together (`Downloadin…` / `Qwen2.…`) while the protected
  // controls kept full width. The model suffix now shrinks lopsidedly
  // first — see notifications.css's own comment on `.dl-model` for the
  // "freeze and redistribute" mechanism this relies on.
  it("shrinks the model suffix FIRST — its flex-shrink dwarfs the title's", () => {
    expect(block(CSS, ".dl-title")).toContain("flex: 1 1 auto;");
    expect(block(CSS, ".dl-model")).toContain("flex: 0 999 auto;");
  });

  // D577 moved `.dl-amount` OUT of this protected group; the other three stay,
  // which is what keeps a running row's Cancel reachable rather than pushed
  // past the panel's edge (D571's goal, still standing).
  it("never lets the percentage, Cancel or dismiss give up their intrinsic width", () => {
    for (const selector of [".dl-pct", ".dl-row-cancel", ".dl-x"]) {
      expect(block(CSS, selector)).toContain("flex: 0 0 auto;");
    }
    expect(block(CSS, ".dl-amount")).not.toContain("flex: 0 0 auto;");
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
