// THE SHARED TASK HEAD (TaskHead.tsx; design.md "One task head, everywhere"):
// one row in one order, one lead corner, chevrons that stop at the ends, a
// static title for a head with no task, the three a/b slots (number ↔ Archive,
// count ↔ star, time ↔ Open; 2026-11-15) and the measured ladder — the time
// folds first, the title stacks, the id slot goes last.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { createElement, type ReactElement } from "react";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";
import type { Task } from "@platform/lib/api";

const { TaskHead, foldedOpen } = await import("./TaskHead");
type Props = Parameters<typeof TaskHead>[0];

// ---- the measuring globals row-fit reaches for -------------------------------
// Stated for every test and put back after: bun runs every suite in one
// process, and another file's stand-ins must not decide this one's widths.
const MISSING = Symbol("missing");
const g = globalThis as Record<string, unknown>;
const saved = new Map<string, unknown>();
const inert = class {
  observe() {}
  disconnect() {}
};
const STYLE = {
  display: "block",
  position: "static",
  flexGrow: "0",
  flexShrink: "0",
  minWidth: "0",
  columnGap: "0",
  paddingLeft: "0",
  paddingRight: "0",
  getPropertyValue: () => "",
};
beforeEach(() => {
  const stubs: Record<string, unknown> = {
    ResizeObserver: inert,
    MutationObserver: inert,
    getComputedStyle: (n: { __style?: unknown } | null) => n?.__style ?? STYLE,
  };
  for (const [name, stub] of Object.entries(stubs)) {
    saved.set(name, name in g ? g[name] : MISSING);
    g[name] = stub;
  }
});
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  for (const [name, prior] of saved) {
    if (prior === MISSING) delete g[name];
    else g[name] = prior;
  }
  saved.clear();
});

/** The head's node: `available` px wide, its row holding one seat that wants
 *  500px (the ladder measures the head's rows, row-fit `taskHeadNeed`). */
function headOf(available: number) {
  const seat = {
    getBoundingClientRect: () => ({ width: 500 }),
    scrollWidth: 500,
    children: [],
    textContent: "seat",
    __style: STYLE,
  };
  const row = { className: "task-head-row", children: [seat], __style: STYLE };
  return { clientWidth: available, children: [row], __style: STYLE };
}

function render(el: ReactElement, available = 2000) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(el, {
      createNodeMock: (node) =>
        String((node.props as { className?: string }).className ?? "").split(" ")[0] === "task-head"
          ? headOf(available)
          : {},
    });
  });
  mounted.push(r);
  return r;
}

const task = (over: Partial<Task> = {}): Task =>
  ({
    key: "sess-1",
    task_id: "TASK-042",
    title: "Rename the pane noun",
    project: "/repo/app",
    target: "/repo/app",
    session_id: "sess-1",
    status: "done",
    ...over,
  }) as Task;

const head = (over: Partial<Props> = {}) =>
  createElement(TaskHead, {
    lead: { kind: "close", label: "Close the task panel", hint: "Close · Esc", onPress: () => {} },
    task: task(),
    ...over,
  });

/** Every host node, depth first, in the order it is painted (and tabbed). */
function flat(r: ReactTestRenderer): ReactTestRendererJSON[] {
  const out: ReactTestRendererJSON[] = [];
  const walk = (n: ReactTestRendererJSON | ReactTestRendererJSON[] | string | null) => {
    if (!n || typeof n === "string") return;
    if (Array.isArray(n)) return n.forEach(walk);
    out.push(n);
    (n.children ?? []).forEach((c) => walk(c as ReactTestRendererJSON | string));
  };
  walk(r.toJSON());
  return out;
}
const cls = (n: ReactTestRendererJSON) => String(n.props.className ?? "");
const byClass = (r: ReactTestRenderer, c: string) => flat(r).filter((n) => cls(n).split(" ").includes(c));
const byLabel = (r: ReactTestRenderer, label: string) => flat(r).find((n) => n.props["aria-label"] === label);
const headNode = (r: ReactTestRenderer) => byClass(r, "task-head")[0]!;

test("one row, in the canonical order: lead, chevrons, status, folder, id slot, title, chips, seats, count slot, time slot, ⋮", () => {
  const r = render(
    head({
      nav: { prev: true, next: true, step: () => {} },
      place: createElement("span", { className: "my-place" }, "app"),
      chips: createElement("span", { className: "my-chip" }, "#12"),
      seats: createElement("span", { className: "my-seat" }, "cam"),
      where: createElement("span", { className: "my-where" }, "hex"),
      open: { href: "/x", label: "Open in Explorer", onOpen: () => {} },
      kebab: createElement("button", { className: "my-kebab" }, "⋮"),
    }),
  );
  const nodes = flat(r);
  const at = (pick: (n: ReactTestRendererJSON) => boolean) => {
    const i = nodes.findIndex(pick);
    expect(i).toBeGreaterThan(-1);
    return i;
  };
  const has = (n: ReactTestRendererJSON, c: string) => cls(n).split(" ").includes(c);
  const order = [
    at((n) => n.props["aria-label"] === "Close the task panel"),
    at((n) => n.props["aria-label"] === "Previous task"),
    at((n) => n.props["aria-label"] === "Next task"),
    at((n) => has(n, "task-side-peek-status")),
    at((n) => cls(n) === "task-head-place"),
    at((n) => has(n, "task-head-idslot")),
    at((n) => has(n, "task-side-peek-title")),
    at((n) => cls(n) === "my-chip"),
    at((n) => cls(n) === "my-seat"),
    // WHERE IN THE PLACE (2026-11-16): after the seats, before the count slot.
    at((n) => cls(n) === "task-head-where"),
    at((n) => has(n, "task-head-countslot")),
    at((n) => has(n, "task-head-timeslot")),
    at((n) => cls(n) === "my-kebab"),
  ];
  expect(order).toEqual([...order].sort((a, b) => a - b));
  // The block is whole — ring, number, title — in two halves on one row.
  expect(JSON.stringify(r.toJSON())).toContain("T042");
  expect(JSON.stringify(r.toJSON())).toContain("Rename the pane noun");
  const row = byClass(r, "task-head-row")[0]!;
  expect(((row.children ?? []) as ReactTestRendererJSON[]).map((k) => cls(k).split(" ")[0])).toEqual([
    "task-head-acts",
    "task-side-peek-who",
    "task-side-peek-who",
    "task-head-marks",
  ]);
});

// ---- the a/b slots (Akshil, 2026-11-15) --------------------------------------

/** A slot's two occupants: `a` (the fact, or undefined) and `b`'s children. */
function slot(r: ReactTestRenderer, name: string) {
  const box = byClass(r, name)[0];
  if (!box) return null;
  const kids = (box.children ?? []) as ReactTestRendererJSON[];
  const a = kids.find((k) => cls(k).split(" ").includes("task-head-slot-a"));
  const b = kids.find((k) => cls(k).split(" ").includes("task-head-slot-b"))!;
  return { box, a, b: (b.children ?? []) as ReactTestRendererJSON[] };
}

