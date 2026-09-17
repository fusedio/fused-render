// TWO MOUNTS, ONE READ, BOTH ANSWERED. A card and its own TaskPeek are two
// mounts on the same session, and the dedupe that keeps `/api/tasks` to one
// read must not cost the second mount its answer — it used to sit on the
// session hash until something unrelated re-rendered it.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { Kebab, useTaskId, forgetTaskCaches, ASK_AGAIN_MS } = await import("./Kebab");
const { TASKS_CHANGED_EVENT } = await import("@platform/lib/tasksChanged");
const { readFileSync } = await import("node:fs");
const { join } = await import("node:path");

const SESSION = "abcdef0123456789";
let calls = 0;
let answer: () => Promise<unknown> = async () => ({
  tasks: [{ key: SESSION, task_id: 42, status: "done" }],
});
const realFetch = globalThis.fetch;
beforeEach(() => {
  calls = 0;
  (globalThis as { fetch: unknown }).fetch = async () => {
    calls += 1;
    const body = await answer();
    return { ok: true, json: async () => body } as unknown as Response;
  };
});
/** The shim's `window` is a no-op for events (platform/lib/testDomShim), and
 *  the reactive half of R2-9 IS an event listener — so this suite gives the
 *  global window a real, tiny registry for the length of a test. Not a change
 *  to the shim: a chat's menu is the wrong reason to give every test in the
 *  repo a live event bus. */
function liveWindowEvents(): () => void {
  const w = globalThis.window as unknown as {
    addEventListener: unknown;
    removeEventListener: unknown;
    dispatchEvent: unknown;
  };
  const was = {
    add: w.addEventListener,
    remove: w.removeEventListener,
    fire: w.dispatchEvent,
  };
  const bus = new Map<string, Set<(ev: Event) => void>>();
  w.addEventListener = (type: string, fn: (ev: Event) => void) => {
    const set = bus.get(type) ?? new Set();
    set.add(fn);
    bus.set(type, set);
  };
  w.removeEventListener = (type: string, fn: (ev: Event) => void) => {
    bus.get(type)?.delete(fn);
  };
  w.dispatchEvent = (ev: Event) => {
    for (const fn of [...(bus.get(ev.type) ?? [])]) fn(ev);
    return true;
  };
  return () => {
    w.addEventListener = was.add;
    w.removeEventListener = was.remove;
    w.dispatchEvent = was.fire;
  };
}

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  forgetTaskCaches(SESSION);
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

function Probe({ onLabel }: { onLabel: (label: string) => void }) {
  onLabel(useTaskId(SESSION));
  return null;
}
/** Mount a probe and return every label it has been handed, in order. */
function probe() {
  const labels: string[] = [];
  let r!: ReactTestRenderer;
  act(() => {
    r = create(<Probe onLabel={(l) => labels.push(l)} />);
  });
  mounted.push(r);
  return labels;
}
const settle = () =>
  act(async () => {
    await new Promise((done) => setTimeout(done, 0));
  });

test("a second mount on the same session gets the number too, off ONE read", async () => {
  const first = probe();
  const second = probe(); // the card and its peek, same session, same tick
  expect(first[0]).toBe("abcdef01"); // the hash paints first
  await settle();
  expect(calls).toBe(1); // still one `/api/tasks` read for the pair
  expect(first[first.length - 1]).toBe("42");
  expect(second[second.length - 1]).toBe("42"); // …and NOT the hash
});

test("a failed read leaves both mounts on the hash and does not cache", async () => {
  answer = async () => {
    throw new Error("offline");
  };
  const first = probe();
  const second = probe();
  await settle();
  expect(first[first.length - 1]).toBe("abcdef01");
  expect(second[second.length - 1]).toBe("abcdef01");
  expect(calls).toBe(1);
  // Nothing was cached, so a later mount tries again.
  answer = async () => ({ tasks: [{ key: SESSION, task_id: 7, status: "done" }] });
  const third = probe();
  await settle();
  expect(calls).toBe(2);
  expect(third[third.length - 1]).toBe("7");
});

// ── R2-9: the number arrives without a reload ──────────────────────────────

