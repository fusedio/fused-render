// PR4's additions to the run loop, against the same fake agent.py: the two
// run-clock hooks, the transcript follower's two half-methods, and the three
// postures `resumeRun` adopts a turn nobody on this page started with.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";

const { ARTIFACTS_EVERY_TICKS, createChatController } = await import("./run-controller");
const { createMemoryParamsStore } = await import("../params/store");

import type { runAgent } from "./agent";
import type { AssistantTurn, ChatController, NoteTurn, UserTurn } from "./controller-api";
import type { PollResponse, Segment } from "./types";

type Handler = (fields: Record<string, unknown>, call: number) => unknown;

function fakeAgent(handlers: Record<string, Handler>) {
  const calls: { action: string; fields: Record<string, unknown> }[] = [];
  const counts: Record<string, number> = {};
  const run = ((_dir: string, action: string, fields: Record<string, unknown>) => {
    calls.push({ action, fields });
    const n = (counts[action] = (counts[action] || 0) + 1) - 1;
    const h = handlers[action];
    if (!h) throw new Error("fake agent has no handler for " + action);
    return Promise.resolve(h(fields, n));
  }) as unknown as typeof runAgent;
  return { run, calls, of: (a: string) => calls.filter((c) => c.action === a) };
}

function poll(over: Partial<PollResponse> = {}): PollResponse {
  return {
    text: "",
    done: false,
    session_id: "s1",
    error: "",
    tokens: 0,
    phase: "composing",
    message: "",
    permissions: [],
    app_state: [],
    mode: "prompt",
    skills: [],
    retry: null,
    retry_total: 0,
    retry_status: 0,
    cancelled: false,
    tasks_pending: false,
    activity: { tool: null, tools_open: 0, hook: "", tool_input_bytes: 0, tasks: [] },
    segments: [],
    ...over,
  } as PollResponse;
}

const text = (t: string): Segment => ({ kind: "text", id: t, text: t }) as Segment;

function makeController(
  handlers: Record<string, Handler>,
  params = createMemoryParamsStore(),
) {
  const agent = fakeAgent(handlers);
  const ticks: number[] = [];
  const ended: number[] = [];
  const abandoned: number[] = [];
  const controller = createChatController({
    file: "/proj/app.py",
    agentDir: "/tpl/claude",
    params,
    run: agent.run,
    sleep: () => Promise.resolve(),
    now: () => 1_000,
    hasPane: () => true,
    onArtifactsTick: () => ticks.push(1),
    onRunEnded: () => ended.push(1),
    onRunAbandoned: () => abandoned.push(1),
  });
  return { controller, agent, params, ticks, ended, abandoned };
}

const users = (c: ChatController) =>
  c.getState().turns.filter((t): t is UserTurn => t.role === "user");
const assistants = (c: ChatController) =>
  c.getState().turns.filter((t): t is AssistantTurn => t.role === "assistant");
const notes = (c: ChatController) =>
  c.getState().turns.filter((t): t is NoteTurn => t.role === "note");

// ---- the run clock ---------------------------------------------------------

