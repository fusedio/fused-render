// THE CHAT'S HEADER IS THE TASK PANEL'S (Akshil, 2026-09-14).
//
// In a session with a row behind it, the top line is the task side peek's own
// identity block — status ring · TASK-nnn · title · project — and not the ✻
// Claude wordmark and the file path it used to print. The two surfaces draw the
// SAME component (`shell/TaskPeekWho.tsx`), which is the whole point: a reader
// moving between the peek and the chat must not have to pair up two headers for
// one conversation.
//
// The fallback is the other half of the claim, and it is a real state: a chat
// seconds old has a session id before `/api/tasks` has a row for it, so the old
// line stands until one lands rather than the header flashing in late.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { Task } from "@platform/lib/api";

// DYNAMIC, after the shim above has run: the header reaches
// `@platform/lib/router` through the shell row's own link builder, and that
// module reads `location` at import time (testDomShim's own note). A static
// import is hoisted above the shim call.
const { Topbar } = await import("./Topbar");
const { resetSessionSeeds, seedSessionTask, useSessionTask } = await import(
  "./useRecentTasks"
);
type SubscribeTasks = import("./useRecentTasks").SubscribeTasks;

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

const task = (over: Partial<Task> = {}): Task =>
  ({
    key: "sess-1",
    task_id: "TASK-042",
    title: "Rename the pane noun",
    project: "/repo/app",
    target: "/repo/app/x.py",
    session_id: "sess-1",
    status: "in_progress",
    ...over,
  }) as Task;

function render(props: Parameters<typeof Topbar>[0]) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Topbar, props));
  });
  mounted.push(r);
  const has = (cls: string) =>
    r.root.findAll(
      (n) => typeof n.type === "string" && String(n.props.className ?? "").split(" ").includes(cls),
      { deep: true },
    );
  return { r, has, text: () => JSON.stringify(r.toJSON()) };
}

test("a session with a task row wears the peek's identity block", () => {
  const v = render({ sessionId: "sess-1", subtitle: "x.py", task: task(), running: false });
  expect(v.has("task-side-peek-who").length).toBe(1);
  expect(v.has("task-side-peek-id").length).toBe(1);
  expect(v.has("task-side-peek-title").length).toBe(1);
  expect(v.has("task-side-peek-project").length).toBe(1);
  expect(v.text()).toContain("TASK-042");
  expect(v.text()).toContain("Rename the pane noun");
  // The project is the folder's NAME, not its path — the chip's tooltip carries
  // the whole of it.
  expect(v.text()).toContain("app");
  // …and the tool's own marks are gone: the wordmark, the "Claude" label and
  // the file path are facts about the tool and the file, printed twice over
  // elsewhere on the page.
  expect(v.has("c-tb-title").length).toBe(0);
  expect(v.has("c-tb-file").length).toBe(0);
  expect(v.has("c-session").length).toBe(0);
});

test("the running word stays beside the ring", () => {
  // The ring is the LISTING's answer and arrives on a long-poll; this is the
  // page's own turn clock, and at both ends of a turn it is the faster of the
  // two.
  const v = render({ sessionId: "sess-1", task: task(), running: true });
  expect(v.has("c-tb-run").length).toBe(1);
  const off = render({ sessionId: "sess-1", task: task(), running: false });
  expect(off.has("c-tb-run").length).toBe(0);
});

test("no row yet — the line the chat has always printed", () => {
  const v = render({
    sessionId: "sess-1",
    subtitle: "x.py",
    taskId: "TASK-042",
    task: null,
    running: false,
  });
  expect(v.has("task-side-peek-who").length).toBe(0);
  expect(v.has("c-tb-title").length).toBe(1);
  expect(v.has("c-session").length).toBe(1);
  expect(v.text()).toContain("TASK-042");
});

// ---- AND THE THIRD STATE: NOBODY HAS ANSWERED YET (Akshil, 2026-09-14) ------
//
// "We have not read `/api/tasks`" and "we read it and this session is not in
// it" used to be one `null`, so a deep link into an old conversation wore the ✻
// Claude line — a CLAIM that the chat has no task row — for the length of an
// 800-row listing read, and then swapped. The claim is true of exactly one
// thing: a chat so new the server's watcher has not seen its transcript.

/** The subscription, handed in rather than module-mocked (`useRecentTasks`'s
 *  own note says why: one process, every suite). Opens with the skeleton signal
 *  the real one opens with. */
function stubSubscribe() {
  const served: Array<(rows: Task[] | null) => void> = [];
  const subscribe = ((_file: string | null, cb: (rows: Task[] | null) => void) => {
    served.push(cb);
    cb(null);
    return () => {};
  }) as SubscribeTasks;
  return { subscribe, serve: (rows: Task[] | null) => served[served.length - 1](rows) };
}

/** The header, drawn from the hook — which is where the three states are
 *  decided, so this is the only honest way to pin them. */
function mountHeader(sessionId: string | null, subscribe: SubscribeTasks) {
  function Probe() {
    const head = useSessionTask(sessionId, "/repo/app", subscribe);
    return createElement(Topbar, {
      sessionId: sessionId ?? "",
      subtitle: "x.py",
      task: head.task,
      pending: head.pending,
      running: false,
    });
  }
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Probe));
  });
  mounted.push(r);
  const has = (cls: string) =>
    r.root.findAll(
      (n) => typeof n.type === "string" && String(n.props.className ?? "").split(" ").includes(cls),
      { deep: true },
    );
  return { r, has, text: () => JSON.stringify(r.toJSON()) };
}