test("a session with no task row yet is asked about AGAIN, and the number lands", async () => {
  // The bug: a chat that has just started has a session id seconds before
  // `/api/tasks` has a row for it. One read, ever, meant the header printed a
  // truncated session hash until the reader reloaded the page.
  answer = async () => ({ tasks: [] });
  const labels = probe();
  await settle();
  expect(labels[labels.length - 1]).toBe("abcdef01"); // the hash, for now
  expect(calls).toBe(1);

  // The row appears, and the retry that was already scheduled picks it up.
  answer = async () => ({ tasks: [{ key: SESSION, task_id: "TASK-042", status: "done" }] });
  await act(async () => {
    await new Promise((done) => setTimeout(done, ASK_AGAIN_MS[0] + 20));
  });
  expect(calls).toBe(2);
  expect(labels[labels.length - 1]).toBe("TASK-042");

  // …and now that it has landed, the schedule is over: nothing else is read.
  await act(async () => {
    await new Promise((done) => setTimeout(done, ASK_AGAIN_MS[1] + 20));
  });
  expect(calls).toBe(2);
});

test("a tasks-changed announcement beats the timer", async () => {
  const restore = liveWindowEvents();
  try {
  answer = async () => ({ tasks: [] });
  const labels = probe();
  await settle();
  expect(calls).toBe(1);

  answer = async () => ({ tasks: [{ key: SESSION, task_id: "TASK-007", status: "done" }] });
  // What the run controller fires the moment a turn starts — which is the same
  // moment the task row is created (protocol/run-controller.ts).
  await act(async () => {
    window.dispatchEvent(new Event(TASKS_CHANGED_EVENT));
    await new Promise((done) => setTimeout(done, 0));
  });
  expect(calls).toBe(2);
  expect(labels[labels.length - 1]).toBe("TASK-007");
  } finally {
    restore();
  }
});

test("the poke is ignored once the number is known — a number does not change", async () => {
  const restore = liveWindowEvents();
  try {
  answer = async () => ({ tasks: [{ key: SESSION, task_id: 11, status: "done" }] });
  const labels = probe();
  await settle();
  expect(labels[labels.length - 1]).toBe("11");
  await act(async () => {
    window.dispatchEvent(new Event(TASKS_CHANGED_EVENT));
    await new Promise((done) => setTimeout(done, ASK_AGAIN_MS[0] + 20));
  });
  expect(calls).toBe(1);
  } finally {
    restore();
  }
});

// ── R3-2: the menu's own state follows the task row ────────────────────────
//
// `useTaskId` stops asking the moment the NUMBER lands — which is right for a
// number that never changes, and exactly wrong for the STATUS beside it. So the
// menu keeps its own watch: `taskRunning` is cached TRUE for the whole of a
// turn, the popup reads `disabled` as it mounts its items, and a read landing
// mid-open does not lift it — so Archive and Delete stayed greyed out after the
// run had finished and only came back on a second open (owner, R3-2).
//
// The trigger is all that renders here (base-ui mounts no content without a
// real pointer event), which is fine: what is under test is the READ, and the
// read is what the items are decided from.
function kebab(running: boolean): ReactTestRenderer {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(<Kebab agentDir="/tpl" file="/proj/app.py" sessionId={SESSION} running={running} />);
  });
  mounted.push(r);
  return r;
}