describe("the run-clock hooks", () => {
  test("artifacts on the first tick, then every 8th, and once more at the end", async () => {
    const { controller, ticks, ended } = makeController({
      live_host: () => ({ run_id: "" }),
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) =>
        n < ARTIFACTS_EVERY_TICKS + 1
          ? poll()
          : poll({ done: true, text: "done", segments: [text("done")] }),
    });
    await controller.sendMessage("go");
    // The 8-tick spacing IS the cadence (~3.2 s at a 400 ms poll), plus the
    // one at the run's end.
    expect(ticks.length).toBe(3);
    expect(ended.length).toBe(1);
  });

  test("a run that ended while the frame was away still fires both hooks", async () => {
    // `annResolveSent` and `snapInvalidate` hang off `onRunEnded`: the run this
    // frame missed still handled its notes, and may have edited the file.
    const { controller, ended } = makeController({
      poll: () => poll({ done: true, message: "hi", text: "bye", segments: [text("bye")] }),
    });
    await controller.resumeRun("r-gone");
    expect(ended.length).toBe(1);
  });

  test("a stale param hands the notes back WITHOUT claiming a run ended", async () => {
    // T:17792 — the stale-param branch calls `annResolveSent()` and nothing
    // else. `onRunEnded` also bumps the snapshot invalidation, and nothing ran:
    // a bookmarked mid-run URL must not send the landing back to re-read the
    // checkpoint chain.
    const { controller, ended, abandoned } = makeController({
      poll: () => ({ error: "unknown run_id", done: true }),
    });
    await controller.resumeRun("r-dead", { retryUnknown: false });
    expect(abandoned.length).toBe(1);
    expect(ended.length).toBe(0);
    expect(controller.getState().trouble?.kind).toBe("unknown-run");
  });

  test("`retryUnknown: false` takes the answer at its word", async () => {
    const { controller, agent } = makeController({
      poll: () => ({ error: "unknown run_id", done: true }),
    });
    await controller.resumeRun("r-dead", { retryUnknown: false });
    expect(agent.of("poll").length).toBe(1);
  });
});

// ---- resumeRun's three postures --------------------------------------------