test("the id slot: the number at rest, Archive as its other occupant; nothing to file keeps the number alone", () => {
  const done = slot(render(head()), "task-head-idslot")!;
  expect(cls(done.a!)).toContain("task-side-peek-id");
  expect(done.a!.children).toEqual(["T042"]);
  expect(done.b.map(cls)).toEqual(["task-head-file tasks-act--archive"]);
  // A run in flight: the slot stands with the number and an empty `b`.
  const live = slot(render(head({ task: task({ status: "in_progress" }) })), "task-head-idslot")!;
  expect(live.a!.children).toEqual(["T042"]);
  expect(live.b).toEqual([]);
  // The status icon no longer carries the filing button.
  expect(JSON.stringify(byClass(render(head()), "task-side-peek-status")[0])).not.toContain("task-head-file");
});

test("the count slot: the message count at rest (the row's floor of one), the star as its other occupant", () => {
  const r = render(head({ task: task({ message_count: 4 }) }));
  const s = slot(r, "task-head-countslot")!;
  expect(s.a!.props["aria-label"]).toBe("4 messages");
  expect(s.a!.props["data-hint"]).toBe("4 messages in this task");
  // The star left the identity for this slot.
  expect(JSON.stringify(byClass(r, "task-side-peek-who"))).not.toContain("task-star");
  // No messages counted: one, as the List row prints it.
  expect(slot(render(head({ task: task({ message_count: 0 }) })), "task-head-countslot")!.a!.props["aria-label"]).toBe(
    "1 message",
  );
  // A host's own count (the rail card's, with its unread ink) and its own star.
  const own = slot(
    render(
      head({
        count: { n: 7, unread: 2 },
        star: false,
        starNode: createElement("button", { className: "host-star" }),
      }),
    ),
    "task-head-countslot",
  )!;
  expect(cls(own.a!)).toContain("is-unread");
  expect(own.a!.props["aria-label"]).toBe("7 messages, 2 unread");
  expect(own.b.map(cls)).toEqual(["host-star"]);
  // Favourites off and no host star: the count has nothing to trade with.
  expect(slot(render(head({ star: false, task: task({ message_count: 4 }) })), "task-head-countslot")!.b).toEqual([]);
  // Neither a count nor a star: no slot at all.
  expect(slot(render(head({ star: false, count: null })), "task-head-countslot")).toBeNull();
});

test("the time slot: the time at rest, ↗ Open as its other occupant with the time in its hint", () => {
  const when = { text: "16d ago", title: "Last run 10/26/2026, 12:01:08" };
  const r = render(head({ when, open: { href: "/x", label: "Open in Explorer", onOpen: () => {} } }));
  const s = slot(r, "task-head-timeslot")!;
  expect(s.a!.children).toEqual(["16d ago"]);
  expect(s.a!.props["data-hint"]).toBe("Last run 10/26/2026, 12:01:08");
  expect(s.b.map(cls)).toEqual(["task-head-open"]);
  expect(s.b[0]!.props["aria-label"]).toBe("Open in Explorer");
  expect(s.b[0]!.props["data-hint"]).toBe("Open in Explorer · Last run 10/26/2026, 12:01:08");
  // The word is its own span, so rung 1 can leave the ↗ alone.
  expect(byClass(r, "task-head-open-word")[0]!.children).toEqual(["Open"]);
  // No Open (a pane IS the page): the time stays, with nothing to trade with.
  const pane = slot(render(head({ when, open: null })), "task-head-timeslot")!;
  expect(pane.a!.children).toEqual(["16d ago"]);
  expect(pane.b).toEqual([]);
  // The task's own clock when the host states none (the List row's cell).
  const own = slot(render(head({ task: task({ last_active: 1 }) })), "task-head-timeslot")!;
  expect(String(own.a!.props["data-hint"])).toContain("Active");
  // No time and no door: no slot.
  expect(slot(render(head({ when: null, open: null })), "task-head-timeslot")).toBeNull();
});

test("the folder stands before the number: the host's place, the task's own chip, or — told null — none", () => {
  const own = render(head());
  const chip = byClass(own, "task-head-place")[0]!;
  expect(JSON.stringify(chip)).toContain("app");
  const marks = byClass(own, "is-marks")[0]!;
  const kids = ((marks.children ?? []) as ReactTestRendererJSON[]).map((k) => cls(k).split(" ")[0]);
  expect(kids.indexOf("task-head-place")).toBeGreaterThan(kids.indexOf("task-side-peek-status"));
  expect(kids.indexOf("task-head-place")).toBeLessThan(kids.indexOf("task-head-slot"));
  const host = render(head({ place: createElement("span", { className: "my-place" }) }));
  expect(byClass(host, "my-place")).toHaveLength(1);
  expect(JSON.stringify(byClass(host, "task-head-place")[0])).not.toContain("schedule-tv-id");
  expect(byClass(render(head({ place: null })), "task-head-place")).toHaveLength(0);
});

test("no reply line of its own: only a host's subtitle trails the title (2026-11-16)", () => {
  // The task's last reply is NOT drawn by the head: the panel, the Tasks
  // card, the New task chat, the panes and the Cards popup show none.
  const r = render(head({ task: task({ last_reply: "Space should now fire." }) }));
  expect(byClass(r, "task-head-subtitle")).toHaveLength(0);
  expect(JSON.stringify(r.toJSON())).not.toContain("Space should now fire.");
  // A host that passes one (the rail card's hover preview) gets it, in the
  // title's own seat.
  const host = render(
    head({ task: task({ last_reply: "theirs" }), subtitle: createElement("span", { className: "host-sub" }) }),
  );
  expect(byClass(host, "host-sub")).toHaveLength(1);
  expect(JSON.stringify(byClass(host, "is-title")[0])).toContain("task-head-subtitle");
  expect(JSON.stringify(host.toJSON())).not.toContain("theirs");
});

test("the lead corner: × with its hint, ← whose hint is its label, and a refusal said in both", () => {
  const presses: string[] = [];
  const close = render(head({ lead: { kind: "close", label: "Close", hint: "Close · Esc", onPress: () => presses.push("x") } }));
  const x = byLabel(close, "Close")!;
  expect(x.props["data-hint"]).toBe("Close · Esc");
  expect(cls(x)).toContain("is-close");
  act(() => x.props.onClick());
  expect(presses).toEqual(["x"]);

  const back = render(head({ lead: { kind: "back", label: "Back to tasks", onPress: () => {} } }));
  const b = byLabel(back, "Back to tasks")!;
  expect(b.props["data-hint"]).toBe("Back to tasks");
  expect(cls(b)).toContain("is-back");

  const locked = render(
    head({ lead: { kind: "back", label: "Back to chats", onPress: () => {}, disabled: true, reason: "Finish the notes first" } }),
  );
  const l = byLabel(locked, "Back to chats — Finish the notes first")!;
  expect(l.props.disabled).toBe(true);
  expect(l.props["data-hint"]).toBe("Finish the notes first");
});

test("the chevrons stop at the ends of the list and step by one", () => {
  const steps: number[] = [];
  const r = render(head({ nav: { prev: false, next: true, step: (d) => steps.push(d), keyHints: true } }));
  const prev = byLabel(r, "Previous task")!;
  const next = byLabel(r, "Next task")!;
  expect(prev.props.disabled).toBe(true);
  expect(next.props.disabled).toBe(false);
  expect(next.props["data-hint"]).toBe("Next task · ↓");
  act(() => next.props.onClick());
  expect(steps).toEqual([1]);
  // Without a list behind it there are no chevrons at all.
  expect(byLabel(render(head()), "Previous task")).toBeUndefined();
});