afterEach(() => resetSessionSeeds());

test("a row pressed in the Recent list names the header AT ONCE — no listing round trip", () => {
  // The list was already drawing this `Task`; `Lists.pressFor` leaves it here on
  // the way into the chat, so the first paint of the header is the real one.
  seedSessionTask(task({ key: "sess-9", session_id: "sess-9", task_id: "TASK-009" }));
  const feed = stubSubscribe();
  const v = mountHeader("sess-9", feed.subscribe);
  expect(v.has("task-side-peek-who").length).toBe(1);
  expect(v.text()).toContain("TASK-009");
  // …and never the skeleton or the wordmark on the way there.
  expect(v.has("c-tb-skel").length).toBe(0);
  expect(v.has("c-tb-title").length).toBe(0);
});

test("a DEEP LINK with no seed draws the skeleton, not the ✻ Claude line", () => {
  const feed = stubSubscribe();
  const v = mountHeader("sess-deep", feed.subscribe);
  expect(v.has("c-tb-skel").length).toBe(1);
  expect(v.has("c-skel-dot").length).toBe(1);
  expect(v.has("c-skel-bar").length).toBe(1);
  // The claim is not made while the answer is unknown.
  expect(v.has("c-tb-title").length).toBe(0);
  expect(v.has("task-side-peek-who").length).toBe(0);
  // …and it gives way to the row the moment the listing answers.
  act(() => feed.serve([task({ key: "sess-deep", session_id: "sess-deep" })]));
  expect(v.has("c-tb-skel").length).toBe(0);
  expect(v.has("task-side-peek-who").length).toBe(1);
});

test("the ✻ Claude line is for a listing that answered and has NO row — a brand-new chat", () => {
  const feed = stubSubscribe();
  const v = mountHeader("sess-new", feed.subscribe);
  act(() => feed.serve([task({ key: "someone-else", session_id: "someone-else" })]));
  expect(v.has("c-tb-skel").length).toBe(0);
  expect(v.has("c-tb-title").length).toBe(1);
  expect(v.has("c-tb-file").length).toBe(1);
});

test("the seed is in hand on the FIRST render — no tick, no skeleton frame", () => {
  // Akshil QA, 2026-09-14: a seeded header still wore the skeleton for ~260 ms.
  // The seed is written by the press and read by the hook, so the only way to be
  // late with it is to read it late — in an effect, or after a subscription
  // tick. Every paint is counted here, and the first one already has the row.
  seedSessionTask(task({ key: "sess-7", session_id: "sess-7", task_id: "TASK-007" }));
  const feed = stubSubscribe();
  const paints: Array<{ id: boolean; skel: boolean }> = [];
  function Probe() {
    const head = useSessionTask("sess-7", "/repo/app", feed.subscribe);
    paints.push({ id: !!head.task, skel: head.pending });
    return createElement(Topbar, {
      sessionId: "sess-7",
      task: head.task,
      pending: head.pending,
      running: false,
    });
  }
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Probe));
  });
  mounted.push(r);
  expect(paints.length).toBeGreaterThan(0);
  expect(paints.every((p) => p.id && !p.skel)).toBe(true);
  // …including the subscription's opening `null`, which is what the 260 ms was.
  act(() => feed.serve(null));
  expect(paints.every((p) => p.id && !p.skel)).toBe(true);
});

test("the session ARRIVING is the same first render — the landing's hook is already mounted", () => {
  // The hook lives in `ClaudeChat`'s body and is handed `null` while the landing
  // is up, so the press does not mount it: the id arrives as a prop change, and
  // the seed has to be adopted on THAT render rather than in the effect after it.
  seedSessionTask(task({ key: "sess-8", session_id: "sess-8", task_id: "TASK-008" }));
  const feed = stubSubscribe();
  const paints: Array<{ id: boolean; skel: boolean }> = [];
  function Probe({ sessionId }: { sessionId: string | null }) {
    const head = useSessionTask(sessionId, "/repo/app", feed.subscribe);
    paints.push({ id: !!head.task, skel: head.pending });
    return createElement(Topbar, {
      sessionId: sessionId ?? "",
      task: head.task,
      pending: head.pending,
      running: false,
    });
  }
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Probe, { sessionId: null }));
  });
  mounted.push(r);
  paints.length = 0;
  act(() => r.update(createElement(Probe, { sessionId: "sess-8" })));
  expect(paints.length).toBeGreaterThan(0);
  expect(paints.every((p) => p.id && !p.skel)).toBe(true);
  const has = (cls: string) =>
    r.root.findAll(
      (n) => typeof n.type === "string" && String(n.props.className ?? "").split(" ").includes(cls),
      { deep: true },
    );
  expect(has("c-tb-skel").length).toBe(0);
  expect(has("task-side-peek-who").length).toBe(1);
});

test("a task always outranks `pending` — the placeholder never covers its own answer", () => {
  const v = render({ sessionId: "sess-1", task: task(), pending: true, running: false });
  expect(v.has("c-tb-skel").length).toBe(0);
  expect(v.has("task-side-peek-who").length).toBe(1);
});
