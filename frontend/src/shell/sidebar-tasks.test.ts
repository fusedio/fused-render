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
  isUnseenCompletion,
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
      // Read: the completion has been dealt with, so there is nothing waiting.
      task({ key: "c", status: "done", unread: 0 }),
      // Not a completion at all.
      task({ key: "d", status: "upcoming" }),
      task({ key: "e", status: "archived", unread: 3 }),
    ];
    expect(tasksPulse(tasks, {})).toEqual({ running: 1, doneUnread: 1, unseen: 1 });
    expect(tasksPulse([], {})).toEqual(EMPTY_TASKS_PULSE);
  });

  it("does not paint a FAILED run green", () => {
    // Failed is a status of its own on this page (taskColumn), and the green mark
    // means "work you were waiting on is ready". A broken run wearing the done
    // hue would be the one place in the app where a colour disagreed with the
    // ring the row itself draws (design-principles §1).
    const broke = [task({ key: "f", status: "failed", unread: 1 })];
    expect(tasksPulse(broke, {})).toEqual({ running: 0, doneUnread: 0, unseen: 0 });
  });

  it("keeps the COUNT through a visit and drops only the dot", () => {
    // The two are different kinds of statement (Akshil, 2026-08-18). The dot is
    // an interruption and the visit answers it; the count is a standing fact
    // about unread work, and glancing at the list does not make it untrue.
    const finished = task({ key: "b", status: "done", unread: 1, last_active: 500 });
    const before = [task({ key: "a", status: "in_progress" }), finished];
    expect(tasksPulse(before, {})).toEqual({ running: 1, doneUnread: 1, unseen: 1 });

    const seen = seenAfterVisit(before);
    const after = tasksPulse(before, seen);
    expect(after.unseen).toBe(0);
    expect(after.doneUnread).toBe(1);
    // The count falls when the work is READ — the server's own `unread`, nothing
    // this module stores.
    const read = [task({ key: "b", status: "done", unread: 0, last_active: 500 })];
    expect(tasksPulse(read, seen).doneUnread).toBe(0);
    expect(isDoneUnread(finished)).toBe(true);
    expect(isDoneUnread(read[0])).toBe(false);
  });

  it("brings the dot back for a completion the visit never showed", () => {
    const finished = task({ key: "b", status: "done", unread: 1, last_active: 500 });
    const before = [task({ key: "a", status: "in_progress" }), finished];
    // Landing on /tasks stamps every DONE task with the completion on screen.
    const seen = seenAfterVisit(before);
    expect(seen).toEqual({ b: 500 });
    expect(tasksPulse(before, seen).unseen).toBe(0);
    // Still nothing after a poll that changes nothing — the whole point of
    // persisting the stamp rather than a "dismissed" flag on the session.
    expect(tasksPulse([...before], seen).unseen).toBe(0);

    // The RUNNING task completing is a new completion: it was never stamped,
    // because its completion had not happened when the reader was there.
    const settled = [task({ key: "a", status: "done", unread: 1 }), finished];
    expect(tasksPulse(settled, seen).unseen).toBe(1);

    // And so is the SAME task running again and finishing again: `last_active`
    // moves, so the stamp no longer describes what is on screen. A bare set of
    // dismissed keys would have swallowed this one forever.
    const again = [task({ key: "b", status: "done", unread: 1, last_active: 900 })];
    expect(tasksPulse(again, seen).unseen).toBe(1);
    expect(isUnseenCompletion(again[0], seen)).toBe(true);
    expect(isUnseenCompletion(finished, seen)).toBe(false);
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

  it("merges a visit's stamps over the ones already held", () => {
    // BUGBOT, 2026-08-18: rebuilding the map out of the DONE rows alone dropped
    // the stamp of any task that was momentarily something else. A finished task
    // that has just been re-run reads `in_progress` for the length of that run,
    // so the old rule threw its stamp away mid-run and the PREVIOUS completion
    // popped back as unseen the moment the new one landed.
    const prev: TasksSeen = { a: 100, b: 200 };
    const rerunning = task({ key: "a", status: "in_progress", last_active: 400 });
    const finished = task({ key: "b", status: "done", unread: 1, last_active: 250 });
    const next = seenAfterVisit([rerunning, finished], prev);
    expect(next).toEqual({ a: 100, b: 250 });
    // The prune is the ANSWER'S OWN membership: a task that has left the list
    // takes its stamp with it, so the row cannot grow without bound.
    expect(seenAfterVisit([finished], prev)).toEqual({ b: 250 });
    // With nothing held, a merge is the plain stamping it always was.
    expect(seenAfterVisit([finished])).toEqual({ b: 250 });
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
    // The store publishes only on a CHANGED triple, and the sidebar's own
    // mark-seen effect runs on every published pulse — value equality is what
    // keeps that from looping. `unseen` is in the comparison: it is the field the
    // dismissal moves, and a publish that skipped it would leave the dot up.
    const p = { running: 1, doneUnread: 1, unseen: 1 };
    expect(samePulse(p, { running: 1, doneUnread: 1, unseen: 1 })).toBe(true);
    expect(samePulse(p, { running: 1, doneUnread: 1, unseen: 0 })).toBe(false);
    expect(samePulse(p, { running: 1, doneUnread: 2, unseen: 1 })).toBe(false);
    expect(samePulse(p, { running: 0, doneUnread: 1, unseen: 1 })).toBe(false);
    const a: TasksSeen = { x: 1, y: 2 };
    expect(sameSeen(a, { y: 2, x: 1 })).toBe(true);
    expect(sameSeen(a, { x: 1 })).toBe(false);
    expect(sameSeen(a, { x: 1, y: 3 })).toBe(false);
  });

  it("says the same sentence in both collapse states", () => {
    // The tooltip names the STATE, not the dismissal, so a dot and a chip on the
    // same entry cannot quote different numbers.
    expect(runningLabel(1)).toBe("1 running");
    expect(pulseTitle({ running: 2, doneUnread: 1, unseen: 0 }))
      .toBe("2 running \u00b7 1 finished, not read");
    expect(pulseTitle({ running: 0, doneUnread: 3, unseen: 1 })).toBe("3 finished, not read");
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
    expect(STORE).toMatch(/if \(listeners\.size === 0 \|\| feeders > 0\) return;/);
    // Cadence follows the state, and idle is slower than the page's own 20s.
    expect(STORE).toContain("pulse.running > 0 ? ACTIVE_MS : IDLE_MS");
    expect(STORE).toContain("const IDLE_MS = 30_000");
  });

  it("stands down completely while the page is the poller", () => {
    // BUGBOT, 2026-08-18: restarting the timer on every publish was not enough.
    // The page polls every 20s and this module re-armed at ACTIVE_MS (10s)
    // whenever anything was running — so the busiest case, /tasks open with work
    // in flight, fired an EXTRA request between the page's own. A feeder is not a
    // hint about timing: it says this module is not the poller, and the timer
    // does not run at all while one is held.
    expect(STORE).toContain("export function useTasksFeeder()");
    // Including the sidebar's own mount read: it remounts on every navigation
    // (App keys it on the nav epoch), so an unconditional fetch there would
    // spend the same double-poll per trip to /tasks instead of per tick.
    expect(STORE).toContain("if (feeders === 0) void poll();");
    expect(STORE).toContain("feeders++");
    expect(STORE).toContain("feeders--");
    expect(SCHEDULED).toContain("useTasksFeeder();");
    // Held for the page's whole life, so it is armed exactly while the page's own
    // poll is — not toggled per fetch, where a failed round would hand the job
    // back mid-visit.
    expect(SCHEDULED.indexOf("useTasksFeeder();")).toBeLessThan(
      SCHEDULED.indexOf("const reload = () =>"),
    );
  });

  it("drops a stale self-poll that resolves after a fresher publish", () => {
    // BUGBOT, 2026-08-18: a self-poll already in the air when the page starts
    // feeding (or when a fresher publish lands) must LOSE, not overwrite. The
    // poll captures the generation on departure and publishes only if nothing
    // moved it while the request was in flight.
    expect(STORE).toContain("const departed = generation;");
    expect(STORE).toMatch(
      /if \(feeders === 0 && generation === departed\) publishTasks\(answer\);/,
    );
    // Every publish — the page's or a poll's own — is a new generation, so two
    // racing polls can't both win either.
    expect(STORE).toMatch(/generation \+= 1;\s*\n\s*tasks = next;/);
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
    // And the DOT is not drawn at all while that page is the one on screen — the
    // stamp needs a poll to land, and a dot flashing beside the open page and
    // then clearing itself is worse than no dot. The CHIP is untouched by the
    // visit on purpose (see the pulse tests above).
    expect(SIDEBAR).toContain("const unseen = tasksActive ? 0 : pulse.unseen;");
    expect(SIDEBAR).not.toContain("tasksActive ? 0 : pulse.doneUnread");
    // localStorage is only ever touched inside a try — a blocked store costs the
    // dismissal, never the sidebar (the same rule Scheduled.tsx's view memory
    // follows).
    expect(STORE).toContain("localStorage.getItem(TASKS_SEEN_KEY)");
    expect(STORE).toContain("localStorage.setItem(TASKS_SEEN_KEY");
    expect((STORE.match(/try \{/g) ?? []).length).toBeGreaterThanOrEqual(3);
  });

  it("never stamps from a store that has not been filled yet", () => {
    // BUGBOT, 2026-08-18: the sidebar's effect runs on the FIRST render on
    // /tasks, before the fetch answers. Stamping "every done task on screen"
    // over an empty store wrote `{}` and threw away every dismissal the reader
    // had — and someone who opened the page and left before the first poll came
    // back lost them permanently. `[]` before the first answer and `[]` on a
    // machine with no tasks are different facts, so the store records which one
    // it is holding.
    expect(STORE).toContain("let loaded = false;");
    expect(STORE).toContain("loaded = true;");
    expect(STORE).toMatch(/export function markTasksSeen\(\) \{\n  if \(!loaded\) return;/);
    // Only a real answer sets it: the flag is raised where the rows arrive.
    const publish = STORE.slice(
      STORE.indexOf("export function publishTasks"),
      STORE.indexOf("}", STORE.indexOf("export function publishTasks")),
    );
    expect(publish).toContain("loaded = true;");
    // And the write MERGES over what is already stored rather than replacing it.
    expect(STORE).toContain("seenAfterVisit(tasks, seen)");
  });
});

describe("the Tasks entry's two marks", () => {
  it("draws ONE dot on the icon, yellow winning over green", () => {
    // One dot: two in a corner is not a state this can draw, and "something is
    // running" is the fact that outranks "something is ready".
    expect(SIDEBAR).toContain("sidebar-rail-dot is-running");
    expect(SIDEBAR).toContain("sidebar-rail-dot is-unread");
    expect(SIDEBAR.indexOf("pulse.running > 0 ? (")).toBeLessThan(
      SIDEBAR.indexOf("unseen > 0 ? ("),
    );
    // The dot reads the DISMISSAL-GATED number, which is the whole difference
    // between it and the chip beside the label.
    expect(SIDEBAR).toContain(") : unseen > 0 ? (");
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

  it("wears the SAME dot when expanded, and adds words to it", () => {
    // The dot is the constant across both modes (Akshil, 2026-08-18): the icon is
    // where the eye lands at either width, so a mark that shows collapsed and
    // vanishes on expand reads as the state going away. Same node, both slots —
    // the rail's `badge` and the row's `extra`.
    expect(SIDEBAR).toContain("badge: tasksDot");
    expect(SIDEBAR).toContain("extra={tasksDot}");
    expect(FRAME).toContain("{extra}");
    // And the glyph's own span anchors it there, or it resolves against the
    // viewport and drags a scrollbar in with it (account.css).
    expect(SIDEBAR_CSS).toMatch(/\.sidebar-item \.icon \{[^}]*position: relative/);
    expect(SIDEBAR_CSS).toContain(".sidebar-item .icon > .sidebar-rail-dot {");
    // Expanded ADDS words beside the dot rather than trading it for them.
    const trailing = SIDEBAR.slice(
      SIDEBAR.indexOf("const tasksTrailing ="),
      SIDEBAR.indexOf("// Everything that is not primary nav"),
    );
    expect(trailing).toContain("runningLabel(pulse.running)");
    // The chip reads the RAW state — no dismissal, no visit suppression: it says
    // what is waiting to be read until it is read.
    expect(trailing).toContain("{pulse.doneUnread}");
    expect(trailing).not.toContain("unseen");
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

    // AND THE WORDS NEVER VANISH (Akshil, 2026-08-18). A band that sweeps toward
    // the page colour takes each letter down to a whisper as it passes — a blink,
    // not a shimmer, with the readout barely there for a third of the cycle.
    // Every stop is the full status hue or the status hue mixed toward `--fg`, so
    // the travelling band is BRIGHTER than the resting ink and the animation's
    // floor is the flat, fully readable label.
    const grad = SIDEBAR_CSS.slice(
      SIDEBAR_CSS.indexOf(".sidebar-running {"),
      SIDEBAR_CSS.indexOf("@keyframes sidebar-running-shimmer"),
    );
    expect(grad).toContain("color-mix(in srgb, var(--status-progress) 65%, var(--fg))");
    expect(grad).not.toContain("var(--bg)");
    expect(grad).not.toContain("transparent)");
    // The sweep is a background POSITION and nothing else: no opacity in the
    // cycle, no colour keyframes — nothing that can take the element towards
    // invisible on its way past.
    const frames = SIDEBAR_CSS.slice(SIDEBAR_CSS.indexOf("@keyframes sidebar-running-shimmer"));
    const cycle = frames.slice(0, frames.indexOf("\n}"));
    expect(cycle).toContain("background-position");
    expect(cycle).not.toContain("opacity");
    expect(cycle).not.toContain("color");

    // AND IT NEVER GOES AWAY EITHER (Akshil, screenshot, 2026-08-18: the label
    // animated in and out). With `background-clip: text` plus a transparent fill,
    // the glyphs are a window onto the background — so anywhere the background
    // does not reach, the letters are not drawn at all. Two holes, both closed:
    //
    // 1. THE SWEEP RAN OFF THE BOX. `no-repeat` and a travel from 150% to -150%
    //    left the gradient entirely outside the element for most of the cycle,
    //    blanking the label and bringing it back. Every stop of the travel must
    //    stay inside 0%-100%, where a 300%-wide image still covers the box.
    const stops = [...cycle.matchAll(/background-position:\s*(-?\d+)%/g)].map((m) =>
      Number(m[1]),
    );
    expect(stops.length).toBeGreaterThanOrEqual(2);
    for (const stop of stops) {
      expect(stop).toBeGreaterThanOrEqual(0);
      expect(stop).toBeLessThanOrEqual(100);
    }
    // ...with the fill repeating, so not even a rounding error at the ends of the
    // travel can expose an unpainted letter.
    expect(grad).toContain("background-repeat: repeat");
    expect(grad).not.toContain("no-repeat");

    // 2. THE GLYPHS OVERFLOWED THE CLIP BOX. At `line-height: 1` the painted area
    //    is shorter than the type it is clipped to, and the descender of the "g"
    //    in "running" came out chipped. The box has to hold the whole glyph.
    const lh = grad.match(/line-height:\s*([\d.]+)/);
    expect(lh).not.toBe(null);
    expect(Number(lh![1])).toBeGreaterThanOrEqual(1.4);
    expect(grad).toMatch(/padding:\s*\d+px/);

    // The blanket reduced-motion rule runs an animation ONCE at 0.01ms, which
    // would park this gradient wherever it stopped — a readout whose ink depends
    // on an animation frame. So the gradient is dropped and the ink is the flat
    // status hue: the same label the moving version rests on.
    const rm = SIDEBAR_CSS.slice(SIDEBAR_CSS.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(rm).toContain(".sidebar-running");
    expect(rm).toContain("animation: none");
    expect(rm).toContain("background-image: none");
    expect(rm).toContain("-webkit-text-fill-color: var(--status-progress)");
  });
});