test("no task: a static title carrying the dialog's id, and the Open door's own word", () => {
  const r = render(
    head({
      task: null,
      title: "Tasks",
      titleId: "dlg-title",
      open: { href: "/tasks", label: "Open the Tasks page", word: "Open tasks", onOpen: () => {} },
    }),
  );
  const t = byClass(r, "task-head-title-static")[0]!;
  expect(t.props.id).toBe("dlg-title");
  // The heading the dialog's `h2` was, now that the head replaces it.
  expect(t.props.role).toBe("heading");
  expect(t.props["aria-level"]).toBe(2);
  expect(t.children).toEqual(["Tasks"]);
  const open = byClass(r, "task-head-open")[0]!;
  expect(open.props["aria-label"]).toBe("Open the Tasks page");
  expect(JSON.stringify(open.children)).toContain("Open tasks");
  // A head with no task has no a/b: its door stands in the row itself.
  expect(byClass(r, "task-head-timeslot")).toHaveLength(0);
  // A task's head puts the id on the TITLE span, not on the whole block.
  const withTask = render(head({ titleId: "dlg-2" }));
  const tt = byClass(withTask, "task-side-peek-title")[0]!;
  expect(tt.props.id).toBe("dlg-2");
  expect(tt.props.role).toBe("heading");
  // No dialog, no heading role: a panel head is not a document heading.
  const plain = byClass(render(head()), "task-side-peek-title")[0]!;
  expect(plain.props.role).toBeUndefined();
});

test("↗ Open is a real link: a plain press calls the host, a ⌘-press is the browser's", () => {
  const opened: string[] = [];
  const r = render(head({ open: { href: "/files/repo", label: "Open in Explorer", onOpen: (h) => void opened.push(h) } }));
  const a = byClass(r, "task-head-open")[0]!;
  expect(a.type).toBe("a");
  expect(a.props.href).toBe("/files/repo");
  let prevented = false;
  act(() => a.props.onClick({ metaKey: true, button: 0, preventDefault: () => (prevented = true) }));
  expect(opened).toEqual([]);
  expect(prevented).toBe(false);
  act(() => a.props.onClick({ button: 0, preventDefault: () => (prevented = true) }));
  expect(opened).toEqual(["/files/repo"]);
  expect(prevented).toBe(true);
});

test("the ladder: wide, nothing folds; narrow, it walks every rung and the title takes row 2", () => {
  const seen: number[] = [];
  const kebab = (fit: number) => {
    seen.push(fit);
    return createElement("button", { className: "my-kebab" }, "⋮");
  };
  const props = {
    kebab,
    nav: { prev: true, next: true, step: () => {} },
    place: createElement("span", { className: "my-place" }, "app"),
    open: { href: "/x", label: "Open in Explorer", onOpen: () => {} },
  };
  const wide = render(head(props), 2000);
  expect(headNode(wide).props["data-fit"]).toBe(0);
  expect(byClass(wide, "task-head-titlerow")).toHaveLength(0);

  seen.length = 0;
  // A rail window walks all seven and gives the title row 2…
  const narrow = render(head({ ...props, reveal: "window" }), 100);
  expect(headNode(narrow).props["data-fit"]).toBe(7);
  expect(cls(headNode(narrow))).toContain("is-stacked");
  expect(seen[seen.length - 1]).toBe(7);
  // …a panel or a pane walks the same rungs and stays one row.
  const pane = render(head(props), 100);
  expect(headNode(pane).props["data-fit"]).toBe(7);
  expect(cls(headNode(pane))).not.toContain("is-stacked");
  expect(byClass(pane, "task-head-titlerow")).toHaveLength(0);
  // ↗ Open never folds away: it is the time slot's other occupant at every width.
  expect(foldedOpen(0)).toBe(false);
  expect(foldedOpen(7)).toBe(false);
  for (const r of [wide, narrow]) expect(byClass(r, "task-head-open")).toHaveLength(1);
  // Row 1 keeps where the task lives — the folder — and the slots; the
  // ring, the title and its number are not on it (a rail window is two
  // lines, 2026-11-16).
  const row1 = byClass(narrow, "task-head-row")[0]!;
  const kids = (row1.children ?? []) as ReactTestRendererJSON[];
  expect(kids.map((k) => cls(k).split(" ")[0])).toEqual([
    "task-head-acts",
    "task-head-crumb",
    // No spacer: the slots stand at the row's end (their `margin-left: auto`).
    "task-head-marks",
  ]);
  expect(JSON.stringify(row1)).not.toContain("task-side-peek-status");
  expect(JSON.stringify(row1)).toContain("my-place");
  expect(JSON.stringify(row1)).not.toContain("Rename the pane noun");
  // THE NUMBER GOES DOWN WITH THE TITLE (2026-11-16): row 1 has no id slot…
  expect(JSON.stringify(row1)).not.toContain("T042");
  expect(JSON.stringify(row1)).not.toContain("task-head-idslot");
  // …row 2 is the ring, the id slot (the number and its Archive), then the
  // title.
  const row2 = byClass(narrow, "task-head-titlerow")[0]!;
  const two = JSON.stringify(row2);
  expect(two).toContain("Rename the pane noun");
  expect(two).toContain("task-side-peek-status");
  expect(two.indexOf("task-side-peek-status")).toBeLessThan(two.indexOf("task-head-idslot"));
  expect(two).toContain("task-head-idslot");
  expect(two.indexOf("T042")).toBeGreaterThan(-1);
  expect(two.indexOf("T042")).toBeLessThan(two.indexOf("Rename the pane noun"));
  // One row: the number is with the marks, once.
  expect(byClass(wide, "task-head-idslot")).toHaveLength(1);
  expect(byClass(narrow, "task-head-idslot")).toHaveLength(1);
});

test("the chevrons share one seat, so one rung folds them; the lead is not in it", () => {
  const r = render(head({ nav: { prev: true, next: true, step: () => {} } }));
  const nav = byClass(r, "task-head-nav")[0]!;
  const labels = ((nav.children ?? []) as ReactTestRendererJSON[]).map((b) => b.props["aria-label"]);
  expect(labels).toEqual(["Previous task", "Next task"]);
  expect(JSON.stringify(nav)).not.toContain("Close the task panel");
});

test("stacked: true pins the title's row at any width; false never stacks and stops at rung 6", () => {
  const pinned = render(head({ stacked: true }), 2000);
  expect(cls(headNode(pinned))).toContain("is-stacked");
  expect(byClass(pinned, "task-head-titlerow")).toHaveLength(1);
  expect(headNode(pinned).props["data-fit"]).toBe(0);

  const never = render(head({ stacked: false }), 100);
  expect(headNode(never).props["data-fit"]).toBe(6);
  expect(byClass(never, "task-head-titlerow")).toHaveLength(0);
  expect(cls(headNode(never))).not.toContain("is-stacked");
});

test("fitEnabled: false measures nothing and folds nothing", () => {
  const r = render(head({ fitEnabled: false }), 100);
  expect(headNode(r).props["data-fit"]).toBe(0);
});