test("the menu re-reads the row when the run ends, and on every announcement (R3-2)", async () => {
  const restore = liveWindowEvents();
  try {
    // A turn is live, and the listing agrees.
    answer = async () => ({
      tasks: [{ key: SESSION, task_id: 42, status: "in_progress" }],
    });
    const r = kebab(true);
    await settle();
    expect(calls).toBe(1);

    // The turn ends. `running` moving is this page's own news — it knows one
    // render before any listing does — and it must buy a fresh read.
    answer = async () => ({ tasks: [{ key: SESSION, task_id: 42, status: "done" }] });
    await act(async () => {
      r.update(
        <Kebab agentDir="/tpl" file="/proj/app.py" sessionId={SESSION} running={false} />,
      );
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(calls).toBe(2);

    // …and an announcement re-reads it too, which is the case `useTaskId`
    // refuses: the number has already landed, so that hook has gone quiet for
    // good, and the status is only now starting to matter.
    await act(async () => {
      window.dispatchEvent(new Event(TASKS_CHANGED_EVENT));
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(calls).toBe(3);
  } finally {
    restore();
  }
});

test("the menu's watch goes with the mount", async () => {
  const restore = liveWindowEvents();
  try {
    answer = async () => ({ tasks: [{ key: SESSION, task_id: 42, status: "done" }] });
    const r = kebab(false);
    await settle();
    const before = calls;
    act(() => r.unmount());
    mounted.length = 0;
    await act(async () => {
      window.dispatchEvent(new Event(TASKS_CHANGED_EVENT));
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(calls).toBe(before);
  } finally {
    restore();
  }
});

test("the landing's menu has no session and reads nothing", async () => {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(<Kebab agentDir="/tpl" file="/proj" sessionId="" running={false} landing />);
  });
  mounted.push(r);
  await settle();
  // Six of these on a cards wall would be six `/api/tasks` reads for a menu
  // whose one item needs no session at all (T:13415).
  expect(calls).toBe(0);
});

// ── R2-8: Archive closes the menu before it does anything ──────────────────
//
// READ OFF THE SOURCE, and deliberately so. The item lives inside a base-ui
// `DropdownMenuContent`, which mounts nothing at all until a real pointer event
// has opened the popup — there is no item to click under a renderer with no DOM
// (the repo does the same thing for tasksPulse's wiring, shell/sidebar-tasks).
// What is pinned here is the pair of facts the bug was made of: the item used to
// opt OUT of closing on click, and the close used to be the LAST thing to
// happen, 1.1s after the network round trip it waited on.
test("the Archive item closes the menu on click, before the call (R2-8)", () => {
  const src = readFileSync(join(import.meta.dir, "Kebab.tsx"), "utf8");
  const item = src.slice(src.indexOf("onClick={() => void onArchive()}") - 700);
  const archiveItem = item.slice(0, item.indexOf("onClick={() => void onArchive()}"));
  expect(archiveItem).not.toContain("closeOnClick={false}");

  const action = src.slice(src.indexOf("const onArchive ="), src.indexOf("// `rev` is read"));
  // The close, and the optimistic flip that makes the reopened (or next) menu
  // say the new word, both land BEFORE the first await.
  const beforeAwait = action.slice(0, action.indexOf("await unarchiveTask"));
  expect(beforeAwait).toContain("setOpen(false)");
  expect(beforeAwait).toContain("remember(archiveStates, sessionId, !wasFiled)");
  // …and a failure puts the world back and says so, rather than reverting in
  // silence.
  expect(action).toContain("remember(archiveStates, sessionId, wasFiled)");
  expect(action).toContain("setOpen(true)");
});

// ── P3R1-5: the nav lock reaches the two items that leave this chat ─────────
//
// Read off the source for the reason the R2-8 test above is: base-ui mounts no
// item without a real pointer event. What is pinned is the pair of facts the
// item is made of — BOTH refusals on `disabled`, and a `title` that names the
// gesture the reader can actually make.
test("Archive and Delete are refused while a mode owns the page (P3R1-5)", () => {
  const src = readFileSync(join(import.meta.dir, "Kebab.tsx"), "utf8");
  // The prop, and its reason, come in from the chat rather than being imported:
  // this menu owns no vocabulary of the annotation subsystem's.
  expect(src).toContain("locked?: boolean");
  expect(src).toContain("lockedReason?: string");
  const items = src.split("<DropdownMenuItem").slice(2);
  expect(items).toHaveLength(2); // Archive and Delete; the terminal item is neither
  for (const item of items) {
    const head = item.slice(0, item.indexOf("</DropdownMenuItem>"));
    // A run is still named first — it is the refusal the reader cannot lift
    // from here — and the lock is the second answer, never a silent no.
    expect(head).toContain("disabled={live || locked}");
    expect(head).toContain(
      'title={live ? "Stop the run first" : locked ? lockedReason : undefined}',
    );
  }
  // And the chat hands it the lock the rest of the nav already reads
  // (`annNavLocked`, T:6888) with T's own sentence for it.
  const chat = readFileSync(join(import.meta.dir, "../ClaudeChat.tsx"), "utf8");
  const mount = chat.slice(chat.indexOf("<Kebab"));
  const props = mount.slice(0, mount.indexOf("/>"));
  expect(props).toContain("locked={ann.locked}");
  expect(props).toContain("lockedReason={NAV_LOCKED_REASON}");
});

// ── A `pending:` KEY IS NOT A HASH ─────────────────────────────────────────
//
// A chat that has never run is asked about by its LEADER — `pending:<entry id>`
// — and the fail-open at the end of `useTaskId` used to truncate that key like a
// session hash. `"pending:8f2…".slice(0, 8)` is the literal word "pending:", and
// that is what the top of a queued new chat printed until the listing caught up
// (Akshil, 2026-09-12, 🔴 review).
test("answers nothing for a pending: key rather than the word `pending:`", async () => {
  const KEY = "pending:8f2c11d4-aaaa-bbbb";
  answer = async () => ({ tasks: [] });
  const labels: string[] = [];
  function PendingProbe() {
    labels.push(useTaskId(KEY));
    return null;
  }
  let r!: ReactTestRenderer;
  act(() => {
    r = create(<PendingProbe />);
  });
  mounted.push(r);
  await settle();
  expect(labels.length).toBeGreaterThan(0);
  for (const label of labels) {
    expect(label).toBe("");
    expect(label).not.toContain("pending");
  }
  forgetTaskCaches(KEY);
});

// …and when the listing DOES know the number for that same key, it is handed
// back: the guard drops the fallback, not the answer.
test("still answers the number the listing holds for a pending: key", async () => {
  const KEY = "pending:9a1b";
  answer = async () => ({ tasks: [{ key: KEY, task_id: "TASK-077", status: "queued" }] });
  const labels: string[] = [];
  function PendingProbe() {
    labels.push(useTaskId(KEY));
    return null;
  }
  let r!: ReactTestRenderer;
  act(() => {
    r = create(<PendingProbe />);
  });
  mounted.push(r);
  await settle();
  expect(labels[0]).toBe("");
  expect(labels[labels.length - 1]).toBe("TASK-077");
  forgetTaskCaches(KEY);
});

// ── A WAITING CHAT OFFERS NEITHER A TERMINAL NOR A FILE ────────────────────
//
// Read off the source, for the reason the P3R1-5 test above is: base-ui mounts
// no item without a real pointer event. `queued` means the task row says so, or
// this conversation is a message that has never run — either way there is no
// session behind it, so "Continue in terminal" would resume a conversation that
// does not exist and Archive would file work that has not happened. Delete
// stays, because calling the message off IS what a reader wants here (Akshil,
// 2026-09-12).
test("hides Continue in terminal and Archive while the chat is queued", () => {
  const src = readFileSync(join(import.meta.dir, "Kebab.tsx"), "utf8");
  expect(src).toContain("queued?: boolean");
  expect(src).toContain("queued = false,");
  // The terminal item is wrapped in the guard…
  expect(src).toContain("{!queued ? (");
  const terminal = src.slice(src.indexOf("{!queued ? ("));
  expect(terminal.slice(0, terminal.indexOf("</DropdownMenuItem>"))).toContain(
    'sessionId ? "Continue in terminal" : "New session in terminal"',
  );
  // …Archive takes it as a third condition…
  expect(src).toContain("{!landing && hasTask && !queued ? (");
  // …and Delete does NOT: exactly one item in this menu keeps its old condition.
  expect(src).toContain("{!landing && hasTask ? (");
  const del = src.slice(src.lastIndexOf("{!landing && hasTask ? ("));
  expect(del.slice(0, del.indexOf("</DropdownMenuItem>"))).toContain("Delete this task");
  // And the chat hands the fact down from the row it already reads.
  const chat = readFileSync(join(import.meta.dir, "../ClaudeChat.tsx"), "utf8");
  // A CHAT WITH NO SESSION, and nothing else (🟡 review, 2026-09-12). A real
  // conversation whose FOLDER is held is filed `queued` too — it is waiting for
  // its next message, not for its first — and reading that word on its own took
  // Archive and Continue away from a chat with a transcript behind it.
  expect(chat).toContain(
    '    inChat && !state.sessionId && (sched.rec?.status === "queued" || !!leaderId);',
  );
  expect(chat.slice(chat.indexOf("<Kebab"))).toContain("queued={queuedChat}");
});
