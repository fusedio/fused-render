// What the sidebar's Tasks entry says about the page behind it, and how it stops
// saying it.
//
// Two halves, both here: the DERIVATION (tasks-lib.tasksPulse and the dismissal
// it is gated on — pure, so it is exercised directly) and the WIRING (which mark
// each collapse state draws, where the numbers come from, and the CSS that the
// count chip is shared rather than approximated). The second half is read out of
// the source, the way the rest of this suite reads claims a DOM-less test cannot
// otherwise hold.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Task } from "@platform/lib/api";
import {
  EMPTY_TASKS_PULSE,
  TASKS_SEEN_KEY,
  isDoneUnread,
  parseTasksSeen,
  pulseTitle,
  runningLabel,
  sameSeen,
  samePulse,
  seenAfterVisit,
  tasksPulse,
} from "./tasks-lib";
import type { TasksSeen } from "./tasks-lib";

const SHELL = new URL(".", import.meta.url).pathname;
const SIDEBAR = readFileSync(join(SHELL, "GlobalSidebar.tsx"), "utf8");
const STORE = readFileSync(join(SHELL, "tasksPulse.ts"), "utf8");
const FRAME = readFileSync(
  join(SHELL, "../platform/ui/sidebar/SidebarFrame.tsx"),
  "utf8",
);
const BOOKMARKS = readFileSync(
  join(SHELL, "../apps/explorer/sidebar/BookmarksSection.tsx"),
  "utf8",
);
const SCHEDULED = readFileSync(join(SHELL, "Scheduled.tsx"), "utf8");
const SIDEBAR_CSS = readFileSync(join(SHELL, "../styles/sidebar.css"), "utf8");

function task(over: Partial<Task> = {}): Task {
  return {
    key: "sess-1",
    task_id: "TASK-001",
    project: "/Users/x/proj",
    target: "/Users/x/proj",
    session_id: "sess-1",
    title: "pull the news",
    title_source: "user",
    description: "",
    status: "done",
    failed: false,
    live: false,
    unread: 0,
    last_active: 1_000,
    message_count: 1,
    messages: [],
    ...over,
  } as Task;
}

describe("the sidebar's tasks pulse", () => {
  it("counts what is running and what finished unread, and nothing else", () => {
    const tasks = [
      task({ key: "a", status: "in_progress" }),
      task({ key: "b", status: "done", unread: 2 }),
      // Read: the completion has been looked at, so there is nothing to send
      // anybody to the page for.
      task({ key: "c", status: "done", unread: 0 }),
      // Not a completion at all.
      task({ key: "d", status: "upcoming" }),
      task({ key: "e", status: "archived", unread: 3 }),
    ];
    expect(tasksPulse(tasks, {})).toEqual({ running: 1, doneUnread: 1 });
    expect(tasksPulse([], {})).toEqual(EMPTY_TASKS_PULSE);
  });

  it("does not paint a FAILED run green", () => {
    // Failed is a status of its own on this page (taskColumn), and the green mark
    // means "work you were waiting on is ready". A broken run wearing the done
    // hue would be the one place in the app where a colour disagreed with the
    // ring the row itself draws (design-principles §1).
    const broke = [task({ key: "f", status: "failed", unread: 1 })];
    expect(tasksPulse(broke, {})).toEqual({ running: 0, doneUnread: 0 });
  });

  it("clears on a visit, and only for the completions that visit showed", () => {
    const finished = task({ key: "b", status: "done", unread: 1, last_active: 500 });
    const running = task({ key: "a", status: "in_progress" });
    const before = [running, finished];
    expect(tasksPulse(before, {}).doneUnread).toBe(1);

    // Landing on /tasks stamps every DONE task with the completion on screen.
    const seen = seenAfterVisit(before);
    expect(seen).toEqual({ b: 500 });
    expect(tasksPulse(before, seen).doneUnread).toBe(0);
    // Still nothing after a poll that changes nothing — the whole point of
    // persisting the stamp rather than a "dismissed" flag on the session.
    expect(tasksPulse([...before], seen).doneUnread).toBe(0);

    // The RUNNING task completing is a new completion: it was never stamped,
    // because its completion had not happened when the reader was there.
    const settled = [task({ key: "a", status: "done", unread: 1 }), finished];
    expect(tasksPulse(settled, seen).doneUnread).toBe(1);

    // And so is the SAME task running again and finishing again: `last_active`
    // moves, so the stamp no longer describes what is on screen. A bare set of
    // dismissed keys would have swallowed this one forever.
    const again = [task({ key: "b", status: "done", unread: 1, last_active: 900 })];
    expect(tasksPulse(again, seen).doneUnread).toBe(1);
    expect(isDoneUnread(again[0], seen)).toBe(true);
  });

  it("prunes the dismissal to the tasks in the answer", () => {
    // The row is written to localStorage on every visit, so it must not grow by
    // one key per task the machine has ever had.
    const seen = seenAfterVisit([task({ key: "b", status: "done" })]);
    expect(Object.keys(seenAfterVisit([task({ key: "z", status: "done" })]))).toEqual(["z"]);
    expect(seen).not.toHaveProperty("z");
    // A running task is deliberately NOT pre-stamped: stamping a completion that
    // has not happened is how the one mark this feature exists for is never drawn.
    expect(seenAfterVisit([task({ key: "a", status: "in_progress" })])).toEqual({});
  });

  it("reads a hand-edited or ancient store as 'nothing dismissed'", () => {
    // One extra dot is the failure mode; a throw inside a render is not.
    for (const raw of [null, "", "not json", "[]", '"x"', "7"]) {
      expect(parseTasksSeen(raw)).toEqual({});
    }
    // Unusable VALUES are dropped one by one rather than costing the whole row.
    expect(parseTasksSeen('{"a": 5, "b": "no", "c": null}')).toEqual({ a: 5 });
    expect(TASKS_SEEN_KEY.startsWith("fused-render:")).toBe(true);
  });

  it("compares pulses and dismissals by value", () => {
    // The store publishes only on a CHANGED pair, and the sidebar's own
    // mark-seen effect runs on every published pulse — value equality is what
    // keeps that from looping.
    expect(samePulse({ running: 1, doneUnread: 0 }, { running: 1, doneUnread: 0 })).toBe(true);
    expect(samePulse({ running: 1, doneUnread: 0 }, { running: 1, doneUnread: 2 })).toBe(false);
    const a: TasksSeen = { x: 1, y: 2 };
    expect(sameSeen(a, { y: 2, x: 1 })).toBe(true);
    expect(sameSeen(a, { x: 1 })).toBe(false);
    expect(sameSeen(a, { x: 1, y: 3 })).toBe(false);
  });

  it("says the same sentence in both collapse states", () => {
    expect(runningLabel(1)).toBe("1 running");
    expect(pulseTitle({ running: 2, doneUnread: 1 })).toBe("2 running · 1 finished, not read");
    expect(pulseTitle({ running: 0, doneUnread: 3 })).toBe("3 finished, not read");
    expect(pulseTitle(EMPTY_TASKS_PULSE)).toBe("");
  });
});