describe("resumeRun reconciles against what is already on screen", () => {
  /** A transcript with one user turn, restored the way history restores one. */
  async function withHistory(handlers: Record<string, Handler>) {
    const rig = makeController({
      history: () => ({
        turns: [
          { role: "user", text: "count the rows", uuid: "u1" },
          { role: "assistant", text: "partial…", uuid: "a1" },
        ],
        transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
      }),
      live_run: () => ({ run_id: "" }),
      ...handlers,
    });
    await rig.controller.openSession("s1");
    return rig;
  }

  test("MATCHES: the live turn's own user line is kept and its partial rows dropped", async () => {
    const { controller } = await withHistory({
      poll: (_f, n) =>
        n === 0
          ? poll({ message: "count the rows" })
          : poll({ done: true, message: "count the rows", segments: [text("4 rows")] }),
    });
    await controller.resumeRun("r1");
    expect(users(controller).map((t) => t.text)).toEqual(["count the rows"]);
    // The restored partial is gone: `pollLoop` re-streams the whole turn.
    expect(assistants(controller).map((t) => t.text)).toEqual(["4 rows"]);
  });

  test("NEVERSHOWN: identical text never REPAIRS a turn, and never appends one", async () => {
    // Two rules meet here. The same prompt sent twice is a COINCIDENCE, so it
    // cannot identify this run's own line: no `matches`, and the restored
    // partial rows stay put rather than being stripped as if they belonged to
    // the run being attached.
    //
    // And the text IS on screen, so nothing is appended either (Bugbot PR
    // #1075): `neverShown` is the caller's belief about what it rendered, not a
    // fact about the log, and the printing question is settled by the log.
    const { controller } = await withHistory({
      poll: () =>
        poll({ done: true, message: "count the rows", segments: [text("4 rows")] }),
    });
    await controller.resumeRun("r1", { neverShown: true });
    expect(users(controller).map((t) => t.text)).toEqual(["count the rows"]);
    expect(assistants(controller).map((t) => t.text)).toEqual(["partial…"]);
  });

  test("NEVERSHOWN: a turn nobody has shown is still appended in full", async () => {
    // The other side of the same test: a scheduled run whose turn really is
    // absent from the transcript keeps arriving, question and answer both.
    const { controller } = await withHistory({
      poll: () =>
        poll({ done: true, message: "and the columns?", segments: [text("7")] }),
    });
    await controller.resumeRun("r1", { neverShown: true });
    expect(users(controller).map((t) => t.text)).toEqual([
      "count the rows",
      "and the columns?",
    ]);
    expect(assistants(controller).map((t) => t.text)).toEqual(["partial…", "7"]);
  });

  test("THE TWO CLOCKS: a refresh at 5 s then a schedule tick at 15 s is ONE turn", async () => {
    // The bug this pair of watchers had (Bugbot PR #1075): the standing watch's
    // `refreshHistory` pulls a short scheduled turn in from the transcript, and
    // `history` rows carry no run id, so nothing lands in `shownRuns`. The
    // schedule poll then still believes the id is unattached and resumes it
    // with `neverShown` — which used to append the very turn now on screen.
    const rig = makeController({
      history: (_f, n) => ({
        turns:
          n === 0
            ? [{ role: "user", text: "count the rows", uuid: "u1" }]
            : [
                { role: "user", text: "count the rows", uuid: "u1" },
                // The 5 s refresh finds the scheduled turn already written.
                { role: "user", text: "nightly report", uuid: "u2" },
                { role: "assistant", text: "12 rows", uuid: "a2" },
              ],
        transcript: { path: "/p/s1.jsonl", mtime: n + 1, size: (n + 1) * 10 },
      }),
      live_run: () => ({ run_id: "" }),
      poll: () => poll({ done: true, message: "nightly report", segments: [text("12 rows")] }),
    });
    await rig.controller.openSession("s1");
    await rig.controller.refreshHistory("s1");
    expect(users(rig.controller).map((t) => t.text)).toEqual([
      "count the rows",
      "nightly report",
    ]);
    // 15 s: the poller's tick, on an id `shownRuns` has never heard of.
    expect(rig.controller.hasShownRun("r-sched")).toBe(false);
    await rig.controller.resumeRun("r-sched", { neverShown: true });
    expect(users(rig.controller).map((t) => t.text)).toEqual([
      "count the rows",
      "nightly report",
    ]);
    expect(assistants(rig.controller).map((t) => t.text)).toEqual(["12 rows"]);
  });

  test("QUIET: a turn already on screen prints nothing", async () => {
    const { controller } = await withHistory({
      poll: (_f, n) =>
        n === 0
          ? poll({ message: "count the rows" })
          : poll({ done: true, message: "count the rows", segments: [text("4 rows")] }),
    });
    await controller.resumeRun("r1", { quiet: true });
    expect(users(controller).map((t) => t.text)).toEqual(["count the rows"]);
  });

  test("QUIET: a turn made in ANOTHER TAB brings its own question with it", async () => {
    // A short turn is over before the watch's first look, and dropping it
    // silently was the whole of the second tab's remaining complaint (D415).
    const { controller } = await withHistory({
      poll: () => poll({ done: true, message: "and the columns?", segments: [text("7")] }),
    });
    await controller.resumeRun("r1", { quiet: true });
    expect(users(controller).map((t) => t.text)).toEqual([
      "count the rows",
      "and the columns?",
    ]);
    expect(assistants(controller).map((t) => t.text)).toEqual(["partial…", "7"]);
  });

  test("an in-flight turn nobody showed gets its user line before the stream", async () => {
    const { controller } = await withHistory({
      poll: (_f, n) =>
        n === 0
          ? poll({ message: "and the columns?" })
          : poll({ done: true, message: "and the columns?", segments: [text("7")] }),
    });
    await controller.resumeRun("r1", { quiet: true });
    expect(users(controller).map((t) => t.text)).toEqual([
      "count the rows",
      "and the columns?",
    ]);
  });

  test("A DONE RUN WITH NOTHING TO SAY repairs nothing at all", async () => {
    const { controller } = await withHistory({
      poll: () => poll({ done: true, segments: [text("orphan")] }),
    });
    await controller.resumeRun("r1", { neverShown: true });
    // No message means no user line to hang the reply under, and an assistant
    // row on its own would read as belonging to whatever the reader last said.
    expect(assistants(controller).map((t) => t.text)).toEqual(["partial…"]);
  });

  test("a failure the frame never saw gets its own user line to hang under", async () => {
    const { controller } = await withHistory({
      poll: () => poll({ done: true, error: "the CLI died", message: "run the thing" }),
    });
    await controller.resumeRun("r1", { neverShown: true });
    expect(users(controller).map((t) => t.text)).toEqual([
      "count the rows",
      "run the thing",
    ]);
    expect(controller.getState().trouble?.message).toContain("the CLI died");
  });

  test("QUIET: a FAILED turn made in another tab is appended, not discarded", async () => {
    // The standing watch adopts `quiet: true`, and the done-error branch used to
    // handle only `matches` / an empty log / `neverShown` — so a finished FAILED
    // run from another tab was dropped outright while its succeeding twin was
    // appended (Bugbot PR #1075).
    const { controller } = await withHistory({
      poll: () => poll({ done: true, error: "the CLI died", message: "and the columns?" }),
    });
    await controller.resumeRun("r1", { quiet: true });
    expect(users(controller).map((t) => t.text)).toEqual([
      "count the rows",
      "and the columns?",
    ]);
    expect(controller.getState().trouble?.message).toContain("the CLI died");
  });

  test("QUIET: a failed run with NO message repairs nothing", async () => {
    // The message is the whole of the evidence about what this transcript is
    // already showing, so with none there is no turn to append — the same
    // answer the success branch gives.
    const { controller } = await withHistory({
      poll: () => poll({ done: true, error: "the CLI died" }),
    });
    await controller.resumeRun("r1", { quiet: true });
    expect(users(controller).map((t) => t.text)).toEqual(["count the rows"]);
    expect(controller.getState().trouble).toBe(null);
  });

  test("THE TWO CLOCKS, FAILED: the prompt is on screen and the failure still lands", async () => {
    // The two-clock race, on the failing side (Bugbot PR #1075, third pass):
    // the 5 s `refreshHistory` pulls the scheduled turn in from the transcript,
    // so at the 15 s attach the turn is no longer `unseen` — and the error,
    // which lives in the RUN DIR and not in the transcript, used to be dropped
    // with it. The prompt is not doubled and the failure is not lost.
    const rig = makeController({
      history: (_f, n) => ({
        turns:
          n === 0
            ? [{ role: "user", text: "count the rows", uuid: "u1" }]
            : [
                { role: "user", text: "count the rows", uuid: "u1" },
                { role: "user", text: "nightly report", uuid: "u2" },
              ],
        transcript: { path: "/p/s1.jsonl", mtime: n + 1, size: (n + 1) * 10 },
      }),
      live_run: () => ({ run_id: "" }),
      poll: () => poll({ done: true, error: "the CLI died", message: "nightly report" }),
    });
    await rig.controller.openSession("s1");
    await rig.controller.refreshHistory("s1");
    await rig.controller.resumeRun("r-sched", { neverShown: true });
    expect(users(rig.controller).map((t) => t.text)).toEqual([
      "count the rows",
      "nightly report",
    ]);
    const rows = rig.controller.getState().turns;
    expect(rows.filter((t) => t.role === "error").map((t) => t.text)).toEqual([
      "the CLI died",
    ]);
    expect(rig.controller.getState().trouble?.message).toContain("the CLI died");
    // ...and under the line it belongs to, not floating above it.
    expect(rows[rows.length - 1].role).toBe("error");
  });

  test("A FAILURE THE TRANSCRIPT ALREADY CARRIES is not printed twice", async () => {
    // `historyToTurns` renders a transcript `error` row verbatim, so a run
    // whose failure was written down before the attach is already on screen —
    // the error text itself is the test, and it says there is nothing to add.
    //
    // `neverShown` rather than `quiet`, because that is the caller this can
    // happen to: with it identical text is never `matches`, so the attach
    // reaches the on-screen branch instead of repairing its own line.
    const rig = makeController({
      history: () => ({
        turns: [
          { role: "user", text: "nightly report", uuid: "u1" },
          { role: "error", text: "the CLI died" },
        ],
        transcript: { path: "/p/s1.jsonl", mtime: 1, size: 10 },
      }),
      live_run: () => ({ run_id: "" }),
      poll: () => poll({ done: true, error: "the CLI died", message: "nightly report" }),
    });
    await rig.controller.openSession("s1");
    await rig.controller.resumeRun("r-sched", { neverShown: true });
    expect(users(rig.controller).map((t) => t.text)).toEqual(["nightly report"]);
    expect(
      rig.controller.getState().turns.filter((t) => t.role === "error").map((t) => t.text),
    ).toEqual(["the CLI died"]);
  });
});

