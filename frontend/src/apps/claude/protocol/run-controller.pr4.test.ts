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

  test("BOOT KEEPS THE CARD; the adoption roads recover in silence (P4-05)", async () => {
    // The card is right for a bookmarked mid-run URL — the reader put that id
    // there. The same road is taken by the standing watch and the schedule
    // poller, whose ids come from `live_run`: the run ends and its dir is
    // pruned before the probe lands, and a reader who touched NOTHING got
    // "That turn is no longer running" over a healthy transcript. T recovered
    // from those in silence (T:17787-17795).
    const boot = makeController({ poll: () => ({ error: "unknown run_id", done: true }) });
    await boot.controller.resumeRun("r-dead", { retryUnknown: false });
    expect(boot.controller.getState().trouble?.kind).toBe("unknown-run");

    const watched = makeController({ poll: () => ({ error: "unknown run_id", done: true }) });
    await watched.controller.resumeRun("r-dead", { quiet: true });
    expect(watched.controller.getState().trouble).toBe(null);
    // The notes still come back, and no run is claimed to have ended.
    expect(watched.abandoned.length).toBe(1);
    expect(watched.ended.length).toBe(0);

    const scheduled = makeController({ poll: () => ({ error: "unknown run_id", done: true }) });
    await scheduled.controller.resumeRun("r-dead", { neverShown: true });
    expect(scheduled.controller.getState().trouble).toBe(null);
    expect(scheduled.abandoned.length).toBe(1);
  });

  test("THE RETRY IS OPT-IN, and only boot opts in (P4-18)", async () => {
    // T:17749, 17776-17786. The five 700 ms waits exist for one case — "a frame
    // handed a run id by its EMBEDDER can boot before the freshly created run
    // dir is visible to the agent" — and they are spent inside the `sending`
    // gate, where the composer refuses a send and the watch cannot lap.
    const boot = makeController({ poll: () => ({ error: "unknown run_id", done: true }) });
    await boot.controller.resumeRun("r-boot");
    expect(boot.agent.of("poll").length).toBe(1 + 5);

    const watched = makeController({ poll: () => ({ error: "unknown run_id", done: true }) });
    await watched.controller.resumeRun("r-watched", { quiet: true });
    expect(watched.agent.of("poll").length).toBe(1);

    const scheduled = makeController({ poll: () => ({ error: "unknown run_id", done: true }) });
    await scheduled.controller.resumeRun("r-sched", { neverShown: true });
    expect(scheduled.agent.of("poll").length).toBe(1);

    // AN EXPLICIT FLAG STILL WINS, in both directions.
    const forced = makeController({ poll: () => ({ error: "unknown run_id", done: true }) });
    await forced.controller.resumeRun("r-forced", { quiet: true, retryUnknown: true });
    expect(forced.agent.of("poll").length).toBe(1 + 5);
  });

  test("THE CARET GOES BACK IN THE BOX on every re-attach road (T:17866, P4-17)", async () => {
    const focused: number[] = [];
    const agent = fakeAgent({ poll: () => ({ error: "unknown run_id", done: true }) });
    const controller = createChatController({
      file: "/proj/app.py",
      agentDir: "/tpl/claude",
      params: createMemoryParamsStore(),
      run: agent.run,
      sleep: () => Promise.resolve(),
      now: () => 1_000,
      focusComposer: () => focused.push(1),
    });
    // The stale-param road — the one that never reaches the poll loop at all,
    // and the one T's `finally` exists to cover.
    await controller.resumeRun("r-dead", { retryUnknown: false });
    expect(focused.length).toBe(1);
    // An adoption mid-session is the case `autoFocus` never covered.
    await controller.resumeRun("r-dead2", { quiet: true });
    expect(focused.length).toBe(2);
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

  /** The JSONL as `_history` reads it: the leader's turn, and — once the
   *  follow-up's rows have been flushed — the follow-up's own prompt and reply
   *  after it. `n` is the call number, so call 0 is the restore and every later
   *  call is a refresh. */
  const movedWindowHistory =
    (flushed: boolean) =>
    (_f: Record<string, unknown>, n: number) => ({
      turns:
        n === 0 || !flushed
          ? [
              { role: "user", text: "count the rows", uuid: "u1" },
              { role: "assistant", text: "SECOND", uuid: "a1" },
            ]
          : [
              { role: "user", text: "count the rows", uuid: "u1" },
              { role: "assistant", text: "SECOND", uuid: "a1" },
              { role: "user", text: "and the columns?", uuid: "u2" },
              { role: "assistant", text: "THIRD", uuid: "a2" },
            ],
      transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
    });

  test("A MOVED WINDOW REFRESHES: the follow-up lands with its own line, not as an orphan reply", async () => {
    // THE BUG (browser QA round 2, then round-3 review, 2026-09-12). A chat
    // adopted by `openSession` after its leader ran shows the leader's line and
    // the leader's reply. A queued FOLLOWER is then dispatched into the same
    // host — same run, same `out.jsonl` — and the probe still reports `message`
    // as the run's ORIGINAL first message, because that is what `message` means.
    // So `matches` is true while the payload above the moved cursor is the
    // FOLLOW-UP'S reply.
    //
    // Round 2 stopped the strip, which saved "SECOND" and left the other half
    // standing: "THIRD" was appended under the prompt that produced "SECOND",
    // an orphan whose own user line was nowhere on screen. `poll.window` above
    // zero says the payload cannot speak for the line that matched at all — so
    // neither half of the repair fires, and the transcript that holds BOTH rows
    // is asked instead.
    const rig = makeController({
      history: movedWindowHistory(true),
      live_run: () => ({ run_id: "" }),
      poll: () =>
        poll({ done: true, message: "count the rows", window: 4096, segments: [text("THIRD")] }),
    });
    await rig.controller.openSession("s1");
    await rig.controller.resumeRun("r1");
    // The refresh happened, and it is what put the rows on screen.
    expect(rig.agent.of("history").length).toBe(2);
    expect(users(rig.controller).map((t) => t.text)).toEqual([
      "count the rows",
      "and the columns?",
    ]);
    expect(assistants(rig.controller).map((t) => t.text)).toEqual(["SECOND", "THIRD"]);
    // In the file's order: every reply sits under the prompt it answers.
    expect(rig.controller.getState().turns.map((t) => t.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
  });

  test("A MOVED WINDOW REFRESHES ON THE ERROR ROAD TOO: the failure hangs under the right line", async () => {
    // THE OTHER HALF OF THE SAME REPAIR (Bugbot, PR #1124). Everything the
    // success path knows about a moved cursor was true of a FAILED follower and
    // the error path asked none of it: `matches` still points at the leader's
    // first prompt, so the failure was printed under the line that produced the
    // reply above it — a CLI death blamed on the wrong message — and the
    // follow-up's own prompt never came in at all, because only the refresh
    // brings it.
    const rig = makeController({
      history: movedWindowHistory(true),
      live_run: () => ({ run_id: "" }),
      poll: () =>
        poll({
          done: true,
          message: "count the rows",
          window: 4096,
          error: "the CLI died",
        }),
    });
    await rig.controller.openSession("s1");
    await rig.controller.resumeRun("r1");
    // The transcript was asked, exactly as it is when the turn succeeded.
    expect(rig.agent.of("history").length).toBe(2);
    expect(users(rig.controller).map((t) => t.text)).toEqual([
      "count the rows",
      "and the columns?",
    ]);
    // …and the failure is the LAST row, which is what puts it under the
    // follow-up's own line rather than under the leader's.
    expect(rig.controller.getState().turns.map((t) => t.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
      "error",
    ]);
    expect(rig.controller.getState().trouble?.message).toBe("the CLI died");
  });

  test("A MOVED WINDOW REFRESHES ON THE ERROR ROAD: a failure the file already carries is not doubled", async () => {
    // The refresh can bring the failure in with the prompt — `historyToTurns`
    // maps a transcript `error` row to its text verbatim — and printing the
    // probe's copy under it would be one death said twice. The same
    // `errorShown` rule the `shownAlready` branch reads.
    const rig = makeController({
      history: (_f: Record<string, unknown>, n: number) => ({
        turns:
          n === 0
            ? [
                { role: "user", text: "count the rows", uuid: "u1" },
                { role: "assistant", text: "SECOND", uuid: "a1" },
              ]
            : [
                { role: "user", text: "count the rows", uuid: "u1" },
                { role: "assistant", text: "SECOND", uuid: "a1" },
                { role: "user", text: "and the columns?", uuid: "u2" },
                { role: "error", text: "the CLI died", uuid: "e1" },
              ],
        transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
      }),
      live_run: () => ({ run_id: "" }),
      poll: () =>
        poll({
          done: true,
          message: "count the rows",
          window: 4096,
          error: "the CLI died",
        }),
    });
    await rig.controller.openSession("s1");
    await rig.controller.resumeRun("r1");
    expect(
      rig.controller
        .getState()
        .turns.filter((t) => t.role === "error")
        .map((t) => t.text),
    ).toEqual(["the CLI died"]);
  });

  test("A MOVED WINDOW REFRESHES: the same on the LIVE road, and polling carries on", async () => {
    // The turn was still running when the chat attached, so the probe hands off
    // to `pollLoop` — which renders the window it is given and has no way to put
    // back a row the strip took, nor a user line the payload never carried. The
    // refresh supplies that line first; the partial beneath it goes, because
    // that is precisely what the loop re-renders; then the loop streams.
    const rig = makeController({
      history: movedWindowHistory(true),
      live_run: () => ({ run_id: "" }),
      poll: (_f, n) =>
        n === 0
          ? poll({ message: "count the rows", window: 4096, text: "and the" })
          : poll({
              done: true,
              message: "count the rows",
              window: 4096,
              segments: [text("THIRD")],
            }),
    });
    await rig.controller.openSession("s1");
    await rig.controller.resumeRun("r1");
    expect(rig.agent.of("history").length).toBe(2);
    expect(users(rig.controller).map((t) => t.text)).toEqual([
      "count the rows",
      "and the columns?",
    ]);
    // "THIRD" once — the refresh's copy stripped, the loop's copy streamed —
    // and "SECOND" still there, which is the row round 2 was about.
    expect(assistants(rig.controller).map((t) => t.text)).toEqual(["SECOND", "THIRD"]);
    // And the loop really ran: it polled past the probe.
    expect(rig.agent.of("poll").length).toBeGreaterThan(1);
  });

  test("A MOVED WINDOW NEVER STRIPS ON A GUESS: an unflushed follow-up row costs no reply", async () => {
    // The refusal that keeps the round-2 fix intact. If the follow-up's user row
    // has not reached the JSONL yet, the refreshed transcript still ends at the
    // leader's line — and stripping beneath THAT line would delete "SECOND"
    // exactly as the original bug did. So the strip is anchored on a line that
    // is demonstrably not the one that matched, and here there is none: the
    // reply before is kept, and the new one is appended rather than lost.
    const rig = makeController({
      history: movedWindowHistory(false),
      live_run: () => ({ run_id: "" }),
      poll: (_f, n) =>
        n === 0
          ? poll({ message: "count the rows", window: 4096, text: "and the" })
          : poll({
              done: true,
              message: "count the rows",
              window: 4096,
              segments: [text("THIRD")],
            }),
    });
    await rig.controller.openSession("s1");
    await rig.controller.resumeRun("r1");
    expect(users(rig.controller).map((t) => t.text)).toEqual(["count the rows"]);
    expect(assistants(rig.controller).map((t) => t.text)).toEqual(["SECOND", "THIRD"]);
  });

  test("AN UNMOVED WINDOW STILL STRIPS: the guard is the cursor, not the match", async () => {
    // The other side of it, and the behaviour the strip exists for: window 0 —
    // or an older agent.py that sends none at all — is a payload that really
    // does still hold the whole turn under that user line, so the restored
    // partial goes and `pollLoop` re-renders it. The MATCHES test above is this
    // with no `window` field; this one pins the explicit zero.
    const { controller } = await withHistory({
      poll: () =>
        poll({ done: true, message: "count the rows", window: 0, segments: [text("4 rows")] }),
    });
    await controller.resumeRun("r1");
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

  test("THE DONE-REPAIR STAMPS IT TOO (Bugbot 3975677791)", async () => {
    // A run that finished while this frame was away is reconciled from the
    // probe payload, never through `pollLoop` — so this road stamped nothing,
    // the watermark stayed at 0, and `followDecision`'s own-echo guard
    // (`probe.mtime > ownEnd`) then read the rows this repair had just
    // accounted for as somebody else's turn and raised the external working
    // line over them.
    const { controller } = makeController({
      live_run: () => ({ run_id: "r9" }),
      poll: () => poll({ done: true, message: "another tab", segments: [text("ok")] }),
    });
    await controller.adoptLiveRun("s1", { laps: 1, quiet: true });
    expect(controller.getState().ownRunEndedAt).toBe(1_000);
  });

  test("...and on the repair's ERROR road, which appends rows just the same", async () => {
    const { controller } = makeController({
      live_run: () => ({ run_id: "r9" }),
      poll: () => poll({ done: true, message: "another tab", error: "claude exited" }),
    });
    await controller.adoptLiveRun("s1", { laps: 1, quiet: true });
    expect(controller.getState().ownRunEndedAt).toBe(1_000);
  });

  test("but not into a transcript that has since been REPLACED", async () => {
    // The stamp is a fact about ROWS, so a repair whose conversation is gone has
    // nothing to say about the one that replaced it. (`newChat` bumps `logGen`,
    // which this attach already bails on straight after the probe; the
    // `transcriptGen` half of the guard covers the `openSession` road, which
    // holds the `sending` gate instead of bumping the generation.)
    let ctl: ChatController | null = null;
    const { controller } = makeController({
      live_run: () => ({ run_id: "r9" }),
      poll: () => {
        ctl!.newChat();
        return poll({ done: true, message: "another tab", segments: [text("ok")] });
      },
    });
    ctl = controller;
    await controller.adoptLiveRun("s1", { laps: 1, quiet: true });
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

// ---- a repaired turn is scrolled to (P4-10) --------------------------------

describe("a run that finished while the frame was away", () => {
  test("A REPAIR BUMPS `repaired`, so the renderer can scroll to it (T:17851)", async () => {
    // A repair appends a whole turn in ONE commit: there is no `running` →
    // `idle` edge for the settle-scroll to hang off, and the follow-tail rule
    // only pins a reader already at the bottom. So the reader this case is
    // ABOUT — one who scrolled up and came back — saw nothing appear. T scrolls
    // unconditionally; the nonce is how the renderer is told to.
    const { controller } = makeController({
      poll: () =>
        poll({
          done: true,
          message: "what changed?",
          text: "these three files",
          segments: [text("these three files")],
        }),
    });
    expect(controller.getState().repaired).toBe(0);
    await controller.resumeRun("r-done", { neverShown: true });
    expect(controller.getState().repaired).toBe(1);
    expect(assistants(controller).map((t) => t.text)).toEqual(["these three files"]);
  });

  test("A NONCE, not a flag: two repairs are two scrolls", async () => {
    // TWO DIFFERENT TURNS, because the nonce tracks appended ROWS rather than
    // trips through the branch (Bugbot 3974975062): the same message twice is
    // already on screen the second time, and a scroll for no new content is
    // just a reader losing their place.
    const { controller } = makeController({
      poll: (_f, n) =>
        poll({ done: true, message: "again " + n, text: "and again " + n }),
    });
    await controller.resumeRun("r-1", { neverShown: true });
    await controller.resumeRun("r-2", { neverShown: true });
    expect(users(controller).length).toBe(2);
    expect(controller.getState().repaired).toBe(2);
  });

  test("a STALE id repairs nothing, so nothing is scrolled to", async () => {
    const { controller } = makeController({
      poll: () => ({ error: "unknown run_id", done: true }),
    });
    await controller.resumeRun("r-dead", { quiet: true });
    expect(controller.getState().repaired).toBe(0);
  });
});

// ---- the writer does not adopt the reply it just streamed -----------------

describe("shownRuns is READ, not only written (Bugbot, PR4 batch)", () => {
  test("a run this frame streamed is never re-adopted by the watch", async () => {
    // `noteChatActivity` announces at BOTH turn boundaries, and at the end one
    // the `busy()` gate is already down — so the tile that ran the turn hears
    // its own poke, `live_run` still answers the id for a few seconds, and this
    // frame would re-adopt the reply it just streamed: the done branch strips
    // and rebuilds the turn, bumps `repaired` (a forced scroll) and takes the
    // caret back.
    const { controller, agent } = makeController({
      start: () => ({ run_id: "r-mine" }),
      poll: () => poll({ done: true, session_id: "s1", text: "streamed" }),
      live_host: () => ({ run_id: "" }),
      // The run has ended but its dir has not been pruned yet, which is the
      // whole window.
      live_run: () => ({ run_id: "r-mine" }),
      history: () => ({ turns: [] }),
    });
    await controller.sendMessage("go");
    const before = assistants(controller).map((t) => t.text);
    expect(before).toEqual(["streamed"]);
    const repairedBefore = controller.getState().repaired;
    const pollsBefore = agent.of("poll").length;

    // The writer's own poke, which is what the live watch turns into a tick.
    const liveBefore = agent.of("live_run").length;
    await controller.adoptLiveRun("s1", { laps: 1, quiet: true });

    // THE LOOKUP REALLY HAPPENED — so this is the `shownRuns` skip and not some
    // earlier gate quietly making the test pass.
    expect(agent.of("live_run").length).toBe(liveBefore + 1);
    // Nothing was re-resumed: no extra probe, no rebuilt turn, no scroll.
    expect(agent.of("poll").length).toBe(pollsBefore);
    expect(assistants(controller).map((t) => t.text)).toEqual(before);
    expect(controller.getState().repaired).toBe(repairedBefore);
  });

  test("...but a run THIS FRAME NEVER SHOWED is still adopted", async () => {
    // The turn made in another tab — the whole point of the standing watch.
    const { controller } = makeController({
      live_run: () => ({ run_id: "r-theirs" }),
      poll: () => poll({ done: true, message: "from the other tab", text: "its reply" }),
      history: () => ({ turns: [] }),
    });
    await controller.adoptLiveRun("s1", { laps: 1, quiet: true });
    expect(users(controller).map((t) => t.text)).toEqual(["from the other tab"]);
    expect(assistants(controller).map((t) => t.text)).toEqual(["its reply"]);
  });
});

// ---- the half of "in flight" that ChatState cannot see --------------------

test("isBusy() covers a send INSIDE the gate, which no ChatState field does", async () => {
  // `leftLive` (P4-21) is answered from the Back gesture, and the window the
  // two write-covering reads exist for is exactly a send that has entered the
  // gate with no run id yet: `status` is not "running", `runId` is null and
  // `queued` is empty, so every ChatState reading of "in flight" says no
  // (Bugbot, this batch).
  const agent = fakeAgent({
    defaults: () => ({}),
    live_host: () => ({ run_id: "" }),
    // The spawn never answers, so the controller is parked inside `sending`.
    start: () => new Promise(() => {}),
  });
  const controller = createChatController({
    file: "/proj/app.py",
    agentDir: "/tpl/claude",
    params: createMemoryParamsStore(),
    run: agent.run,
    sleep: () => Promise.resolve(),
    now: () => 1_000,
  });
  void controller.sendMessage("go");
  for (let i = 0; i < 6; i++) await Promise.resolve();

  const s = controller.getState();
  expect(s.status).not.toBe("running");
  expect(s.runId).toBeFalsy();
  expect(s.queued.length).toBe(0);
  // ...and the controller still knows.
  expect(controller.isBusy()).toBe(true);
});

// ---- the mark goes down on ATTACHMENT, never on intent (batch review F1) ---

describe("shownRuns records what was ATTACHED, not what was attempted", () => {
  test("A TRANSIENTLY FAILED PROBE IS TRIED AGAIN ON THE NEXT LAP", async () => {
    // The loop's own recovery was unreachable: the mark went down in
    // `adoptWatch` before `resumeRun` and again at the top of `resumeAttach`
    // before the probe, so the next lap hit `shownRuns.has(id)` and skipped the
    // id for the life of the page — a live run whose first probe threw was
    // never adopted by the run-dir road at all, and only the coarse transcript
    // follower recovered it (no streaming chrome, whole-turn granularity).
    const { controller, agent } = makeController({
      live_run: () => ({ run_id: "r1" }),
      poll: (_f, n) => {
        if (n === 0) throw new Error("socket dropped");
        return poll({ done: true, message: "from the other tab", text: "its reply" });
      },
      history: () => ({ turns: [] }),
    });
    await controller.adoptLiveRun("s1", { laps: 2, quiet: true });
    // Two probes, because the first one's failure did not write the id off.
    expect(agent.of("poll").length).toBe(2);
    expect(users(controller).map((t) => t.text)).toEqual(["from the other tab"]);
    expect(assistants(controller).map((t) => t.text)).toEqual(["its reply"]);
  });

  test("a STALE id is not marked either: nothing about it is on screen", async () => {
    // The `unknown run_id` branch is passed BEFORE the mark, and it must be:
    // nothing was reconciled there, so recording the id would be a claim this
    // transcript is showing a turn it never saw — and the schedule poller reads
    // that claim.
    const { controller } = makeController({
      poll: () => ({ error: "unknown run_id", done: true }),
    });
    await controller.resumeRun("r-dead", { quiet: true });
    expect(controller.hasShownRun("r-dead")).toBe(false);
    expect(users(controller).length).toBe(0);
  });

  test("...while a run genuinely ATTACHED is still refused a second time", async () => {
    // The half that must not regress: past the stale-id check the run is ours,
    // so the schedule poller and a later lap both have to be turned away
    // (Bugbot PR #1075).
    const { controller, agent } = makeController({
      live_run: () => ({ run_id: "r1" }),
      poll: () => poll({ done: true, message: "once", text: "only once" }),
      history: () => ({ turns: [] }),
    });
    await controller.adoptLiveRun("s1", { laps: 1, quiet: true });
    expect(users(controller).map((t) => t.text)).toEqual(["once"]);
    expect(controller.hasShownRun("r1")).toBe(true);
    const polls = agent.of("poll").length;
    await controller.adoptLiveRun("s1", { laps: 1, quiet: true });
    expect(agent.of("poll").length).toBe(polls);
    expect(users(controller).map((t) => t.text)).toEqual(["once"]);
  });

  test("a claim in flight also answers `hasShownRun`, so nothing attaches behind it", async () => {
    // What the early write was ALSO doing, and the half that was right: while
    // this frame is probing an id, the schedule poller must not attach to the
    // same run behind it (Bugbot PR #1075).
    let release: (() => void) | null = null;
    const { controller } = makeController({
      poll: () =>
        new Promise((res) => {
          release = () => res(poll({ done: true, message: "m", text: "t" }));
        }),
    });
    const attaching = controller.resumeRun("r-claimed", { neverShown: true });
    for (let i = 0; i < 6; i++) await Promise.resolve();
    expect(controller.hasShownRun("r-claimed")).toBe(true);
    release!();
    await attaching;
    // And it is a SHOWN run once it lands, not merely a claim.
    expect(controller.hasShownRun("r-claimed")).toBe(true);
  });

  test("BACK DURING AN ATTACH LETS THE CLAIM GO (Bugbot 3975433059)", async () => {
    // `newChat` cleared `shownRuns` but not the claims, and `hasShownRun`
    // counts a claim as shown — so Back in the middle of an adopted, scheduled
    // or `?run=` attach kept that id claimed until the abandoned probe
    // returned, and for the rest of the page if the poll hung. `adoptWatch`
    // skipped the re-attach for exactly as long.
    const releases: Array<() => void> = [];
    const { controller } = makeController({
      poll: () =>
        new Promise((res) => {
          releases.push(() => res(poll({ done: true, message: "m", text: "t" })));
        }),
      live_host: () => ({ run_id: "" }),
      history: () => ({ turns: [] }),
    });
    const first = controller.resumeRun("r-claimed", { neverShown: true });
    for (let i = 0; i < 6; i++) await Promise.resolve();
    expect(controller.hasShownRun("r-claimed")).toBe(true);

    // Back. It releases the send gate too, so the watch's next lap can really
    // reach this id again.
    controller.newChat();
    expect(controller.hasShownRun("r-claimed")).toBe(false);

    // AND THE ABANDONED ATTACH'S OWN EXIT MUST NOT LET GO OF THE FRESH CLAIM:
    // the release is conditional on still holding the seat it took.
    const second = controller.resumeRun("r-claimed", { neverShown: true });
    for (let i = 0; i < 6; i++) await Promise.resolve();
    expect(releases.length).toBe(2);
    expect(controller.hasShownRun("r-claimed")).toBe(true);

    releases[0]();
    await first;
    // The first attach has exited; the second one's claim is still standing.
    expect(controller.hasShownRun("r-claimed")).toBe(true);

    releases[1]();
    await second;
    // And the run it did attach to is a SHOWN run now, not a claim.
    expect(controller.hasShownRun("r-claimed")).toBe(true);
  });

  test("`openSession` lets the claims go too", async () => {
    // The reachable shape: the first probe says the run is LIVE, so
    // `resumeAttach` frees the `sending` gate and hands off to `pollLoop` while
    // still holding its claim. A restore from the session list can then land in
    // the middle of it — and must not leave the id claimed by a loop whose
    // transcript it has just replaced.
    const { controller } = makeController({
      poll: (_f, n) => (n === 0 ? poll() : new Promise(() => {})),
      history: () => ({
        turns: [],
        transcript: { path: "/p/s2.jsonl", mtime: 1, size: 2 },
      }),
      live_run: () => ({ run_id: "" }),
    });
    void controller.resumeRun("r-live", { neverShown: true });
    for (let i = 0; i < 10; i++) await Promise.resolve();
    expect(controller.hasShownRun("r-live")).toBe(true);
    await controller.openSession("s2");
    expect(controller.hasShownRun("r-live")).toBe(false);
  });

  test("a probe that THREW lets the claim go", async () => {
    const { controller } = makeController({
      poll: () => {
        throw new Error("socket dropped");
      },
    });
    await controller.resumeRun("r-thrown", { quiet: true });
    expect(controller.hasShownRun("r-thrown")).toBe(false);
  });
});

// ---- Back mid-turn re-attaches (Bugbot 3974975055) ------------------------

describe("a replaced transcript puts every run back in play", () => {
  test("BACK MID-TURN RE-ATTACHES: `newChat` clears the listing", async () => {
    // Back is live mid-turn — the run keeps going server-side and the session
    // list is meant to re-attach to it. With the id still recorded from
    // `pollLoop`, `adoptWatch` refused it and the reader got only the external
    // "Running outside this app" line: no stop control, no token count.
    const { controller } = makeController({
      start: () => ({ run_id: "r-mine" }),
      live_host: () => ({ run_id: "" }),
      poll: () => poll({ done: true, session_id: "s1", message: "go", text: "streamed" }),
      live_run: () => ({ run_id: "r-mine" }),
      history: () => ({ turns: [] }),
    });
    await controller.sendMessage("go");
    expect(controller.hasShownRun("r-mine")).toBe(true);

    controller.newChat();
    expect(controller.hasShownRun("r-mine")).toBe(false);
    await controller.adoptLiveRun("s1", { laps: 1, quiet: true });
    // The turn is back, and by the run-dir road: a real re-attach.
    expect(users(controller).map((t) => t.text)).toEqual(["go"]);
    expect(assistants(controller).map((t) => t.text)).toEqual(["streamed"]);
  });

  test("`openSession` clears it too — the rows arriving know no run ids", async () => {
    const { controller } = makeController({
      start: () => ({ run_id: "r-mine" }),
      live_host: () => ({ run_id: "" }),
      poll: () => poll({ done: true, session_id: "s1", message: "go", text: "streamed" }),
      live_run: () => ({ run_id: "" }),
      history: () => ({
        turns: [],
        transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
      }),
    });
    await controller.sendMessage("go");
    expect(controller.hasShownRun("r-mine")).toBe(true);
    await controller.openSession("s1");
    expect(controller.hasShownRun("r-mine")).toBe(false);
  });
});

// ---- the quiet roads stay quiet when the PROBE ITSELF fails (F2) ----------

describe("a thrown probe is a card only for an id the caller supplied (P4-05)", () => {
  const thrower = { poll: () => Promise.reject(new Error("server went away")) };

  test("the STANDING WATCH's quiet adoption paints nothing", async () => {
    const { controller } = makeController(thrower);
    await controller.resumeRun("r1", { quiet: true });
    // A dropped socket, a server restart, a sleep/wake — any of which make one
    // `poll` reject — used to paint a trouble card over a healthy transcript
    // for a reader who touched nothing. T only warns here (T:17862-17865).
    expect(controller.getState().trouble).toBe(null);
  });

  test("the SCHEDULE POLLER's `neverShown` adoption paints nothing", async () => {
    const { controller } = makeController(thrower);
    await controller.resumeRun("r1", { neverShown: true });
    expect(controller.getState().trouble).toBe(null);
  });

  test("...but BOOT's `?run=` still gets its answer", async () => {
    // The reader put that id on the URL and is owed an explanation — the same
    // predicate the `unknown run_id` branch uses, on the adjacent road.
    const { controller } = makeController(thrower);
    await controller.resumeRun("r1");
    expect(controller.getState().trouble?.message).toContain("server went away");
  });
});

// ---- the scroll nonce tracks ROWS, not roads (Bugbot 3974939169 / 3974975062)

describe("`repaired` is bumped when a repair actually appended something", () => {
  test("A NO-OP REPAIR DOES NOT FORCE-SCROLL (3974975062)", async () => {
    // The 5 s `refreshHistory` has already drawn the turn; the watch then
    // attaches the same id. Nothing is appended — and the renderer treats the
    // nonce as an unconditional scroll-to-bottom, so a reader who had scrolled
    // up was yanked to the bottom for no new content.
    const { controller } = makeController({
      history: () => ({
        turns: [{ role: "user", text: "already here", uuid: "u1" }],
        transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
      }),
      poll: () => poll({ done: true, message: "already here", text: "reply" }),
      live_run: () => ({ run_id: "" }),
    });
    await controller.openSession("s1");
    const before = controller.getState().repaired;
    await controller.resumeRun("r1", { neverShown: true });
    expect(users(controller).map((t) => t.text)).toEqual(["already here"]);
    expect(controller.getState().repaired).toBe(before);
  });

  test("A FAILED REPAIR DOES SCROLL (3974939169)", async () => {
    // The done-error branch appends a user line and an error in one commit and
    // then returned before bumping the nonce, so a reader who had scrolled up
    // never saw a failed scheduled or adopted turn land at all.
    const { controller } = makeController({
      history: () => ({
        turns: [{ role: "user", text: "already here", uuid: "u1" }],
        transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
      }),
      poll: () => poll({ done: true, error: "the CLI died", message: "already here" }),
      live_run: () => ({ run_id: "" }),
    });
    await controller.openSession("s1");
    const before = controller.getState().repaired;
    await controller.resumeRun("r1", { neverShown: true });
    expect(controller.getState().repaired).toBe(before + 1);
  });

  test("a failed repair that appended NOTHING does not scroll either", async () => {
    // The transcript already carries the failure row, so `errorShown` refuses
    // it and there is nothing new to scroll to.
    const { controller } = makeController({
      history: () => ({
        turns: [
          { role: "user", text: "already here", uuid: "u1" },
          { role: "error", text: "the CLI died", uuid: "u2" },
        ],
        transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
      }),
      poll: () => poll({ done: true, error: "the CLI died", message: "already here" }),
      live_run: () => ({ run_id: "" }),
    });
    await controller.openSession("s1");
    const before = controller.getState().repaired;
    await controller.resumeRun("r1", { neverShown: true });
    expect(controller.getState().repaired).toBe(before);
  });
});

// ---- the held follow-ups a RELOAD reads ------------------------------------
//
// A line typed into a running chat is in the CLI's own stdin queue and in no
// JSONL row — nothing has consumed it — so a page that comes BACK has only one
// place to learn about it: the `history` answer, which it makes before it has a
// run to poll. `landHistory` publishes that list whenever the answer names a
// live run at all (`live_run` is a string), and `live_run: ""` publishes the
// empty one, because nothing is held when nothing is running.
describe("the inbox a restored conversation draws", () => {
  test("lands from `history` when the answer names a live run", async () => {
    const { controller } = makeController({
      history: () => ({
        turns: [{ role: "user", text: "count the rows", uuid: "u1" }],
        transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
        live_run: "r1",
        permissions: [],
        inbox: [
          { id: "f1", text: "go on" },
          // A DRAINED entry rides the same list and draws the same bubble.
          { id: "f2", text: "and again", drained: true },
        ],
      }),
      live_run: () => ({ run_id: "" }),
    });
    await controller.openSession("s1");
    expect(controller.getState().inbox?.map((m) => m.id)).toEqual(["f1", "f2"]);
    expect(controller.getState().inbox?.map((m) => m.text)).toEqual(["go on", "and again"]);
  });

  test("publishes the EMPTY list when the answer says nothing is running", async () => {
    // `live_run: ""` is "I looked, and nothing is running here" — so the answer
    // carries no held follow-ups and the list it publishes is empty. (The field
    // being a STRING is the test; its value is the server's news.)
    const { controller } = makeController({
      history: () => ({
        turns: [{ role: "user", text: "count the rows", uuid: "u1" }],
        transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
        live_run: "",
        permissions: [],
      }),
      live_run: () => ({ run_id: "" }),
    });
    await controller.openSession("s1");
    expect(controller.getState().inbox ?? []).toEqual([]);
  });

  test("leaves the list ALONE on an answer with no `live_run` at all (older server)", async () => {
    const { controller } = makeController({
      history: () => ({
        turns: [{ role: "user", text: "count the rows", uuid: "u1" }],
        transcript: { path: "/p/s1.jsonl", mtime: 1, size: 2 },
      }),
      live_run: () => ({ run_id: "" }),
    });
    await controller.openSession("s1");
    expect(controller.getState().inbox ?? []).toEqual([]);
  });
});
