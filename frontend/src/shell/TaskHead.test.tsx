// THE SHARED TASK HEAD (TaskHead.tsx; design.md "One task head, everywhere"):
// one row in one order, one lead corner, chevrons that stop at the ends, a
// static title for a head with no task, and the measured ladder — Open folds
// first, the title stacks last.
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

test("one row, in the canonical order: lead, chevrons, identity, place, chips, seats, Open, ⋮", () => {
  const r = render(
    head({
      nav: { prev: true, next: true, step: () => {} },
      place: createElement("span", { className: "my-place" }, "app"),
      chips: createElement("span", { className: "my-chip" }, "#12"),
      seats: createElement("span", { className: "my-seat" }, "cam"),
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
  const order = [
    at((n) => n.props["aria-label"] === "Close the task panel"),
    at((n) => n.props["aria-label"] === "Previous task"),
    at((n) => n.props["aria-label"] === "Next task"),
    at((n) => cls(n).includes("task-side-peek-who")),
    at((n) => cls(n) === "task-head-place"),
    at((n) => cls(n) === "my-chip"),
    at((n) => cls(n) === "my-seat"),
    at((n) => cls(n) === "task-head-open"),
    at((n) => cls(n) === "my-kebab"),
  ];
  expect(order).toEqual([...order].sort((a, b) => a - b));
  // The identity is the full block — ring, number, title — with the star seat.
  expect(JSON.stringify(r.toJSON())).toContain("T042");
  expect(JSON.stringify(r.toJSON())).toContain("Rename the pane noun");
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

test("the ladder: wide, nothing folds; narrow, it walks every rung and the identity takes row 2", () => {
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
  const narrow = render(head(props), 100);
  // Chips, seats, chevrons, the identity's row, the number: all five rungs.
  expect(headNode(narrow).props["data-fit"]).toBe(5);
  expect(cls(headNode(narrow))).toContain("is-stacked");
  expect(seen[seen.length - 1]).toBe(5);
  // ↗ Open never folds: it stands beside the ⋮ at every width.
  expect(foldedOpen(0)).toBe(false);
  expect(foldedOpen(5)).toBe(false);
  for (const r of [wide, narrow]) expect(byClass(r, "task-head-open")).toHaveLength(1);
  // Row 1 is [lead][↑↓ seat][place] … [Open][⋮]: no identity on it, and the
  // place right after the lead's cluster.
  const row1 = byClass(narrow, "task-head-row")[0]!;
  const kids = (row1.children ?? []) as ReactTestRendererJSON[];
  expect(kids.map(cls)).toEqual(["task-head-acts", "task-head-place", "task-head-idle", "task-head-marks"]);
  expect(JSON.stringify(row1)).not.toContain("task-side-peek-who");
  // Row 2 is the WHOLE identity: ring, number, title.
  const row2 = byClass(narrow, "task-head-titlerow")[0]!;
  expect(JSON.stringify(row2)).toContain("Rename the pane noun");
  expect(JSON.stringify(row2)).toContain("T042");
  expect(JSON.stringify(row2)).toContain("task-side-peek-status");
  // Unstacked, the place trails the identity.
  const wideKids = ((byClass(wide, "task-head-row")[0]!.children ?? []) as ReactTestRendererJSON[]).map(cls);
  expect(wideKids.indexOf("task-head-place")).toBeGreaterThan(
    wideKids.findIndex((c) => c.startsWith("task-side-peek-who")),
  );
});

test("the chevrons share one seat, so one rung folds them; the lead is not in it", () => {
  const r = render(head({ nav: { prev: true, next: true, step: () => {} } }));
  const nav = byClass(r, "task-head-nav")[0]!;
  const labels = ((nav.children ?? []) as ReactTestRendererJSON[]).map((b) => b.props["aria-label"]);
  expect(labels).toEqual(["Previous task", "Next task"]);
  expect(JSON.stringify(nav)).not.toContain("Close the task panel");
});

test("stacked: true pins the identity's row at any width; false never stacks and stops at rung 3", () => {
  const pinned = render(head({ stacked: true }), 2000);
  expect(cls(headNode(pinned))).toContain("is-stacked");
  expect(byClass(pinned, "task-head-titlerow")).toHaveLength(1);
  expect(headNode(pinned).props["data-fit"]).toBe(0);

  const never = render(head({ stacked: false }), 100);
  expect(headNode(never).props["data-fit"]).toBe(3);
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

test("the ladder: chips, seats, chevrons, the identity's row, the number — never Open or the place", async () => {
  const { TASK_HEAD_DROPS } = await import("./row-fit");
  expect([...TASK_HEAD_DROPS]).toEqual([
    ".task-head-chips",
    ".task-head-seats",
    ".task-head-nav",
    ".task-head-title-seat",
    ".task-side-peek-id",
  ]);
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
  expect(JSON.stringify(byClass(r, "task-head-open")[0])).not.toContain("lucide-arrow-up-right");
  const withArrow = render(head({ open: { href: "/x", label: "Open", onOpen: () => {} } }));
  expect(JSON.stringify(byClass(withArrow, "task-head-open")[0])).toContain("lucide-arrow-up-right");
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