// ---- what the page has already shown ---------------------------------------

describe("hasShownRun", () => {
  test("a run this frame re-attached to is never the schedule poller's to resume", async () => {
    const { controller } = makeController({
      live_run: () => ({ run_id: "" }),
      poll: () => poll({ done: true, message: "scheduled thing", segments: [text("ok")] }),
    });
    expect(controller.hasShownRun("r1")).toBe(false);
    await controller.resumeRun("r1", { quiet: true });
    expect(controller.hasShownRun("r1")).toBe(true);
    // And it says nothing about a run nobody here has touched.
    expect(controller.hasShownRun("r2")).toBe(false);
  });

  test("a run the standing watch adopted counts as shown", async () => {
    const { controller } = makeController({
      live_run: () => ({ run_id: "r9" }),
      poll: () => poll({ done: true, message: "another tab", segments: [text("ok")] }),
    });
    await controller.adoptLiveRun("s1", { laps: 1, quiet: true });
    expect(controller.hasShownRun("r9")).toBe(true);
  });

  test("a run this frame SENT counts as shown", async () => {
    const { controller } = makeController({
      live_host: () => ({ run_id: "" }),
      start: () => ({ run_id: "r5" }),
      poll: () => poll({ done: true, text: "done", segments: [text("done")] }),
    });
    await controller.sendMessage("go");
    expect(controller.hasShownRun("r5")).toBe(true);
  });
});

