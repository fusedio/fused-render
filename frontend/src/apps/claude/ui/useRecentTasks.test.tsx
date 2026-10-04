// WHEN THE SKELETON IS HONEST, and when a `null` off the subscription must be
// swallowed instead (P4-08 / C G-20).
//
// `subscribeTasks` opens every subscription with `null` — the right signal for
// the first read of the page's life, and the wrong one for every read after it.
// T's rule is "the skeleton stands in for rows we do not have yet — never for
// rows that are already up" (T:18408-18411), and separately "the two counts are
// deliberately NOT reset" on the way back to the landing, because "a stale count
// for a moment is quieter than a section that blinks" (T:13049-13053).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { Task } from "@platform/lib/api";

const { skippedOverride } = await import("@shell/tasks-lib");
const { noteQueueClaim, resetQueueClaims, useRecentTasks, useSessionTask } =
  await import("./useRecentTasks");
type SubscribeTasks = import("./useRecentTasks").SubscribeTasks;

/** The subscriptions this mount opened, newest last, each with the callback the
 *  hook handed in and whether it has been torn down. */
interface Sub {
  file: string | null;
  cb(rows: Task[] | null): void;
  stopped: boolean;
  /** T's `leftLive` — whether this subscription asked for the two extra looks. */
  coverWrite: boolean;
}
const subs: Sub[] = [];
/** HANDED IN, not module-patched: an ESM namespace object is frozen, and a
 *  `mock.module` would replace `protocol/sessions` for every suite loaded after
 *  this one in the same process. */
const subscribe = ((
  file: string | null,
  cb: (rows: Task[] | null) => void,
  _env: unknown,
  coverWrite = false,
) => {
  const sub: Sub = { file, cb, stopped: false, coverWrite };
  subs.push(sub);
  // Every real subscription opens with the skeleton signal.
  cb(null);
  return () => {
    sub.stopped = true;
  };
}) as SubscribeTasks;

/** A task in the pane every mount below uses, so the hook's own narrowing
 *  (`taskInPane`) passes it through and the assertions stay about the skeleton
 *  rule this suite is written for. */
const row = (id: string, target = "/repo/x.py"): Task =>
  ({
    key: id,
    task_id: id.toUpperCase(),
    project: "/repo",
    target,
    session_id: id,
    title: id,
  }) as Task;

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  subs.length = 0;
  // The claim store is MODULE state, exactly as the seeds below are: it outlives
  // every renderer in a `bun test` process and has to be put back by hand.
  resetQueueClaims();
});

interface Harness {
  rows(): Task[] | null;
  /** Re-render with a different target, or with none (entering a chat). */
  retarget(agentDir: string | null, file?: string | null): Promise<void>;
  /** The newest subscription's callback. */
  serve(rows: Task[] | null): Promise<void>;
  subs: Sub[];
}

async function mount(
  agentDir: string | null,
  file: string | null,
  coverWrite = false,
): Promise<Harness> {
  let out: Task[] | null = null;
  function Probe(p: { agentDir: string | null; file: string | null }) {
    out = useRecentTasks(p.agentDir, p.file, subscribe, coverWrite);
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(Probe, { agentDir, file }));
  });
  mounted.push(r);
  return {
    rows: () => out,
    async retarget(next: string | null, nextFile: string | null = file) {
      await act(async () => {
        r.update(createElement(Probe, { agentDir: next, file: nextFile }));
      });
    },
    async serve(rows) {
      const sub = subs[subs.length - 1];
      await act(async () => sub.cb(rows));
    },
    subs,
  };
}

test("BOOT is the only place the skeleton is real", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  expect(h.rows()).toBe(null);
  await h.serve([row("a")]);
  expect(h.rows()?.map((t) => t.key)).toEqual(["a"]);
});