test("a queued task wears the queued row's number and its place in the line", () => {
  const r = render(
    head({
      queue: { status: "queued", queue_position: 2, queue_ahead: "TASK-046", task_id: "TASK-050", title: "The waiting one" },
    }),
  );
  const text = JSON.stringify(r.toJSON());
  expect(byClass(r, "tasks-row-queue")).toHaveLength(1);
  expect(text).toContain("T050");
  expect(text).toContain("The waiting one");
  expect(text).toContain("TASK-046");
  // No queue: no caption.
  expect(byClass(render(head()), "tasks-row-queue")).toHaveLength(0);
});

test("the chip seat hands the chat a node to portal into, at the head of the chips", () => {
  const got: unknown[] = [];
  const r = render(
    head({
      chipsRef: (el) => got.push(el),
      chips: createElement("span", { className: "my-chip" }, "#12"),
    }),
  );
  const chips = byClass(r, "task-head-chips")[0]!;
  expect(cls(chips)).toContain("c-tokens");
  const kids = (chips.children ?? []) as ReactTestRendererJSON[];
  expect(cls(kids[0]!)).toBe("task-head-chips-host");
  expect(cls(kids[1]!)).toBe("my-chip");
  expect(got.some((el) => el !== null)).toBe(true);
});

test("the ladder: the chips one at a time, the facts, a rail window's title row, seats, chevrons, the place's cap — never the id slot", async () => {
  const { TASK_HEAD_DROPS, TASK_HEAD_FACTS_LEVEL, TASK_HEAD_STACK_LEVEL, TASK_HEAD_TITLE_ROOM, TASK_HEAD_TITLE_MAX } = await import("./row-fit");
  expect([...TASK_HEAD_DROPS]).toEqual([
    // Rung 1 also folds the where-tag's word to its icon (2026-11-16).
    ".task-head-where .place-tag-word, .task-head-chips-host",
    ".task-head-chips",
    ".task-head-time",
    ".task-head-title-seat",
    ".task-head-seats",
    ".task-head-nav",
    ".task-head-place",
  ]);
  expect(TASK_HEAD_FACTS_LEVEL).toBe(3);
  expect(TASK_HEAD_STACK_LEVEL).toBe(4);
  expect(TASK_HEAD_TITLE_ROOM).toBe(180);
  const css = await Bun.file(new URL("../styles/task-head.css", import.meta.url)).text();
  // The title claims 400px (the ladder charges it no more), the chips stand
  // beside that, and spare width goes back to the title — never past its own
  // words (2026-11-16).
  expect(TASK_HEAD_TITLE_MAX).toBe(400);
  expect(css).toContain(`  flex: 1 1 ${TASK_HEAD_TITLE_MAX}px;\n  max-width: max-content;`);
  expect(css).not.toContain(`max-width: ${TASK_HEAD_TITLE_MAX}px;`);
  // Row 2 keeps the Archive button's height with or without the button (the
  // picker closes on a scroll, and a head that changed height scrolled the chat).
  expect(css).toContain(".task-head-titlerow {\n  display: flex;\n  align-items: center;\n  min-width: 0;");
  expect(css).toMatch(/\.task-head-titlerow \{[^}]*min-height: 28px;\s*padding: 0 2px 6px 6px;/);
  // The where-tag's word: gone on every rung past 0 while the head is one
  // row; a head pinned stacked has it back until row 1 itself is short. A
  // rail window's two-line head never folds it (2026-11-16: "they show up
  // full, not collapsed").
  expect(css).toContain('.task-head:not(.is-stacked):not([data-fit="0"]) .task-head-where .place-tag-word');
  expect(css).toContain('.task-head.is-stacked:not(.is-two-line):is([data-fit="6"], [data-fit="7"]) .task-head-where .place-tag-word');
  expect(css).not.toContain('.task-head.is-stacked:is([data-fit="6"], [data-fit="7"]) .task-head-where');
  const from = (n: number) =>
    `:is(${[1, 2, 3, 4, 5, 6, 7].filter((k) => k >= n).map((k) => `[data-fit="${k}"]`).join(", ")})`;
  // The chips give first, one at a time: the worktree pill, then the PR chip.
  expect(css).toContain(`.task-head${from(1)} .task-head-chips-host {`);
  expect(css).toContain(`.task-head${from(2)} .task-head-chips {`);
  const facts = ":is(.task-head-countslot, .task-head-timeslot)";
  // IN A RAIL WINDOW NOTHING MOVES (2026-11-16): the facts never fold there
  // and no slot collapses — every seat holds its box at rest and under the
  // pointer, so the where-tag and the title do not jump.
  expect(css).not.toContain(`.task-head.is-window:not(.is-stacked)${from(3)} ${facts} {\n  width: 0;`);
  expect(css).not.toContain(".task-head.is-window .task-head-kebab {\n  width: 0;");
  // A panel or a pane gives the time alone; the count stays.
  expect(css).toContain(`.task-head:not(.is-window)${from(3)} .task-head-timeslot {\n  width: 0;`);
  // Short on width, a panel's or a pane's door is its ↗ alone; a rail
  // window's two-line head keeps the word at every width (2026-11-16) — only
  // a window head that is not two lines (no task) folds it, from rung 5.
  expect(css).toContain(`.task-head:not(.is-window)${from(3)} .task-head-timeslot .task-head-open-word,`);
  expect(css).toContain(`.task-head.is-window:not(.is-two-line)${from(5)} :is(.task-head-facts, .task-head-timeslot) .task-head-open-word {`);
  expect(css).not.toContain(`.task-head.is-window${from(5)} :is(.task-head-facts, .task-head-timeslot)`);
  // A rail window's ⋮ always stands (2026-11-16): no rule hides it or holds
  // its seat open, and nothing is moved over it at rest.
  expect(css).not.toContain(".task-head.is-window .task-head-kebab {");
  expect(css).not.toContain("translateX(");
  expect(css).toContain(`.task-head${from(5)} .task-head-seats {`);
  expect(css).toContain(`.task-head${from(6)} .task-head-nav {`);
  // The last rung caps the place; the id slot (and its Archive) never goes.
  expect(css).toContain(`.task-head[data-fit="7"] .task-head-place {\n  max-width: 96px;`);
  expect(css).not.toContain(".task-head-idslot {\n  display: none;");
});