// ---- the follower's two half-methods ---------------------------------------

describe("refreshHistory", () => {
  test("REFRESH MODE: turns and watermark replaced, no skeleton, no session write", async () => {
    const params = createMemoryParamsStore();
    const { controller, agent } = makeController(
      {
        history: (_f, n) => ({
          turns:
            n === 0
              ? [{ role: "user", text: "one", uuid: "u1" }]
              : [
                  { role: "user", text: "one", uuid: "u1" },
                  { role: "assistant", text: "two", uuid: "a1" },
                ],
          transcript: { path: "/p/s1.jsonl", mtime: n + 1, size: (n + 1) * 10 },
        }),
        live_run: () => ({ run_id: "" }),
      },
      params,
    );
    await controller.openSession("s1");
    expect(controller.getState().transcript?.mtime).toBe(1);
    await controller.refreshHistory("s1");
    expect(controller.getState().turns.length).toBe(2);
    // The new watermark comes from the REFETCH's own pre-read stat, never from
    // the probe that triggered it.
    expect(controller.getState().transcript?.mtime).toBe(2);
    expect(controller.getState().historyLoading).toBe(false);
    // One `session_id` write, from `openSession`; a refresh navigates nowhere.
    expect(params.get("session_id")).toBe("s1");
    expect(agent.of("history").length).toBe(2);
  });

  test("a failed refresh leaves the transcript — and the watermark — as they were", async () => {
    const { controller } = makeController({
      history: (_f, n) =>
        n === 0
          ? {
              turns: [{ role: "user", text: "one", uuid: "u1" }],
              transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
            }
          : { error: "the transcript went away" },
      live_run: () => ({ run_id: "" }),
    });
    await controller.openSession("s1");
    await controller.refreshHistory("s1");
    expect(controller.getState().turns.length).toBe(1);
    // NOT advanced, so the next lap tries again.
    expect(controller.getState().transcript?.mtime).toBe(1);
  });

  test("AN ANSWER WITH NO STAT KEEPS THE WATERMARK (T:17663-17665)", async () => {
    // `noteTranscript` writes the mark only when the stat carries a path. Wipe
    // it instead and `followTranscript` bails at "no render to compare against"
    // for the rest of the session — the standing watch goes deaf on the very
    // refresh that just succeeded.
    const { controller } = makeController({
      history: (_f, n) =>
        n === 0
          ? {
              turns: [{ role: "user", text: "one", uuid: "u1" }],
              transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
            }
          : // Rows, no stat.
            {
              turns: [
                { role: "user", text: "one", uuid: "u1" },
                { role: "assistant", text: "two", uuid: "a1" },
              ],
            },
      live_run: () => ({ run_id: "" }),
    });
    await controller.openSession("s1");
    await controller.refreshHistory("s1");
    // The rows landed...
    expect(controller.getState().turns.length).toBe(2);
    // ...and the watch can still compare against something.
    expect(controller.getState().transcript?.path).toBe("/p/s1.jsonl");
    expect(controller.getState().transcript?.mtime).toBe(1);
  });
});