describe("one poll behind both readers", () => {
  it("has the page publish its own rows instead of a second poll", () => {
    // Two polls of /api/tasks would be two answers, and the sidebar would show a
    // dot the page disagreed with for up to twenty seconds at a time.
    expect(SCHEDULED).toContain("publishTasks(r.tasks ?? [])");
    expect(SIDEBAR).toContain("useTasksPulse()");
    expect(SIDEBAR).not.toContain("getTasks(");
    // Polling belongs to the subscribers: it starts with the first reader and
    // stops with the last, like aiRuntime's.
    expect(STORE).toContain("listeners.add(setCurrent)");
    expect(STORE).toContain("listeners.delete(setCurrent)");
    expect(STORE).toMatch(/if \(listeners\.size === 0\)/);
    // Cadence follows the state, and idle is slower than the page's own 20s.
    expect(STORE).toContain("pulse.running > 0 ? ACTIVE_MS : IDLE_MS");
    expect(STORE).toContain("const IDLE_MS = 30_000");
  });

  it("keeps the route in the sidebar and the storage in the store", () => {
    // A store that reads location.pathname is a store that has to be told when
    // the pathname changed. The sidebar owns "the reader is on /tasks".
    // Read past the prose, which names the rule it is obeying.
    const code = STORE.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
    expect(code).not.toContain("location.pathname");
    expect(SIDEBAR).toContain("if (tasksActive) markTasksSeen();");
    // And the count is not drawn at all while that page is the one on screen —
    // the dismissal needs a poll to land, and a chip that flashes beside the row
    // of the open page and then clears itself is worse than no chip.
    expect(SIDEBAR).toContain("doneUnread: tasksActive ? 0 : pulse.doneUnread");
    // localStorage is only ever touched inside a try — a blocked store costs the
    // dismissal, never the sidebar (the same rule Scheduled.tsx's view memory
    // follows).
    expect(STORE).toContain("localStorage.getItem(TASKS_SEEN_KEY)");
    expect(STORE).toContain("localStorage.setItem(TASKS_SEEN_KEY");
    expect((STORE.match(/try \{/g) ?? []).length).toBeGreaterThanOrEqual(3);
  });
});

describe("the Tasks entry's two marks", () => {
  it("draws ONE dot on the collapsed rail, yellow winning over green", () => {
    // The rail has no label, so the whole signal is a dot on the icon — and it is
    // one dot: two in a corner is not a state this can draw, and "something is
    // running" is the fact that outranks "something is ready".
    expect(SIDEBAR).toContain("sidebar-rail-dot is-running");
    expect(SIDEBAR).toContain("sidebar-rail-dot is-unread");
    expect(SIDEBAR.indexOf("shown.running > 0 ? (")).toBeLessThan(
      SIDEBAR.indexOf("shown.doneUnread > 0 ? ("),
    );
    // Nothing at all when there is nothing to say.
    expect(SIDEBAR).toContain("badge: tasksDot");
    expect(FRAME).toContain("{item.badge}");
    // Positioned against the rail button, which is what makes an absolutely
    // placed dot land on the glyph instead of resolving against the viewport (the
    // bug account.css records for the Settings dot).
    expect(SIDEBAR_CSS).toMatch(/\.sidebar-rail-btn \{[^}]*position: relative/);
    const dot = SIDEBAR_CSS.slice(SIDEBAR_CSS.indexOf(".sidebar-rail-dot {"));
    expect(dot.slice(0, dot.indexOf("}"))).toContain("position: absolute");
  });

  it("draws NO dot when expanded — words instead, in the same hues", () => {
    // A dot on the icon plus a readout beside the label is one fact stated twice.
    const trailing = SIDEBAR.slice(
      SIDEBAR.indexOf("const tasksTrailing ="),
      SIDEBAR.indexOf("// Everything that is not primary nav"),
    );
    expect(trailing).not.toContain("sidebar-rail-dot");
    expect(trailing).toContain("runningLabel(shown.running)");
    expect(trailing).toContain("{shown.doneUnread}");
    expect(SIDEBAR).toContain("trailing={tasksTrailing}");
    expect(FRAME).toContain('<span className="sidebar-item-trail">{trailing}</span>');
    // Both marks name the state in the status ring's own tokens, so the sidebar
    // and the page cannot describe one status in two colours.
    expect(SIDEBAR_CSS).toContain("background: var(--status-progress)");
    expect(SIDEBAR_CSS).toContain("background: var(--status-done)");
    expect(SIDEBAR_CSS).toMatch(/\.sidebar-running \{[^}]*color: var\(--status-progress\)/);
  });

  it("wears the bookmark folder's count chip rather than a lookalike", () => {
    // The folder row's nested count is where this shape was settled. The skin is
    // stated ONCE and all three counts wear it; a second hand-tuned 10.5px pill is
    // how two counts in one sidebar end up half a pixel and one shade apart.
    const chip = SIDEBAR_CSS.slice(SIDEBAR_CSS.indexOf(".sidebar-count-chip {"));
    const body = chip.slice(0, chip.indexOf("}"));
    for (const decl of [
      "font-size: 10.5px",
      "color: var(--fg-muted)",
      "background: rgba(var(--tint), 0.07)",
      "border-radius: 8px",
      "padding: 3px 6px",
    ]) {
      expect(body).toContain(decl);
    }
    expect(BOOKMARKS).toContain('className="sidebar-count-chip folder-count"');
    expect(BOOKMARKS).toContain('className="sidebar-count-chip recents-count"');
    expect(SIDEBAR).toContain('className="sidebar-count-chip"');
    // And the folder row's own class keeps ONLY what is peculiar to it — its
    // placement over the hover actions and the fade that hands them the slot.
    const folder = SIDEBAR_CSS.slice(SIDEBAR_CSS.indexOf(".folder-count {"));
    const folderBody = folder.slice(0, folder.indexOf("}"));
    expect(folderBody).toContain("position: absolute");
    expect(folderBody).not.toContain("font-size");
    expect(folderBody).not.toContain("background:");
  });

  it("shimmers while work is in flight, and states it flatly when motion is off", () => {
    // The animation is load-bearing: it is what separates "2 running" as a live
    // readout from a number that might be stale.
    expect(SIDEBAR_CSS).toContain("animation: sidebar-running-shimmer");
    expect(SIDEBAR_CSS).toContain("@keyframes sidebar-running-shimmer");
    expect(SIDEBAR_CSS).toContain("background-clip: text");
    // The blanket reduced-motion rule runs an animation ONCE at 0.01ms, which
    // would park this gradient mid-travel and leave the words half-faded — a
    // dimmed readout reads as stale, the opposite of what it says. So the
    // gradient is dropped and the ink is the flat status hue.
    const rm = SIDEBAR_CSS.slice(SIDEBAR_CSS.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(rm).toContain(".sidebar-running");
    expect(rm).toContain("animation: none");
    expect(rm).toContain("background-image: none");
    expect(rm).toContain("-webkit-text-fill-color: var(--status-progress)");
  });
});