test("a panel or a pane that asks wears the rail window's two lines, keeping its own reveal (2026-11-17)", async () => {
  const props = {
    twoLine: true,
    place: createElement("span", { className: "my-place" }, "app"),
    where: createElement("span", { className: "my-where" }, "hex"),
    open: { href: "/x", label: "Open in Explorer", onOpen: () => {} },
    kebab: createElement("button", { className: "my-kebab" }, "⋮"),
  };
  const r = render(head(props), 2000);
  expect(cls(headNode(r))).toContain("is-two-line");
  expect(cls(headNode(r))).toContain("is-stacked");
  // Still a panel: the pointer on the head is what trades the slots.
  expect(cls(headNode(r))).not.toContain("is-window");
  // ROW 1: the lead, `folder › file`, the facts as one box, the ⋮.
  const row1 = byClass(r, "task-head-row")[0]!;
  const kids = (row1.children ?? []) as ReactTestRendererJSON[];
  expect(kids.map((k) => cls(k).split(" ")[0])).toEqual(["task-head-acts", "task-head-crumb", "task-head-marks"]);
  expect(byClass(r, "task-head-where")).toHaveLength(1);
  expect(byClass(r, "task-head-facts")).toHaveLength(1);
  expect(JSON.stringify(row1)).not.toContain("Rename the pane noun");
  // ROW 2: the ring, the id slot, the title.
  const two = JSON.stringify(byClass(r, "task-head-titlerow")[0]!);
  const order = ["task-side-peek-status", "task-head-idslot", "Rename the pane noun"].map((w) => two.indexOf(w));
  expect(order.every((i) => i > -1)).toBe(true);
  expect([...order].sort((x, y) => x - y)).toEqual(order);

  // A chat with no task row yet keeps the two lines (its fallback title on
  // row 2, the place once), so the head does not grow when the task lands…
  const fallback = render(head({ ...props, task: null, title: "Claude" }), 2000);
  expect(cls(headNode(fallback))).toContain("is-two-line");
  expect(JSON.stringify(byClass(fallback, "task-head-titlerow")[0]!)).toContain("Claude");
  expect(byClass(fallback, "task-head-place")).toHaveLength(1);
  // …and a head with nothing to name (a pane's landing) stays one row.
  const landing = render(head({ twoLine: true, task: null }), 2000);
  expect(cls(headNode(landing))).not.toContain("is-two-line");
  expect(byClass(landing, "task-head-titlerow")).toHaveLength(0);

  // One act alone (favourites off: the host passes `star={false}`): the
  // facts keep a slot each, so the count stays under the pointer.
  const noStar = render(head({ ...props, star: false }), 2000);
  expect(byClass(noStar, "task-head-facts")).toHaveLength(0);
  expect(byClass(noStar, "task-head-timeslot")).toHaveLength(1);

  // THE CRUMB HOLDS ITS WIDTH in a panel (the ladder folds the chips and the
  // seats instead), is cut only on the last rung, and the time never folds.
  const css = await Bun.file(new URL("../styles/task-head.css", import.meta.url)).text();
  expect(css).toMatch(/\.task-head\.is-two-line:not\(\.is-window\) \.task-head-crumb \{\s*flex: 0 0 auto;/);
  expect(css).toMatch(/\.task-head\.is-two-line:not\(\.is-window\)\[data-fit="7"\] \.task-head-crumb > :is\(\.task-head-place, \.task-head-where\) \{\s*flex: 0 1 auto;\s*max-width: 96px;/);
  // The tools' words go first (a class, not a rung of `data-fit`), and the
  // acts start at the facts' left edge so no hole follows the last tag.
  expect(css).toMatch(/\.task-head\.is-seats-tight \.task-head-seats \.c-anncta \{\s*--c-annlbl: none;/);
  expect(css).toMatch(/\.task-head\.is-two-line \.task-head-facts-layer\.is-b \{\s*justify-self: start;/);
});

test("reveal: a rail window says so in its class; a panel or a pane does not", () => {
  expect(cls(headNode(render(head({ reveal: "window" }))))).toContain("is-window");
  expect(cls(headNode(render(head())))).not.toContain("is-window");
});

test("a rail window's task head is two lines at every width: where it lives and its facts, then the task (2026-11-16)", async () => {
  const props = {
    reveal: "window" as const,
    lead: null,
    pinNode: createElement("button", { className: "my-pin" }, "pin"),
    place: createElement("span", { className: "my-place" }, "app"),
    where: createElement("span", { className: "my-where" }, "hex"),
    chips: createElement("span", { className: "my-chip" }, "#12"),
    open: { href: "/x", label: "Open in Explorer", onOpen: () => {} },
    kebab: createElement("button", { className: "my-kebab" }, "⋮"),
  };
  // Wide — nothing has folded — and it is two lines all the same.
  const r = render(head(props), 2000);
  expect(headNode(r).props["data-fit"]).toBe(0);
  expect(cls(headNode(r))).toContain("is-two-line");
  expect(cls(headNode(r))).toContain("is-stacked");
  // ROW 1: the pin, `folder › file`, then the chips, the facts and the ⋮.
  const row1 = byClass(r, "task-head-row")[0]!;
  const kids = (row1.children ?? []) as ReactTestRendererJSON[];
  expect(kids.map((k) => cls(k).split(" ")[0])).toEqual(["task-head-acts", "task-head-crumb", "task-head-marks"]);
  expect(JSON.stringify(kids[0])).toContain("my-pin");
  const crumb = (kids[1]!.children ?? []) as ReactTestRendererJSON[];
  expect(crumb).toHaveLength(3);
  expect(cls(crumb[0]!)).toBe("task-head-place");
  expect(cls(crumb[1]!).split(" ")).toContain("task-head-crumb-sep");
  expect(cls(crumb[2]!)).toBe("task-head-where");
  expect(JSON.stringify(crumb[0])).toContain("my-place");
  expect(JSON.stringify(crumb[2])).toContain("my-where");
  // The chevron costs the ladder nothing.
  expect(crumb[1]!.props["data-fit-skip"]).toBe("");
  // The where-tag stands once — in the crumb, not at the row's right end.
  expect(byClass(r, "task-head-where")).toHaveLength(1);
  const right = JSON.stringify(kids[2]);
  expect(right).not.toContain("my-where");
  expect(right.indexOf("my-chip")).toBeLessThan(right.indexOf("task-head-facts"));
  expect(right.indexOf("task-head-facts")).toBeLessThan(right.indexOf("my-kebab"));
  const one = JSON.stringify(row1);
  for (const gone of ["task-side-peek-status", "task-head-idslot", "T042", "Rename the pane noun"]) expect(one).not.toContain(gone);
  // ROW 2: the ring, the id slot, the title — in that order, once each.
  const two = JSON.stringify(byClass(r, "task-head-titlerow")[0]!);
  const order = ["task-side-peek-status", "task-head-idslot", "Rename the pane noun"].map((w) => two.indexOf(w));
  expect(order.every((i) => i > -1)).toBe(true);
  expect([...order].sort((a, b) => a - b)).toEqual(order);
  expect(two).not.toContain("my-place");
  expect(byClass(r, "task-side-peek-status")).toHaveLength(1);
  expect(byClass(r, "task-head-idslot")).toHaveLength(1);

  // One tag alone: no chevron; none: no crumb.
  const placeOnly = render(head({ ...props, where: undefined }), 2000);
  expect(byClass(placeOnly, "task-head-crumb")).toHaveLength(1);
  expect(byClass(placeOnly, "task-head-crumb-sep")).toHaveLength(0);
  const bare = render(head({ ...props, place: null, where: undefined }), 2000);
  expect(byClass(bare, "task-head-crumb")).toHaveLength(0);

  // A window head with no task (the New task chat before its row lands) is
  // not two lines; nor is a panel's or a pane's head, at any width.
  const noTask = render(head({ reveal: "window", task: null, title: "New task" }), 2000);
  expect(cls(headNode(noTask))).toContain("is-window");
  expect(cls(headNode(noTask))).not.toContain("is-two-line");
  expect(byClass(noTask, "task-head-titlerow")).toHaveLength(0);
  const pane = render(head({ place: props.place, where: props.where }), 100);
  expect(cls(headNode(pane))).not.toContain("is-two-line");
  expect(byClass(pane, "task-head-crumb")).toHaveLength(0);

  // The crumb gives width (ellipsis) rather than folding to an icon.
  const css = await Bun.file(new URL("../styles/task-head.css", import.meta.url)).text();
  expect(css).toMatch(/\.task-head-crumb \{[^}]*flex: 0 1 auto;\s*min-width: 0;\s*overflow: hidden;/);
});

test("a group, not a banner: role=group with its name", () => {
  const r = render(head({ ariaLabel: "Chat header" }));
  const root = headNode(r);
  expect(root.type).toBe("div");
  expect(root.props.role).toBe("group");
  expect(root.props["aria-label"]).toBe("Chat header");
  expect(headNode(render(head())).props["aria-label"]).toBe("Task header");
});

test("an armed lead says so in its class; a door that stays on the page drops its ↗", () => {
  const r = render(
    head({
      lead: { kind: "close", label: "Close and discard changes", onPress: () => {}, armed: true },
      open: { href: "/x", label: "Open", arrow: false, onOpen: () => {} },
    }),
  );
  expect(cls(byLabel(r, "Close and discard changes")!)).toContain("is-armed");
  // The ↗ is always in the markup — hidden while the word stands, on every
  // door (below), and worn alone where the word has folded (task-head.css).
  expect(cls(byClass(r, "task-head-open")[0]!)).toContain("is-wordy");
  const withArrow = render(head({ open: { href: "/x", label: "Open", onOpen: () => {} } }));
  expect(JSON.stringify(byClass(withArrow, "task-head-open")[0])).toContain("lucide-arrow-up-right");
});

test("no arrow beside the word Open: the ↗ shows only where the word has folded away (2026-11-16)", async () => {
  const css = await Bun.file(new URL("../styles/task-head.css", import.meta.url)).text();
  // Every door, not only a wordy one: the arrow is hidden by default…
  expect(css).toContain(".task-head-open .task-head-open-arrow {\n  display: none;\n}");
  expect(css).not.toContain(".task-head-open.is-wordy .task-head-open-arrow");
  // …and drawn, alone and star-sized, by the ladder's fold of the word — a
  // panel's or a pane's from the facts' rung, a window head with no task's
  // from rung 5. A two-line rail head never folds the word, so never draws it.
  const fold = css.slice(css.indexOf(".task-head-timeslot .task-head-open-arrow,"));
  expect(fold.slice(0, fold.indexOf("}"))).toMatch(/\.task-head-open-arrow \{\s*display: inline;/);
  expect(css).toContain(
    '.task-head.is-window:not(.is-two-line):is([data-fit="5"], [data-fit="6"], [data-fit="7"]) :is(.task-head-facts, .task-head-timeslot) .task-head-open-arrow {\n  display: inline;',
  );
  // The fold's rule must outrank the default that hides it.
  expect(css.indexOf(".task-head-open .task-head-open-arrow {")).toBeLessThan(css.indexOf(".task-head-timeslot .task-head-open-arrow,"));
});

test("a rail window's unread count is the same muted ink as every other count (2026-11-16)", async () => {
  const css = await Bun.file(new URL("../styles/task-head.css", import.meta.url)).text();
  // The class still says unread (the hint and the label read it)…
  const r = render(head({ reveal: "window", count: { n: 5, unread: 2 } }));
  expect(cls(byClass(r, "task-head-count")[0]!)).toContain("is-unread");
  // …a panel's or a pane's count turns to ink; a rail window's does not.
  expect(css).toContain(".task-head-count.is-unread {\n  color: var(--fg);\n}");
  expect(css).toContain(".task-head.is-window .task-head-count.is-unread {\n  color: var(--fg-muted);\n}");
  expect(css.indexOf(".task-head-count.is-unread {")).toBeLessThan(css.indexOf(".task-head.is-window .task-head-count.is-unread {"));
});

test("the rename pencil takes no room: next to the title where there is space, over its end only with `is-rename-over` (2026-11-16)", async () => {
  const css = await Bun.file(new URL("../styles/task-head.css", import.meta.url)).text();
  const who = ".task-head .task-side-peek-who.is-title";
  // At rest it stands after the title and hands its 16px + the 8px gap back.
  expect(css).toMatch(
    new RegExp(`${who.replace(/\./g, "\\.")} \\.task-side-peek-rename \\{\\s*position: relative;\\s*z-index: 1;[^}]*margin-right: -24px;\\s*pointer-events: none;`),
  );
  // No room after the title: pulled back over its end.
  expect(css).toContain(`${who}.is-rename-over .task-side-peek-rename {\n  margin-left: -24px;\n  margin-right: 0;\n}`);
  // The title's end fades only under an overlaid pencil.
  expect(css).toContain(`${who}.is-rename-over:hover:has(.task-side-peek-rename) .task-side-peek-title,`);
  expect(css).toContain(`${who}.is-rename-over:has(.task-side-peek-rename:focus-visible) .task-side-peek-title {`);
  expect(css).not.toContain(`${who}:hover:has(.task-side-peek-rename) .task-side-peek-title`);
  // Touch has no pointer to measure by: always over the end, always faded.
  const touch = css.slice(css.lastIndexOf("@media (hover: none) {"));
  expect(touch).toMatch(/\.task-side-peek-rename \{[^}]*pointer-events: auto;\s*margin-left: -24px;\s*margin-right: 0;/);
  expect(touch).toContain(`${who}:has(.task-side-peek-rename) .task-side-peek-title {`);
});

test("the host's seats sit in their own measurable box", () => {
  const got: unknown[] = [];
  const r = render(head({ seats: createElement("span", { className: "my-seat" }), seatsRef: (el) => got.push(el) }));
  const box = byClass(r, "task-head-seats")[0]!;
  expect(((box.children ?? []) as ReactTestRendererJSON[]).map(cls)).toEqual(["my-seat"]);
  expect(got.some((el) => el !== null)).toBe(true);
  expect(byClass(render(head()), "task-head-seats")).toHaveLength(0);
});

test("pending with no task: the identity's skeleton holds the seat", () => {
  const r = render(head({ task: null, pending: true }));
  expect(byClass(r, "c-tb-skel")).toHaveLength(1);
  expect(byClass(r, "c-skel-dot")).toHaveLength(1);
  expect(byClass(render(head({ task: null })), "c-tb-skel")).toHaveLength(0);
});

// ---- the id slot files the task (TaskFileSeat; the Tasks row's rules) -------

const seatOf = (r: ReactTestRenderer) => byClass(r, "task-head-file")[0];

test("the id slot's Archive is there only when the task can be filed — never while a run is in flight", () => {
  const done = render(head());
  const seat = seatOf(done)!;
  expect(seat.props["aria-label"]).toBe("Archive this task");
  expect(seat.props["data-hint"]).toBe("Archive");
  // It stands in the id slot, over the number.
  expect(JSON.stringify(byClass(done, "task-head-idslot")[0])).toContain("task-head-file");
  // Archived: the other direction.
  expect(seatOf(render(head({ task: task({ status: "archived" }) })))!.props["aria-label"]).toBe(
    "Unarchive this task",
  );
  // A run in flight: no button, the ring stays.
  expect(seatOf(render(head({ task: task({ status: "in_progress" }) })))).toBeUndefined();
  // THE PAGE'S OWN CLOCK outranks a listing that has not caught up: a turn
  // that just started over a row still saying "done" has no Archive either —
  // the press would stop it. A card being answered keeps its (confirmed) one.
  expect(seatOf(render(head({ running: true })))).toBeUndefined();
  expect(seatOf(render(head({ running: true, task: task({ status: "needs_attention" }) })))).toBeDefined();
  // NO SESSION ASKED FOR: a task that never ran (an upcoming scheduled one,
  // `session_id: ""`) files by its key, as its list row does…
  const neverRan = seatOf(render(head({ task: task({ session_id: "", status: "upcoming" }) })));
  expect(neverRan!.props["aria-label"]).toBe("Archive this task");
  expect(seatOf(render(head({ task: task({ session_id: undefined } as Partial<Task>) })))).toBeDefined();
  // …but a draft (nothing filed yet), a queued line and a session-less run in
  // flight still get none.
  expect(seatOf(render(head({ task: task({ session_id: "", kind: "draft", status: "upcoming" } as Partial<Task>) })))).toBeUndefined();
  expect(seatOf(render(head({ task: task({ session_id: "", status: "queued" }) })))).toBeUndefined();
  expect(seatOf(render(head({ task: task({ session_id: "", status: "in_progress" }) })))).toBeUndefined();
  // A host that keeps its archive elsewhere turns it off.
  expect(seatOf(render(head({ fileSeat: false })))).toBeUndefined();
});

test("a host's own overlay wins over the head's Archive, in the same slot", () => {
  const r = render(head({ statusOverlay: createElement("button", { className: "host-archive" }) }));
  expect(seatOf(r)).toBeUndefined();
  expect(byClass(r, "host-archive")).toHaveLength(1);
  expect(JSON.stringify(byClass(r, "task-head-idslot")[0])).toContain("host-archive");
});

test("not while the title is being renamed: the number stays, no Archive under a half-typed title", () => {
  const r = render(head());
  expect(seatOf(r)).toBeDefined();
  const pencil = flat(r).find((n) => n.props["aria-label"] === "Rename task")!;
  act(() => pencil.props.onClick());
  expect(seatOf(r)).toBeUndefined();
});

test("a press files it, tells the host, and hands the number back until the pointer leaves", async () => {
  const realFetch = globalThis.fetch;
  const calls: string[] = [];
  (globalThis as { fetch: unknown }).fetch = async (input: unknown) => {
    calls.push(String(input));
    return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
  };
  try {
    const filed: string[] = [];
    const r = render(head({ onFiled: (k) => filed.push(k) }));
    await act(async () => {
      seatOf(r)!.props.onClick({ stopPropagation() {} });
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(calls).toContain("/api/tasks/archive");
    expect(filed).toEqual(["archive"]);
    // The spent press: the number comes back in front while the pointer stays.
    expect(cls(headNode(r))).toContain("is-refiled");
    // …still spent while the pointer moves inside the id slot…
    act(() => headNode(r).props.onMouseOver({ target: { closest: (sel: string) => (sel === ".task-head-idslot" ? {} : null) } }));
    expect(cls(headNode(r))).toContain("is-refiled");
    // …and re-armed the moment it is anywhere else on the head, or off the
    // head altogether.
    act(() => headNode(r).props.onMouseOver({ target: { closest: () => null } }));
    expect(cls(headNode(r))).not.toContain("is-refiled");
    expect(headNode(r).props.onMouseLeave).toBeUndefined();
  } finally {
    globalThis.fetch = realFetch;
  }
});

// ---- a head that walks tasks (review, PR #2230) ------------------------------

test("a rename half-typed on one task is dropped when the head walks to another", () => {
  const r = render(head());
  const pencil = flat(r).find((n) => n.props["aria-label"] === "Rename task")!;
  act(() => pencil.props.onClick());
  expect(flat(r).some((n) => n.type === "input")).toBe(true);
  act(() => r.update(head({ task: task({ key: "sess-2", session_id: "sess-2", task_id: "TASK-043", title: "Other" }) })));
  expect(flat(r).some((n) => n.type === "input")).toBe(false);
  expect(JSON.stringify(r.toJSON())).toContain("Other");
});

test("the status seat sits beside the title — on no rung — and moves to row 2 with it", () => {
  const got: unknown[] = [];
  const wide = render(head({ statusRef: (el) => got.push(el) }), 2000);
  const row1 = byClass(wide, "task-head-row")[0]!;
  expect(JSON.stringify(row1)).toContain("task-head-status-host");
  // Not inside the chips or the seats, which fold.
  expect(JSON.stringify(byClass(wide, "task-head-marks")[0] ?? null)).not.toContain("task-head-status-host");
  expect(got.some((el) => el !== null)).toBe(true);
  const narrow = render(head({ statusRef: () => {}, reveal: "window" }), 100);
  expect(JSON.stringify(byClass(narrow, "task-head-row")[0])).not.toContain("task-head-status-host");
  expect(JSON.stringify(byClass(narrow, "task-head-titlerow")[0])).toContain("task-head-status-host");
});

test("the stylesheet trades a slot's occupants on hover — the window's in a rail window, the head's elsewhere — and only where there is a `b`", async () => {
  const css = (await Bun.file(new URL("../styles/task-head.css", import.meta.url)).text()).replace(/\s+/g, " ");
  const live = ':has(> .task-head-slot-b > :is(a, button, [role="button"]))';
  // A rail window: the pointer anywhere on it (`.is-hot`). A panel or a pane:
  // the pointer on the head. Or the keyboard on a b control.
  const trigger = ".task-head:is(.is-hot, :hover, :has(.task-head-slot-b :focus-visible))";
  // The fact steps aside only for a slot holding a control…
  expect(css).toContain(`${trigger} .task-head-slot${live} > .task-head-slot-a { opacity: 0;`);
  // …and the control shows.
  expect(css).toContain(`${trigger} .task-head-slot > .task-head-slot-b { opacity: 1;`);
  // At rest the control is invisible AND intangible.
  expect(css).toContain(".task-head-slot-b { display: inline-flex; align-items: center; opacity: 0; pointer-events: none; }");
  // A slot with no fact: a rail window holds its box like every other (the
  // no-shift rule, 2026-11-16) — never collapsed; elsewhere its act stands.
  expect(css).not.toContain(
    ".task-head.is-window .task-head-slot:is(.task-head-countslot, .task-head-timeslot):not(:has(> .task-head-slot-a)) { width: 0;",
  );
  expect(css).toContain(
    ".task-head:not(.is-window) .task-head-slot:is(.task-head-countslot, .task-head-timeslot):not(:has(> .task-head-slot-a)) > .task-head-slot-b { opacity: 1;",
  );
  // IN A RAIL WINDOW THE ARCHIVE WAITS FOR THE HEAD ITSELF (2026-11-16): the
  // pointer elsewhere on the card (`.is-hot`, not `:hover`) keeps the number.
  expect(css).toContain(
    ".task-head.task-head.is-window.is-hot:not(:hover) .task-head-slot.task-head-idslot > .task-head-slot-b:not(:has(:focus-visible)) { opacity: 0;",
  );
  expect(css).toContain(
    ".task-head.task-head.task-head.is-window.is-hot:not(:hover) .task-head-slot.task-head-idslot.task-head-idslot:not(:has(:focus-visible)) > .task-head-slot-a { opacity: 1;",
  );
  // Where there is no hover, the acts stand.
  expect(css).toContain("@media (hover: none) {");
  // The status icon trades with nothing any more.
  expect(css).not.toContain(".task-side-peek-status:hover");
});

// ---- the ring is a button (TaskIconPick, 2026-11-15; any task 2026-11-16) ----

test("the status icon opens the icon picker outside a rename — a favourite's face AND any task's plain ring", async () => {
  const icons = await import("@platform/lib/task-icons-flag");
  const favs = await import("@platform/lib/favourites-flag");
  act(() => {
    icons.publishTaskIcons({ enabled: true });
    favs.publishTaskFavourites({ enabled: true });
    favs.publishFavouritesParent({ enabled: true });
  });
  try {
    const fav = render(head({ task: task({ starred: true, icon_slot: 3 } as Partial<Task>) }));
    const btn = byClass(fav, "task-icon-pick")[0]!;
    expect(btn.type).toBe("button");
    expect(btn.props["aria-label"]).toBe("Choose icon");
    expect(btn.props["aria-expanded"]).toBe(false);
    // Inside the status seat, and no title input anywhere: this is not a rename.
    expect(JSON.stringify(byClass(fav, "task-side-peek-status")[0])).toContain("task-icon-pick");
    expect(flat(fav).some((n) => n.type === "input")).toBe(false);
    // Not starred, no face (owner, 2026-11-16: the icon is not the star's):
    // the plain ring is the same button, in the same seat.
    const ring = render(head());
    const ringBtn = byClass(ring, "task-icon-pick")[0]!;
    expect(ringBtn.type).toBe("button");
    expect(JSON.stringify(byClass(ring, "task-side-peek-status")[0])).toContain("task-icon-pick");
    expect(JSON.stringify(ringBtn)).not.toContain("schedule-ring--glyph");
    // A task with no number yet has nothing to stamp a pick on: no button.
    expect(byClass(render(head({ task: task({ task_id: "" }) })), "task-icon-pick")).toHaveLength(0);
    // The experiment off: no button on either.
    act(() => icons.publishTaskIcons({ enabled: false }));
    expect(byClass(render(head({ task: task({ starred: true, icon_slot: 3 } as Partial<Task>) })), "task-icon-pick")).toHaveLength(0);
    expect(byClass(render(head()), "task-icon-pick")).toHaveLength(0);
  } finally {
    icons.resetTaskIconsFlagForTests();
    favs.resetTaskFavouritesFlagForTests();
  }
});

test("no where-tag, no seat for it (a whole folder, a file: the place alone)", () => {
  const r = render(head());
  expect(byClass(r, "task-head-where")).toHaveLength(0);
});

test("a rail window's facts and acts are two layers of one box; a panel keeps the two slots (2026-11-16)", () => {
  const props = {
    task: task({ message_count: 6 }),
    open: { href: "/x", label: "Open in Explorer", onOpen: () => {} },
  };
  const win = render(head({ ...props, reveal: "window" }));
  // One box, two layers: count + time, then star + door. No per-fact slots.
  expect(byClass(win, "task-head-facts")).toHaveLength(1);
  expect(cls(byClass(win, "task-head-facts")[0]!)).toContain("has-acts");
  expect(byClass(win, "task-head-countslot")).toHaveLength(0);
  expect(byClass(win, "task-head-timeslot")).toHaveLength(0);
  const [a, b] = byClass(win, "task-head-facts-layer");
  expect(cls(a!)).toContain("is-a");
  expect(JSON.stringify(a)).toContain("task-head-count");
  expect(JSON.stringify(a)).toContain("task-head-time");
  expect(cls(b!)).toContain("is-b");
  expect(JSON.stringify(b)).toContain("task-head-open");
  // ONE ACT ALONE (favourites off: no star): the count STAYS under the
  // pointer, so the head keeps one slot per fact — the stayer is one node
  // that cannot jump between two layers (Bugbot on #2297).
  const noStar = render(head({ ...props, reveal: "window", star: false }));
  expect(byClass(noStar, "task-head-facts")).toHaveLength(0);
  const cs = byClass(noStar, "task-head-countslot")[0]!;
  expect(JSON.stringify(cs)).toContain("task-head-count");
  expect(JSON.stringify(cs)).not.toContain("task-star");
  expect(JSON.stringify(byClass(noStar, "task-head-timeslot")[0])).toContain("task-head-open");
  expect(byClass(noStar, "task-head-count")).toHaveLength(1);
  // …and the same with a star but no door.
  const noDoor = render(head({ task: task({ message_count: 6 }), reveal: "window" }));
  expect(byClass(noDoor, "task-head-facts")).toHaveLength(0);
  expect(byClass(noDoor, "task-head-time")).toHaveLength(1);
  // No acts at all: the facts alone, and they stay under the pointer.
  const bare = render(head({ task: task({ message_count: 6 }), reveal: "window", star: false }));
  expect(cls(byClass(bare, "task-head-facts")[0]!)).not.toContain("has-acts");
  expect(byClass(bare, "task-head-facts-layer")).toHaveLength(1);
  // A panel or a pane keeps one slot per fact.
  const pane = render(head(props));
  expect(byClass(pane, "task-head-facts")).toHaveLength(0);
  expect(byClass(pane, "task-head-countslot")).toHaveLength(1);
  expect(byClass(pane, "task-head-timeslot")).toHaveLength(1);
});

test("a two-line panel folds in its own order: the tools' words, then the tags, the time, the tools (2026-11-17)", async () => {
  const { TASK_HEAD_PANEL_DROPS, TASK_HEAD_PANEL_FIT } = await import("./row-fit");
  expect([...TASK_HEAD_PANEL_DROPS]).toEqual([
    ".task-head-seats .c-lbl",
    ".task-head-chips-host",
    ".task-head-chips",
    ".task-head-time",
    ".task-head-seats",
    ".task-head-nav",
    ".task-head-place, .task-head-where",
  ]);
  // One `data-fit` per level (rung 0 included); the words' rung wears no
  // one-row fold — it is the `is-seats-tight` class — and the one-row rung 4
  // (the title's row) is never worn: the title is already on row 2.
  expect([...TASK_HEAD_PANEL_FIT]).toEqual([0, 0, 1, 2, 3, 5, 6, 7]);
  expect(TASK_HEAD_PANEL_FIT).toHaveLength(TASK_HEAD_PANEL_DROPS.length + 1);
  // A settling walkthrough's status is not a `.c-lbl`: folded, the seat hides
  // it and the mic and shows a spinner, so no words spill out of the icon.
  const css = await Bun.file(new URL("../styles/task-head.css", import.meta.url)).text();
  expect(css).toContain(
    ".chat-root .task-head.is-seats-tight .task-head-seats .c-anncta.busy .c-annrec .c-annreclbl,\n" +
      ".chat-root .task-head.is-seats-tight .task-head-seats .c-anncta.busy .c-annrec .c-rec-mic {\n  display: none;",
  );
  expect(css).toContain(".c-anncta.busy .c-annrec::before {");
  // Wide: nothing folded, no class.
  const wide = render(head({ twoLine: true }), 2000);
  expect(headNode(wide).props["data-fit"]).toBe(0);
  expect(cls(headNode(wide))).not.toContain("is-seats-tight");
  // A rail window keeps the one-row ladder and never wears the class.
  const win = render(head({ reveal: "window" }), 100);
  expect(cls(headNode(win))).not.toContain("is-seats-tight");
});