describe("transcriptGen", () => {
  test("ONLY A TRANSCRIPT REPLACEMENT COUNTS — not a session id (T:18000)", async () => {
    // T calls `scheduleResetForNewTranscript()` from `loadHistory`'s non-refresh
    // branch and from nowhere else. Keyed on the session id instead, the reset
    // also fires on MOUNT and on the id the first poll of a brand-new chat
    // reports — MID-RUN, where re-arming the baseline lets the next tick
    // silently write off a scheduled run that fired in that window.
    const params = createMemoryParamsStore();
    const { controller } = makeController(
      {
        history: () => ({ turns: [], transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 } }),
        live_run: () => ({ run_id: "" }),
        live_host: () => ({ run_id: "" }),
        start: () => ({ run_id: "r1" }),
        poll: () => poll({ done: true, text: "hi", segments: [text("hi")] }),
      },
      params,
    );
    // Nothing has been replaced yet: the mount reads 0 and skips the reset.
    expect(controller.getState().transcriptGen).toBe(0);

    // A BRAND-NEW CHAT: the session id arrives from the first poll payload,
    // mid-run. That is not a replacement.
    await controller.sendMessage("go");
    expect(controller.getState().sessionId).toBe("s1");
    expect(controller.getState().transcriptGen).toBe(0);

    // A REFRESH is not one either — the same conversation, re-rendered.
    await controller.refreshHistory("s1");
    expect(controller.getState().transcriptGen).toBe(0);

    // Opening a conversation IS: the visible transcript is replaced wholesale.
    await controller.openSession("s2");
    expect(controller.getState().transcriptGen).toBe(1);
    await controller.openSession("s3");
    expect(controller.getState().transcriptGen).toBe(2);
  });
});

describe("ownRunEndedAt", () => {
  // The stamp is a fact about ROWS — "this frame wrote the tail of the
  // conversation on screen" — and `followDecision`'s own-echo rule reads it to
  // tell rows this page just wrote from somebody else's turn landing over the
  // top of them (D415). Carried into the NEXT conversation it is a lie about a
  // transcript this page has never written a row into, and the guard then
  // swallows the first outside turn to arrive (Bugbot, PR #1075).

  test("a finished own run stamps the clock", async () => {
    const { controller } = makeController({
      live_host: () => ({ run_id: "" }),
      start: () => ({ run_id: "r1" }),
      poll: () => poll({ done: true, text: "hi", segments: [text("hi")] }),
    });
    await controller.sendMessage("go");
    expect(controller.getState().ownRunEndedAt).toBe(1_000);
  });

  test("LEAVING CLEARS IT: an abandoned loop's late finally cannot stamp the fresh chat", async () => {
    // `newChat` bumps `logGen`, so the loop it left mid-turn bails — but its
    // `finally` still runs, and the stamp used to be written there
    // unconditionally, straight onto the state `newChat` had just emptied.
    let ctl: ChatController | null = null;
    const { controller } = makeController({
      live_host: () => ({ run_id: "" }),
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) => {
        // The reader presses Back mid-turn; the run keeps going server-side.
        if (n === 0) ctl!.newChat();
        return poll({ done: true, text: "hi", segments: [text("hi")] });
      },
    });
    ctl = controller;
    await controller.sendMessage("go");
    expect(controller.getState().turns.length).toBe(0);
    expect(controller.getState().ownRunEndedAt).toBe(0);
  });

  test("SWITCHING CLEARS IT: openSession does not inherit the last chat's stamp", async () => {
    // `openSession` is the OTHER way the visible transcript is replaced, and
    // the one `logGen` cannot see — it holds the `sending` gate instead of
    // bumping the generation, so the stamp has to be cleared by hand.
    const { controller } = makeController({
      live_host: () => ({ run_id: "" }),
      live_run: () => ({ run_id: "" }),
      start: () => ({ run_id: "r1" }),
      poll: () => poll({ done: true, text: "hi", segments: [text("hi")] }),
      history: () => ({ turns: [], transcript: { path: "/p/s2.jsonl", mtime: 1, size: 2 } }),
    });
    await controller.sendMessage("go");
    expect(controller.getState().ownRunEndedAt).toBe(1_000);
    await controller.openSession("s2");
    expect(controller.getState().ownRunEndedAt).toBe(0);
  });
});

