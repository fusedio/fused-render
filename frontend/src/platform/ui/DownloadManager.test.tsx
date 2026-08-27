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
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Activity");
  expect(text(findAll(tree, "dl-panel-empty")[0])).toBe("No activity");
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

test("an error job beside a done one still draws and is clearable — only the success vanishes", () => {
  const done = { ...BASE, id: "sys:ai-image:done" };
  const errored: Job = {
    ...BASE,
    id: "sys:ai-image:errored",
    state: "error",
    message: "boom",
  };
  const tree = renderCard([done, errored]);
  expect(tree).not.toBeNull();
  // Exactly the error row — the done one drew nothing.
  expect(findAll(tree, "dl-row")).toHaveLength(1);
  // A terminal row nobody can act on but Clear (it is not running, and it is
  // the only row the card is showing) — dismissible unlike the vanished one.
  expect(findAll(tree, "dl-clear")).toHaveLength(1);
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

test("a terminal row beside a stalled running one offers Clear, counting only the terminal one", () => {
  const stalled: Job = { ...BASE, id: "sys:ai-image:stalled", state: "running", stalled: true };
  const done = { ...BASE, id: "sys:ai-image:errored", state: "error" as const, message: "boom" };
  const tree = renderCard([stalled, done]);
  expect(tree).not.toBeNull();
  expect(findAll(tree, "dl-row")).toHaveLength(2);
  expect(findAll(tree, "dl-clear")).toHaveLength(1);
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

test("everything terminal and failed colours the chip — everything terminal and clean does not", () => {
  const errored: Job = { ...BASE, id: "sys:ai-image:errored", state: "error", message: "boom" };
  const failedTree = renderCard([errored]);
  const failedToggle = findAll(failedTree, "dl-toggle")[0];
  expect((failedToggle.props.className as string).split(" ")).toContain("is-failure");

  const done: Job = { ...BASE, id: "sys:schedule:entry-1", title: "Nightly digest" };
  const cleanTree = renderCard([done]); // sys:schedule:* survives success, unlike an ordinary AI row
  const cleanToggle = findAll(cleanTree, "dl-toggle")[0];
  expect((cleanToggle.props.className as string).split(" ")).not.toContain("is-failure");
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

test("the chip keeps naming the aggregate percentage once collapsed — D563's chip replaces the old collapsed bar", () => {
  // D563 (status bar redesign): a collapsed card used to keep drawing a mini
  // aggregate progress bar directly under its header, so folding the rows
  // away hid the detail without hiding the fact that something was running.
  // That bar had no home once collapsed became a one-line chip in the status
  // bar — the numeric `.dl-pct` next to the summary carries the same fact
  // now, in the one line the chip has room for.
  const running: Job = {
    ...BASE,
    id: "sys:ai-image:running",
    state: "running",
    done: 5,
    total: 10,
    stalled: false,
  };
  const renderer = renderInstance([running]);
  clickToggle(renderer);
  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-bar")).toHaveLength(0);
  expect(text(findAll(after, "dl-pct")[0])).toBe("50%");
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

test("collapsing, then a genuinely new job id arriving, sets a quiet dot WITHOUT opening the panel", () => {
  const first: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  const renderer = renderInstance([first]);
  clickToggle(renderer); // collapse

  const collapsed = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(collapsed, "dl-row")).toHaveLength(0);
  expect(findAll(collapsed, "dl-new-dot")).toHaveLength(0);

  const second: Job = { ...BASE, id: "sys:ai-image:b", state: "running", stalled: false };
  updateInstance(renderer, [first, second]);

  const after = renderer.toJSON() as ReactTestRendererJSON;
  // Still collapsed — nothing here is allowed to reach in and reopen it.
  expect(findAll(after, "dl-row")).toHaveLength(0);
  expect(findAll(after, "dl-new-dot")).toHaveLength(1);
});

test("opening the panel — the user's own click — is what clears the dot", () => {
  const first: Job = { ...BASE, id: "sys:ai-image:a", state: "running", stalled: false };
  const renderer = renderInstance([first]);
  clickToggle(renderer); // collapse
  const second: Job = { ...BASE, id: "sys:ai-image:b", state: "running", stalled: false };
  updateInstance(renderer, [first, second]);
  expect(findAll(renderer.toJSON() as ReactTestRendererJSON, "dl-new-dot")).toHaveLength(1);

  clickToggle(renderer); // expand

  const after = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(after, "dl-new-dot")).toHaveLength(0);
  expect(findAll(after, "dl-row")).toHaveLength(2);
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

  it("lets BOTH the title and the model suffix shrink to nothing and ellipsise", () => {
    for (const selector of [".dl-title", ".dl-model"]) {
      const rule = block(CSS, selector);
      expect(rule).toContain("min-width: 0;");
      expect(rule).toContain("overflow: hidden;");
      expect(rule).toContain("text-overflow: ellipsis;");
      expect(rule).toContain("white-space: nowrap;");
    }
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

  it("never lets the amount, percentage, Cancel or dismiss give up their intrinsic width", () => {
    // A combined block: `.dl-amount, .dl-pct { flex: 0 0 auto; ... }`.
    const amountPct = CSS.slice(
      CSS.indexOf(".dl-amount,"),
      CSS.indexOf("}", CSS.indexOf(".dl-amount,")),
    );
    expect(amountPct).toContain("flex: 0 0 auto;");
    expect(block(CSS, ".dl-row-cancel")).toContain("flex: 0 0 auto;");
    expect(block(CSS, ".dl-x")).toContain("flex: 0 0 auto;");
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