test("a target change repaints in place — no blink into placeholder bars", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  await h.serve([row("a"), row("b")]);

  // The re-subscribe fires `null` first, and a list already drawn must keep its
  // rows through it (T:18408-18411).
  await h.retarget("/tpl", "/repo/y.py");
  expect(h.subs.length).toBe(2);
  // The rows are still THERE — but the pane they are shown in has changed, and
  // the narrowing is the hook's (`taskInPane`), so rows about the old file are
  // no longer this pane's. The point the test is making is that no SKELETON was
  // drawn over them; the filter is a separate, correct answer.
  expect(h.rows()).toEqual([]);

  // Then the new target's real answer lands and replaces them.
  await h.serve([row("c", "/repo/y.py")]);
  expect(h.rows()?.map((t) => t.key)).toEqual(["c"]);
});

test("ENTERING A CHAT does not empty the list (T:13049-13053)", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  await h.serve([row("a")]);

  // `ClaudeChat` passes a null agentDir while in a chat, which tears the
  // subscription down. The rows stay: the tab bar over them counts them, and
  // taking it off screen and putting it back for the trip is the blink T
  // deliberately avoids.
  await h.retarget(null);
  expect(h.subs[0].stopped).toBe(true);
  expect(h.rows()?.map((t) => t.key)).toEqual(["a"]);

  // Back: a fresh subscription, still no skeleton over the drawn rows.
  await h.retarget("/tpl");
  expect(h.subs.length).toBe(2);
  expect(h.rows()?.map((t) => t.key)).toEqual(["a"]);
});

test("an honestly EMPTY answer is still published", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  await h.serve([row("a")]);
  // `[]` is not `null`: a folder whose chats really went away must go back to
  // no section at all, or the block outlives its rows.
  await h.serve([]);
  expect(h.rows()).toEqual([]);
});

test("A COLD LANDING ASKS FOR NO EXTRA LOOKS (P4-21)", async () => {
  const cold = await mount("/tpl", "/repo/x.py");
  expect(cold.subs[0].coverWrite).toBe(false);
});

test("leaving a LIVE turn asks for them (T's `leftLive`, T:13066-13071)", async () => {
  const live = await mount("/tpl", "/repo/x.py", true);
  expect(live.subs[0].coverWrite).toBe(true);
});

// ---- the Tasks page's own order (Akshil, 2026-09-14) ------------------------
// The rows ARE the Tasks page's rows, so "what is at the top of this list" has
// to be the Tasks page's answer: status lanes first (`sortForList`), a lane's
// drafts at its head, the server's order breaking every tie inside that.

/** A row in the pane, in a lane of its own. */
const laned = (id: string, status: string, extra: Partial<Task> = {}): Task =>
  ({ ...row(id), status, ...extra }) as Task;

test("the chat's Recent list is ordered like the Tasks LIST view", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  // Handed over in the server's flat order, which puts Done first.
  await h.serve([
    laned("done", "done"),
    laned("live", "in_progress"),
    laned("soon", "upcoming"),
    laned("filed", "archived"),
  ]);
  // Upcoming · In Progress · Blocked · Done · Archive — the lanes' own order,
  // the same one the Tasks list ranks by.
  expect(h.rows()?.map((t) => t.key)).toEqual(["soon", "live", "done", "filed"]);
});

test("a lane's DRAFTS come first, and ties keep the server's order", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  await h.serve([
    laned("a", "in_progress"),
    laned("b", "in_progress"),
    // A draft is `status: upcoming` + `kind: draft`, and it is hoisted to the
    // head of Upcoming rather than given a lane of its own.
    laned("draft", "upcoming", { kind: "draft" }),
    laned("later", "upcoming"),
  ]);
  expect(h.rows()?.map((t) => t.key)).toEqual(["draft", "later", "a", "b"]);
});

// ---- RUN NEXT's claim (Akshil QA, 2026-09-16) -------------------------------
// The Recent row IS the Tasks row, so its skip is the Tasks page's skip — and
// the claim `TaskNode.skip` hands back had nowhere to go here. The press put a
// request on the wire and the row could not change until the next full listing,
// which reads as a button that does nothing.

test("a skip's claim paints the row, ahead of the sort", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  const waiting = laned("q", "queued", { queue_position: 4, queue_priority: false });
  await h.serve([waiting]);
  expect(h.rows()?.[0].queue_position).toBe(4);

  // What `performSkip` answers with (tasks-lib.skippedOverride): head of the
  // line, priority on, and nothing claimed about the run in flight.
  await act(async () => noteQueueClaim(skippedOverride(waiting)));
  expect(h.rows()?.[0].queue_position).toBe(1);
  expect(h.rows()?.[0].queue_priority).toBe(true);
  expect(h.rows()?.[0].status).toBe("queued");
});