describe("setExternalWorking", () => {
  test("a line with no stop button and no token count", () => {
    const { controller } = makeController({});
    controller.setExternalWorking(true);
    const w = controller.getState().working;
    expect(w?.external).toBe(true);
    expect(w?.phase).toBe("external");
    expect(w?.tokens).toBe(0);
    controller.setExternalWorking(false);
    expect(controller.getState().working).toBeNull();
  });

  test("IDEMPOTENT: a lap that says the same thing is not a frame", () => {
    const { controller } = makeController({});
    controller.setExternalWorking(true);
    const first = controller.getState().working;
    controller.setExternalWorking(true);
    // Read in a layout effect, so a no-op emit is a wasted frame every 5 s.
    expect(controller.getState().working).toBe(first);
  });

  test("IT NEVER SPEAKS OVER A RUN THIS FRAME OWNS", async () => {
    const { controller } = makeController({
      live_host: () => ({ run_id: "" }),
      start: () => ({ run_id: "r1" }),
      // A turn that never finishes: the follower's lap lands mid-stream, which
      // is precisely when the two lines could fight over the seat.
      poll: () => poll({ text: "streaming" }),
    });
    const streaming = controller.sendMessage("go");
    // Let the start round-trip and the first poll land.
    for (let i = 0; i < 6; i++) await Promise.resolve();
    expect(controller.getState().working?.external).toBeUndefined();
    controller.setExternalWorking(true);
    // The real line has a stop button and a token count; this one has neither,
    // and a control that quietly does nothing is worse than no control at all.
    expect(controller.getState().working?.external).toBeUndefined();
    // ...and the OFF direction leaves a real line alone too.
    const own = controller.getState().working;
    controller.setExternalWorking(false);
    expect(controller.getState().working).toBe(own);
    controller.dispose();
    await streaming;
  });
});

describe("addNote", () => {
  test("the scheduled-run rows carry ◷", () => {
    const { controller } = makeController({});
    controller.addNote("Your scheduled message is running now.");
    expect(notes(controller).map((n) => [n.glyph, n.text])).toEqual([
      ["◷", "Your scheduled message is running now."],
    ]);
  });
});

describe("adoptLiveRun's laps", () => {
  test("the standing watch takes ONE lap: it is re-armed by its own triggers", async () => {
    const { controller, agent } = makeController({ live_run: () => ({ run_id: "" }) });
    await controller.adoptLiveRun("s1", { laps: 1, quiet: true });
    expect(agent.of("live_run").length).toBe(1);
  });

  test("opening a chat keeps all eight — a run can start a moment later", async () => {
    const { controller, agent } = makeController({ live_run: () => ({ run_id: "" }) });
    await controller.adoptLiveRun("s1");
    expect(agent.of("live_run").length).toBe(8);
  });

  test("`quiet` rides through to the reconciliation", async () => {
    const { controller } = makeController({
      history: () => ({
        turns: [{ role: "user", text: "already here", uuid: "u1" }],
        transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
      }),
      live_run: (_f, n) => ({ run_id: n === 0 ? "r1" : "" }),
      poll: () => poll({ done: true, message: "already here", segments: [text("ok")] }),
    });
    await controller.openSession("s1");
    await controller.adoptLiveRun("s1", { laps: 1, quiet: true });
    // Printed nothing over the line it is already showing.
    expect(users(controller).map((t) => t.text)).toEqual(["already here"]);
  });
});