test("…and the next listing retires it, right or wrong", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  const waiting = laned("q", "queued", { queue_position: 4, queue_priority: false });
  await h.serve([waiting]);
  await act(async () => noteQueueClaim(skippedOverride(waiting)));
  expect(h.rows()?.[0].queue_position).toBe(1);

  // One answer about a key is the whole life of a claim about that key
  // (tasks-lib.expireQueueOverrides) — otherwise a claim the server disagreed
  // with would survive every poll and the row could never be corrected.
  await h.serve([laned("q", "queued", { queue_position: 4, queue_priority: false })]);
  expect(h.rows()?.[0].queue_position).toBe(4);
  expect(h.rows()?.[0].queue_priority).toBe(false);
});

test("a skip repaints the WHOLE line, so no frame shows two 1sts", async () => {
  // THE DOUBLE-1st FRAME (Akshil, 2026-09-18). The claim promoted the pressed
  // row and said nothing about the row it went past, so for the 0.3-0.6 s before
  // the listing landed both of them read "1st in line" and the reader could not
  // tell which one was going to run.
  const h = await mount("/tpl", "/repo/x.py");
  const first = laned("a", "queued", {
    queue_position: 1,
    queue_ahead: "TASK-000",
    queue_ahead_title: "the run",
    queue_priority: true,
  });
  const second = laned("b", "queued", {
    queue_position: 2,
    queue_ahead: "A",
    queue_ahead_title: "a",
  });
  const third = laned("c", "queued", {
    queue_position: 3,
    queue_ahead: "B",
    queue_ahead_title: "b",
  });
  await h.serve([first, second, third]);

  await act(async () => noteQueueClaim(skippedOverride(second)));
  const by = (key: string) => h.rows()?.find((t) => t.key === key) as Task;
  // The pressed row is the head, and the ⤒ claim is on it and on nothing else.
  expect(by("b").queue_position).toBe(1);
  expect(by("b").queue_priority).toBe(true);
  expect(h.rows()?.filter((t) => t.queue_priority)).toHaveLength(1);
  // The row it went past reads 2nd, and reads it as behind the pressed row —
  // id, title and the pair that makes the id a link.
  expect(by("a").queue_position).toBe(2);
  expect(by("a").queue_priority).toBe(false);
  expect(by("a").queue_ahead).toBe("B");
  expect(by("a").queue_ahead_title).toBe("b");
  expect(by("a").queue_ahead_session).toBe("b");
  expect(by("a").queue_ahead_target).toBe("/repo/x.py");
  // Nobody behind the press moved: a skip jumps the rows in front of it.
  expect(by("c").queue_position).toBe(3);
  expect(by("c").queue_ahead).toBe("B");
  // …and the whole set retires together on the next listing, as one claim did.
  await h.serve([first, second, third]);
  expect(by("a").queue_position).toBe(1);
  expect(by("b").queue_position).toBe(2);
});

test("a claim for a key this pane has no row for changes nothing", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  await h.serve([laned("a", "in_progress")]);
  await act(async () =>
    noteQueueClaim(skippedOverride(laned("elsewhere", "queued"))),
  );
  expect(h.rows()?.map((t) => t.key)).toEqual(["a"]);
  expect(h.rows()?.[0].status).toBe("in_progress");
});

// ---- the header's own row ---------------------------------------------------

async function mountHead(sessionId: string | null, file: string | null) {
  // THREE ANSWERS NOW (`SessionIdentity`): the row, "not read yet" (`pending`),
  // and "read, and there is no row for this session". `task()` below asks the
  // first; the skeleton state has a suite of its own in `topbar-identity`.
  let out: import("./useRecentTasks").SessionIdentity = { task: null, pending: false };
  function Probe(p: { sessionId: string | null }) {
    out = useSessionTask(p.sessionId, file, subscribe);
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(Probe, { sessionId }));
  });
  mounted.push(r);
  return {
    task: () => out.task,
    pending: () => out.pending,
    async serve(rows: Task[] | null) {
      const sub = subs[subs.length - 1];
      await act(async () => sub.cb(rows));
    },
  };
}

test("the chat header finds its own task by session id", async () => {
  const h = await mountHead("b", "/repo/x.py");
  // No row until the listing answers — and that is `pending`, not "no row":
  // the header owes a skeleton there, never the ✻ Claude line, which is a
  // claim about a session the listing has actually been read for.
  expect(h.task()).toBe(null);
  expect(h.pending()).toBe(true);
  await h.serve([row("a"), row("b"), row("c")]);
  expect(h.task()?.key).toBe("b");
  expect(h.pending()).toBe(false);
});

test("…and is NOT narrowed to the pane — the row's identity is the test", async () => {
  const h = await mountHead("elsewhere", "/repo/x.py");
  // A conversation reached by a `session_id` param can be about another file;
  // filtering the header's own row out of its own header would leave the chat
  // nameless.
  await h.serve([row("elsewhere", "/other/y.py")]);
  expect(h.task()?.key).toBe("elsewhere");
});

test("no session, no subscription", async () => {
  const h = await mountHead(null, "/repo/x.py");
  expect(subs.length).toBe(0);
  // And no skeleton either: there is no session for the header to be waiting on.
  expect(h.pending()).toBe(false);
});

test("…and a re-read's skeleton does not drop the header back to the fallback", async () => {
  // Re-entering a chat re-subscribes over a listing the hook already holds, and
  // `subscribeTasks` opens every subscription with `null`. Writing that through
  // blanked the row for the length of the `/api/tasks` round trip, so the
  // topbar printed its `✻ Claude` fallback over a task it could already name.
  const h = await mountHead("b", "/repo/x.py");
  await h.serve([row("a"), row("b")]);
  expect(h.task()?.key).toBe("b");
  await h.serve(null);
  expect(h.task()?.key).toBe("b");
  // …and a real answer still replaces it, skeleton rule or not — and that is
  // the ✻ Claude state, not the skeleton: the listing HAS been read.
  await h.serve([row("a")]);
  expect(h.task()).toBe(null);
  expect(h.pending()).toBe(false);
});

// ---- the dispatched row survives this hook's own narrowing -------------------
// The merge that makes a `pending:<entry>` row and the session it becomes ONE
// row is the feed's (shell/tasksPulse, tasks-lib.mergeTaskChanges); the KEY the
// Recent list draws them under is `tasks-lib.taskListKeys`. Between the two sits
// this hook, which narrows the whole listing to one pane and re-sorts it — so
// the thing worth pinning here is that neither pass drops the waiting row or
// moves its identity out from under it.

test("a waiting row is this pane's row, and keeps its identity when it runs", async () => {
  const { taskListKeys } = await import("@shell/tasks-lib");
  const waiting = {
    key: "pending:e4",
    task_id: "TASK-052",
    project: "/repo",
    target: "/repo/x.py",
    session_id: "",
    status: "queued",
    title: "waiting",
  } as unknown as Task;
  const running = {
    ...waiting, key: "sess-4", session_id: "sess-4", status: "in_progress",
  } as Task;

  const h = await mount("/tpl", "/repo/x.py");
  // A row with NO SESSION is still a row of this pane: `taskInPane` asks about
  // the target, and a message that has not run yet has one.
  await h.serve([waiting]);
  expect(h.rows()?.map((t) => t.key)).toEqual(["pending:e4"]);
  const before = taskListKeys(h.rows() ?? [], true);

  // The feed swaps the two halves in one payload, so the hook only ever sees one
  // of them — and the key it is drawn under has not moved.
  await h.serve([running]);
  expect(h.rows()?.map((t) => t.key)).toEqual(["sess-4"]);
  expect(taskListKeys(h.rows() ?? [], true)).toEqual(before);
  expect(before).toEqual(["TASK-052"]);
  // …and with the flag down, both are drawn under the server's own key.
  expect(taskListKeys(h.rows() ?? [], false)).toEqual(["sess-4"]);
});
