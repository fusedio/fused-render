// The run loop against a FAKE agent.py. Every test here is one of the rules the
// template earned the hard way: whole-turn replay, D687 slicing after a
// follow-up, ownership by the newest loop, card placement, the two poll
// refusals, and a thrown poll that keeps `?run=`.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";

const { PERM_CARD_MAX, createChatController, runEnding, stopAllowed, trimPermCards } =
  await import("./run-controller");
const { createMemoryParamsStore } = await import("../params/store");

import type { runAgent } from "./agent";
import type { AssistantTurn, ChatController, NoteTurn, UserTurn } from "./controller-api";
import type { PermissionRow, PollResponse, Segment } from "./types";

// ---- fake agent.py ---------------------------------------------------------

type Handler = (fields: Record<string, unknown>, call: number) => unknown | Promise<unknown>;

interface Recorded {
  action: string;
  fields: Record<string, unknown>;
}

function fakeAgent(handlers: Record<string, Handler>) {
  const calls: Recorded[] = [];
  const counts: Record<string, number> = {};
  const run = ((_dir: string, action: string, fields: Record<string, unknown>) => {
    calls.push({ action, fields });
    const n = (counts[action] = (counts[action] || 0) + 1) - 1;
    const h = handlers[action];
    if (!h) throw new Error("fake agent has no handler for " + action);
    return Promise.resolve(h(fields, n));
  }) as unknown as typeof runAgent;
  return { run, calls, of: (action: string) => calls.filter((c) => c.action === action) };
}

/** A steady-state poll body with only the fields a test cares about set. */
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
    activity: {
      tool: null,
      tools_open: 0,
      tool_input_bytes: 0,
      thinking_tokens: 0,
      hook: "",
      tasks: [],
      agent_rows: 0,
    },
    segments: [],
    ...over,
  };
}

const text = (t: string): Segment & { text: string } => ({ kind: "text", text: t });
/** agent.py's narrow early-exit body for a run id it has never heard of. */
const unknownRunPoll = () => ({
  text: "",
  done: true,
  session_id: "",
  error: "unknown run_id",
  permissions: [],
  app_state: [],
  skills: [],
  retry: null,
  retry_total: 0,
  retry_status: 0,
  segments: [],
});
/** The text of a segment the test built, for asserting on a rendered span. */
const bodyOf = (sg: Segment): string => (sg as { text?: string }).text || "";

function makeController(
  handlers: Record<string, Handler>,
  params = createMemoryParamsStore(),
  over: { appStateBlock?: () => Promise<string> } = {},
) {
  const agent = fakeAgent(handlers);
  const activity: number[] = [];
  const stranded: string[][] = [];
  const controller = createChatController({
    ...over,
    file: "/proj/app.py",
    agentDir: "/tpl/claude",
    params,
    run: agent.run,
    sleep: () => Promise.resolve(),
    now: () => 1_000,
    model: () => "sonnet",
    effort: () => "high",
    hasPane: () => true,
    onActivity: () => activity.push(1),
    onStranded: (t) => stranded.push(t),
  });
  return { controller, agent, params, activity, stranded };
}

const assistants = (c: ChatController) =>
  c.getState().turns.filter((t): t is AssistantTurn => t.role === "assistant");
const users = (c: ChatController) => c.getState().turns.filter((t): t is UserTurn => t.role === "user");
const notes = (c: ChatController) => c.getState().turns.filter((t): t is NoteTurn => t.role === "note");

// ---- the pure rules --------------------------------------------------------

describe("runEnding (T:15871)", () => {
  test("a clean end keeps its text", () => {
    expect(runEnding({ error: "" }, false)).toEqual({ error: "", note: "", keepText: true });
  });
  test("an unasked-for error drops the partial reply and is reported", () => {
    expect(runEnding({ error: "claude exited" }, false)).toEqual({
      error: "claude exited",
      note: "",
      keepText: false,
    });
  });
  test("a stop swallows the kill's error and keeps the work — here and only here", () => {
    expect(runEnding({ error: "claude exited unexpectedly" }, true)).toEqual({
      error: "",
      note: "Stopped.",
      keepText: true,
    });
  });
  test("a stop this page did not press is still a stop (`cancelled`, Akshil 2026-08-21)", () => {
    expect(runEnding({ error: "claude exited", cancelled: true }, false).note).toBe("Stopped.");
  });
  test("the stop that did not land says so", () => {
    expect(runEnding({ error: "" }, true).note).toBe("The turn finished before the stop landed.");
  });
});

describe("stopAllowed (T:15870)", () => {
  test("needs a run, a seat, and no stop already asked for on THIS seat", () => {
    expect(stopAllowed("r1", 3, 0)).toBe(true);
    expect(stopAllowed("r1", 3, 3)).toBe(false);
    expect(stopAllowed(null, 3, 0)).toBe(false);
    expect(stopAllowed("r1", 0, 0)).toBe(false);
    // A session host makes one run_id span a whole chat: seat 4 may be stopped
    // even though seat 3 already was.
    expect(stopAllowed("r1", 4, 3)).toBe(true);
  });
});

// ---- start → poll → done ---------------------------------------------------

describe("start → poll → done", () => {
  test("a fresh chat skips the live-host probe and streams to a finished turn", async () => {
    const { controller, agent, params, activity } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) =>
        n === 0
          ? poll({ segments: [text("hel")], text: "hel" })
          : n === 1
            ? poll({ segments: [text("hello there")], text: "hello there" })
            : poll({ done: true, segments: [text("hello there")], text: "hello there" }),
    });
    await controller.sendMessage("hi");

    // No session_id yet ⇒ no `live_host` lookup that could only answer "".
    expect(agent.of("live_host").length).toBe(0);
    const started = agent.of("start")[0].fields;
    expect(started).toMatchObject({
      file: "/proj/app.py",
      message: "hi",
      session_id: "",
      model: "sonnet",
      effort: "high",
      permission_mode: "prompt",
      has_pane: "1",
      read_dirs: "[]",
    });
    // The poll rides `file` so the agent can refuse another target's run.
    expect(agent.of("poll")[0].fields).toEqual({ run_id: "r1", file: "/proj/app.py" });

    const s = controller.getState();
    expect(users(controller).map((t) => t.text)).toEqual(["hi"]);
    expect(assistants(controller).length).toBe(1);
    expect(assistants(controller)[0].text).toBe("hello there");
    expect(assistants(controller)[0].streaming).toBe(false);
    expect(s.status).toBe("idle");
    expect(s.working).toBeNull();
    expect(s.trouble).toBeNull();
    // `session_id` from the poll, `run` cleared at the end (T:16245, 16333).
    expect(params.get("session_id")).toBe("s1");
    expect(params.get("run")).toBeUndefined();
    // One stamp at the loop's start, one at its end (T:16238, 16425).
    expect(activity.length).toBe(2);
  });

  test("a session already live absorbs the message instead of spawning a second run", async () => {
    const params = createMemoryParamsStore({ session_id: "s1" });
    const { controller, agent } = makeController(
      {
        live_host: () => ({ run_id: "live-1" }),
        send: () => ({ sent: true as const }),
        poll: () => poll({ done: true, text: "ok", segments: [text("ok")] }),
        start: () => {
          throw new Error("start must not be called when the host took it");
        },
      },
      params,
    );
    await controller.sendMessage("again");
    expect(agent.of("send")[0].fields).toMatchObject({ run_id: "live-1", message: "again" });
    expect(agent.of("start").length).toBe(0);
    expect(agent.of("poll")[0].fields).toMatchObject({ run_id: "live-1" });
  });

  test("a dead host, a refusal or a respawn all fall through to `start`", async () => {
    for (const answer of [{ error: "no host" }, { respawn: true as const }, null]) {
      const params = createMemoryParamsStore({ session_id: "s1" });
      const { controller, agent } = makeController(
        {
          live_host: () => ({ run_id: "live-1" }),
          send: () => (answer === null ? Promise.reject(new Error("network")) : answer),
          start: () => ({ run_id: "r2" }),
          poll: () => poll({ done: true }),
        },
        params,
      );
      await controller.sendMessage("again");
      expect(agent.of("start").length).toBe(1);
      expect(agent.of("poll")[0].fields).toMatchObject({ run_id: "r2" });
    }
  });

  test("`start` refusing rolls the bubble back and reports the failure", async () => {
    const { controller, params } = makeController({
      start: () => ({ error: "(empty message)" }),
      poll: () => poll({ done: true }),
    });
    await controller.sendMessage("hi");
    // The USER's bubble is rolled back — the agent never saw the message — and
    // what is left is the failure's own row (T:13698 `addError`).
    expect(controller.getState().turns.map((t) => t.role)).toEqual(["error"]);
    expect(controller.getState().trouble?.message).toBe("(empty message)");
    expect(params.get("run")).toBeUndefined();
  });

  test("one turn at a time: a second send while the first is in flight is dropped", async () => {
    const { controller, agent } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) => (n === 0 ? poll() : poll({ done: true })),
    });
    const first = controller.sendMessage("one");
    await controller.sendMessage("two");
    await first;
    expect(agent.of("start").length).toBe(1);
    expect(users(controller).map((t) => t.text)).toEqual(["one"]);
  });

  test("a wordless send with blocks still goes, and the bubble shows the marker", async () => {
    const { controller, agent } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: () => poll({ done: true }),
    });
    const block = `<pane-shot>\ncaption\n[{"kind":"pane","view":"/p.png"}]\n</pane-shot>`;
    await controller.sendMessage("", { blocks: [block], readDirs: ["/tmp/shots"] });
    expect(users(controller)[0].text).toBe("🖼 pane screenshot");
    expect(users(controller)[0].raw).toBe(block);
    expect(agent.of("start")[0].fields.read_dirs).toBe('["/tmp/shots"]');
  });

  test("nothing to send at all is a no-op", async () => {
    const { controller, agent } = makeController({ start: () => ({ run_id: "r1" }) });
    await controller.sendMessage("");
    expect(agent.calls.length).toBe(0);
  });

  test("a run whose text arrives only on the poll that ENDS it still renders", async () => {
    const { controller } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: () => poll({ done: true, text: "all at once", segments: [text("all at once")] }),
    });
    await controller.sendMessage("hi");
    expect(assistants(controller).map((t) => t.text)).toEqual(["all at once"]);
  });

  test("the legacy flat-text path survives a `done` poll that carries nothing", async () => {
    const { controller } = makeController({
      start: () => ({ run_id: "r1" }),
      // No segments at all: the pre-segment path. The final poll's body is empty.
      poll: (_f, n) => (n === 0 ? poll({ text: "streamed" }) : poll({ done: true })),
    });
    await controller.sendMessage("hi");
    expect(assistants(controller).map((t) => t.text)).toEqual(["streamed"]);
  });
});

// ---- follow-ups and D687 slicing ------------------------------------------

describe("follow-ups (T:16024, D687)", () => {
  // QA round 3a, defect 5: the hint existed, was styled and was tested, and no
  // user ever saw it — the entry used to be dropped the instant `send` came back
  // `{sent: true}`, which is the inbox taking the bytes, not the model reading
  // them. Its lifetime is the TURN.
  test("the queued hint survives the send's ack and lives until the run ends", async () => {
    let controller!: ChatController;
    const seen: string[][] = [];
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      poll: async (_f, n) => {
        if (n === 0) {
          await controller.sendFollowUp("and then say goodnight");
          // The send has ALREADY come back `{sent: true}` at this point.
          expect(made.agent.of("send").length).toBe(1);
          seen.push(controller.getState().queued);
          return poll({ segments: [text("still working")] });
        }
        if (n === 1) {
          // …and it is still queued several polls later, because the CLI holds
          // it until the turn in flight finishes.
          seen.push(controller.getState().queued);
          return poll({ segments: [text("still working")] });
        }
        return poll({ done: true, segments: [text("still working")] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(seen).toEqual([["and then say goodnight"], ["and then say goodnight"]]);
    // The run is over: the CLI drained its queue as part of it.
    expect(controller.getState().queued).toEqual([]);
  });

  test("two follow-ups are both counted, and identical text does not collapse", async () => {
    let controller!: ChatController;
    let mid: string[] = [];
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      poll: async (_f, n) => {
        if (n === 0) {
          await controller.sendFollowUp("again");
          await controller.sendFollowUp("again");
          mid = controller.getState().queued;
          return poll({ segments: [text("working")] });
        }
        return poll({ done: true, segments: [text("working")] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(mid).toEqual(["again", "again"]);
  });

  // QA round 3a, defect 1. Native's `start` body is pinned FIELD FOR FIELD
  // against T:16609-16640, because the permission mode reaching agent.py at all
  // is what the QA could not tell apart from the CLI auto-approving: the pill
  // read `prompt`, the URL read `permission=prompt`, and Bash still ran
  // unprompted. It runs unprompted because `PERMISSION_MODES["prompt"] is None`
  // (agent.py:266) — so agent.py passes NO `--permission-mode` and the CLI falls
  // back to the machine's own `~/.claude/settings.json`, which on the QA box is
  // `permissions.defaultMode: "auto"`. Identical on T. This test is the guard
  // that the payload is not what changes underneath that conclusion.
  test("the `start` body carries T's exact field set, permission_mode included", async () => {
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      poll: () => poll({ done: true, segments: [text("hi")] }),
    });
    await made.controller.sendMessage("go");
    const started = made.agent.of("start")[0]!;
    expect(Object.keys(started.fields).sort()).toEqual([
      "effort",
      "file",
      "has_pane",
      "message",
      "model",
      "permission_mode",
      "read_dirs",
      "session_id",
    ]);
    // The field name and the value T sends — NOT `permission`, and not the
    // CLI's own spelling of the flag.
    expect(started.fields.permission_mode).toBe("prompt");
  });

  test("the picker's mode rides every follow-up `send` too (T:16128)", async () => {
    let controller!: ChatController;
    const params = createMemoryParamsStore();
    params.set({ permission: "acceptEdits" });
    const made = makeController(
      {
        start: () => ({ run_id: "r1" }),
        send: () => ({ sent: true as const }),
        poll: async (_f, n) => {
          if (n === 0) {
            await controller.sendFollowUp("more");
            return poll({ segments: [text("working")] });
          }
          return poll({ done: true, segments: [text("working")] });
        },
      },
      params,
    );
    controller = made.controller;
    await controller.sendMessage("go");
    expect(made.agent.of("start")[0]!.fields.permission_mode).toBe("acceptEdits");
    expect(made.agent.of("send")[0]!.fields.permission_mode).toBe("acceptEdits");
  });

  // ── the mid-stream follow-up, as agent.py actually replays it (feedback #9) ──
  //
  // The real sequence, and every step of it is a rule somewhere in agent.py:
  //
  //   1. reply A streams.
  //   2. `send` writes `pending_echo` (the byte offset of `out.jsonl`) and puts
  //      the message in the inbox. `followupSeq` bumps here.
  //   3. FOR AS LONG AS THAT FILE EXISTS `_poll` refuses to call the turn
  //      `done` — it cannot trust a trailing `result` yet. It used to blank
  //      `text`/`segments` for the whole window too, so A's remainder streamed
  //      INVISIBLY and then arrived with B in one burst (R2-1). It now blanks
  //      only when the send was made between turns, where the window's rows are
  //      a reply the page has already settled; a send made MID-REPLY keeps
  //      streaming the reply in flight (the test below this one).
  //   4. The CLI drains the inbox and echoes the follow-up. That poll's window
  //      still opens at A's own start, so it carries A COMPLETE, plus the seam:
  //      `turn_breaks: [{segments, text}]`.
  //   5. Once a `result` closed A, `_read_current_turn` advances the cursor past
  //      the echo, so from the next poll on the payload is reply B ALONE with no
  //      seam in it.
  //
  // The bug: the loop froze the bases at step 2 and guessed the seam there, so
  // A's remainder rendered under the follow-up's bubble and B sliced to nothing
  // ("the reply is cut off and it looks stuck on the queued message"). The seam
  // now comes from step 4's payload, and step 5 is absorbed by the shrink test.
  test("both replies land in their own bubbles, in the order a reload shows", async () => {
    let controller!: ChatController;
    const A1 = text("Reply A, first half. ");
    const A2 = text("Reply A, second half.");
    const B1 = text("Reply B.");
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      poll: async (_f, n) => {
        // 1 — A streams.
        if (n === 0) return poll({ segments: [A1], text: A1.text });
        // 2 — the follow-up lands.
        if (n === 1) {
          await controller.sendFollowUp("and also this");
          // 3 — `pending_echo` blanks the payload. A's bubble must KEEP what it
          // already has rather than be wiped by an empty poll.
          return poll({ segments: [], text: "" });
        }
        if (n === 2) return poll({ segments: [], text: "" });
        // 4 — the echo landed: A complete, and the seam that says so.
        if (n === 3) {
          return poll({
            segments: [A1, A2],
            text: A1.text + A2.text,
            turn_breaks: [{ segments: 2, text: A1.text.length + A2.text.length }],
          });
        }
        // 5 — the cursor stepped over the seam; the payload is B alone.
        if (n === 4) return poll({ segments: [B1], text: B1.text });
        return poll({ done: true, segments: [B1], text: B1.text });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");

    expect(controller.getState().turns.map((t) => t.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    const reply = assistants(controller);
    // A is WHOLE — its remainder went into its own bubble, not the follow-up's.
    expect(reply[0]!.text).toBe(A2.text);
    expect((reply[0]!.segments || []).map((sg) => bodyOf(sg))).toEqual([
      A1.text,
      A2.text,
    ]);
    // B is B, and nothing of A leaked into it.
    expect((reply[1]!.segments || []).map((sg) => bodyOf(sg))).toEqual([
      B1.text,
    ]);
    expect(reply[1]!.text).toBe(B1.text);
    // Both settled — the older bubble must not be left with a caret on it.
    expect(reply.map((t) => !!t.streaming)).toEqual([false, false]);
    expect(controller.getState().queued).toEqual([]);
  });

  // R2-1: the same sequence with agent.py streaming THROUGH the window. Step 3
  // is no longer a hole — A's remainder arrives poll by poll, into A's own
  // bubble, and only the seam at step 4 opens B's. What this pins is that the
  // continued payload extends slot 0 instead of being read as a new reply.
  test("A's remainder streams into A's own bubble during the follow-up window", async () => {
    let controller!: ChatController;
    const A1 = text("Reply A, ");
    const A2 = text("Reply A, still going. ");
    const A3 = text("Reply A, still going. Done.");
    const B1 = text("Reply B.");
    /** The assistant bubbles as of each poll, to prove ONE grew. */
    const seen: string[][] = [];
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ segments: [A1], text: A1.text });
        if (n === 1) {
          await controller.sendFollowUp("and also this");
          // Still inside the window, and the payload is A GROWING.
          return poll({ segments: [A2], text: A2.text });
        }
        if (n === 2) {
          seen.push(assistants(controller).map((t) => bodyOf((t.segments || [])[0]!)));
          return poll({ segments: [A3], text: A3.text });
        }
        if (n === 3) {
          seen.push(assistants(controller).map((t) => bodyOf((t.segments || [])[0]!)));
          // The echo landed: A closed, so the seam appears and B starts.
          return poll({
            segments: [A3, B1],
            text: A3.text + B1.text,
            turn_breaks: [{ segments: 1, text: A3.text.length }],
          });
        }
        return poll({
          done: true,
          segments: [A3, B1],
          text: A3.text + B1.text,
          turn_breaks: [{ segments: 1, text: A3.text.length }],
        });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");

    // ONE bubble grew across the window — never a second one for the same reply.
    expect(seen).toEqual([[A2.text], [A3.text]]);
    expect(controller.getState().turns.map((t) => t.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    const reply = assistants(controller);
    expect((reply[0]!.segments || []).map(bodyOf)).toEqual([A3.text]);
    expect((reply[1]!.segments || []).map(bodyOf)).toEqual([B1.text]);
    expect(reply.map((t) => !!t.streaming)).toEqual([false, false]);
  });

  test("the seam splits the FLAT text too, for a turn with no segments at all", async () => {
    // The legacy flat path a pre-segments agent.py still replays: the seam's
    // `text` offset is a string index, and the same split has to happen there.
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ text: "first half" });
        if (n === 1) {
          await controller.sendFollowUp("and also this");
          return poll({ text: "" });
        }
        if (n === 2) {
          return poll({
            text: "first halfsecond half",
            turn_breaks: [{ segments: 0, text: "first halfsecond half".length }],
          });
        }
        if (n === 3) return poll({ text: "answer to the follow-up" });
        return poll({ done: true, text: "answer to the follow-up" });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    const reply = assistants(controller);
    expect(reply.map((t) => t.text)).toEqual([
      "first halfsecond half",
      "answer to the follow-up",
    ]);
    expect(reply[1]!.segments).toBeUndefined();
  });

  test("a payload with a seam IN it renders both spans at once", async () => {
    // The other shape of step 4/5: the CLI absorbed the follow-up with no
    // `result` between, so the cursor never advances and ONE payload carries
    // both replies for the rest of the run.
    let controller!: ChatController;
    const A = text("A. ");
    const B = text("B.");
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ segments: [A], text: A.text });
        if (n === 1) {
          await controller.sendFollowUp("also");
          return poll({ segments: [], text: "" });
        }
        const both = {
          segments: [A, B],
          text: A.text + B.text,
          turn_breaks: [{ segments: 1, text: A.text.length }],
        };
        return poll(n === 2 ? both : { ...both, done: true });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(controller.getState().turns.map((t) => t.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    const reply = assistants(controller);
    expect(reply[0]!.text).toBe(A.text);
    expect(reply[1]!.text).toBe(B.text);
    expect(reply[1]!.text).not.toContain("A.");
  });

  // Bugbot PR #1061 (MED). TWO follow-ups absorbed into ONE run. The seam test
  // used to be a boolean, so the FIRST cursor step disarmed it — and the second
  // step commonly reports no seam at all (the newer reply alone in the window,
  // the echo and its `result` both landed between two polls). With the test
  // disarmed that payload read as an ordinary one and went into the PREVIOUS
  // slot, overwriting the reply before it: the middle bubble simply vanished.
  test("a second follow-up in the same run opens its OWN bubble, not the last one's", async () => {
    let controller!: ChatController;
    const A1 = text("Reply A. ");
    const B1 = text("Reply B, first half. ");
    const B2 = text("Reply B, second half.");
    const C1 = text("Reply C.");
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      poll: async (_f, n) => {
        // A streams.
        if (n === 0) return poll({ segments: [A1], text: A1.text });
        // Follow-up 1 lands; `pending_echo` blanks the window.
        if (n === 1) {
          await controller.sendFollowUp("and also this");
          return poll({ segments: [], text: "" });
        }
        // Its echo landed: A complete, plus the seam that opens B.
        if (n === 2) {
          return poll({
            segments: [A1, B1],
            text: A1.text + B1.text,
            turn_breaks: [{ segments: 1, text: A1.text.length }],
          });
        }
        // Follow-up 2 lands while the FIRST seam is still in the window — so
        // two are outstanding at once, which is the case a flag cannot hold.
        if (n === 3) {
          await controller.sendFollowUp("and one more");
          return poll({ segments: [], text: "" });
        }
        // Step one: the cursor carried A's seam out, so the window is B alone,
        // still growing.
        if (n === 4) return poll({ segments: [B1, B2], text: B1.text + B2.text });
        // Step two, and agent.py reports NO seam for it: C alone. The only
        // signal is the shrink, and it counts only while a seam is still owed.
        if (n === 5) return poll({ segments: [C1], text: C1.text });
        return poll({ done: true, segments: [C1], text: C1.text });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");

    expect(controller.getState().turns.map((t) => t.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    const reply = assistants(controller);
    expect(reply.map((t) => (t.segments || []).map(bodyOf))).toEqual([
      [A1.text],
      [B1.text, B2.text],
      [C1.text],
    ]);
    // B survived: the last reply did not land on top of it.
    expect(reply[2]!.text).toBe(C1.text);
    expect(reply.map((t) => !!t.streaming)).toEqual([false, false, false]);
  });

  // ── owner feedback R4-3 ──────────────────────────────────────────────────
  //
  // "Sometimes when I reply, it re-streams the previous message's response, and
  // then shows the new response."
  //
  // THE IDLE-TIME SEND. `_read_current_turn`'s cursor only advances past a turn
  // boundary it has PROVEN, and it never trims what the poll it advances on
  // hands back — so the first non-blank payload after a send into an idle host
  // is the previous reply IN FULL with the new one growing behind it. And
  // `turn_breaks` is empty for that window: a genuine boundary is not an
  // absorbed fold-in, so `_absorbed_turn_breaks` names no seam.
  //
  // A send into a live host starts a FRESH `pollLoop` (`sendMessage`'s live-host
  // road), which has no memory of that reply having landed — so it opened a new
  // bubble, typed reply A into it a second time, and replaced it with reply B
  // when the cursor finally moved. The payload sequence below is that shape.
  test("a follow-up never re-types the turn already on screen (R4-3)", async () => {
    const A = text("Reply A, all of it.");
    const B1 = text("Reply B, first half. ");
    const B2 = text("Reply B, second half.");
    const params = createMemoryParamsStore();
    const made = makeController(
      {
        start: () => ({ run_id: "r1" }),
        live_host: () => ({ run_id: "r1" }),
        send: () => ({ sent: true as const }),
        poll: (_f, n) => {
          // Turn one: A streams, then ends. The window is [echo q1, A…, result]
          // and the cursor stays at q1 — there is no newer turn to prove yet.
          if (n === 0) return poll({ segments: [A], text: A.text });
          if (n === 1) return poll({ done: true, segments: [A], text: A.text });
          // The send lands while the run is idle, so `pending_echo` blanks the
          // payload for as long as the echo is outstanding.
          if (n === 2) return poll({ segments: [], text: "" });
          // The echo landed. `_read_current_turn` will advance PAST it — but
          // not until the next call: this payload is still the old window, so
          // it carries reply A in full with reply B behind it, and no seam.
          if (n === 3) return poll({ segments: [A, B1], text: A.text + B1.text });
          // The cursor has moved. B alone, still growing.
          if (n === 4) return poll({ segments: [B1, B2], text: B1.text + B2.text });
          return poll({ done: true, segments: [B1, B2], text: B1.text + B2.text });
        },
      },
      params,
    );
    const { controller } = made;
    await controller.sendMessage("go");
    expect(assistants(controller).map((t) => t.text)).toEqual([A.text]);

    // EVERY intermediate state is inspected, because the defect is transient:
    // the bubble showed A, then was replaced by B. A final-state assertion
    // cannot see it at all.
    const seen: string[][] = [];
    const off = controller.subscribe(() => {
      seen.push(assistants(controller).map((t) => (t.segments || []).map(bodyOf).join("")));
    });
    await controller.sendMessage("now say done");
    off();

    // Two replies, in order, and the second is only ever reply B.
    const reply = assistants(controller);
    expect(reply.map((t) => (t.segments || []).map(bodyOf))).toEqual([
      [A.text],
      [B1.text, B2.text],
    ]);
    // …and it never once carried reply A, at any tick.
    const restreamed = seen.filter((frame) => frame.slice(1).some((b) => b.includes(A.text)));
    expect(restreamed).toEqual([]);
    // The first bubble is left exactly as turn one settled it — not rebuilt,
    // not re-typed, not duplicated into a third one.
    expect(reply.length).toBe(2);
    expect(reply.every((t) => !t.streaming)).toBe(true);
  });

  test("an agent.py with no `turn_breaks` keeps one payload as one reply", async () => {
    // The compatibility floor: no seam reported, so nothing is split. One
    // bubble that grows, which is the pre-feedback-#9 rendering minus the
    // guess — never a bubble sliced against an offset nobody sent.
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ segments: [text("one")], text: "one" });
        if (n === 1) {
          await controller.sendFollowUp("also");
          return poll({ segments: [text("one two")], text: "one two" });
        }
        return poll({ done: true, segments: [text("one two")], text: "one two" });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(assistants(controller).map((t) => t.text)).toEqual(["one two"]);
  });

  // ── feedback #10: the follow-up path is `send`, and only `send` ────────────
  test("a follow-up goes out as ONE `send` on the live run — never a cancel or a restart", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      poll: async (_f, n) => {
        if (n === 0) {
          await controller.sendFollowUp("read this mid-reply");
          return poll({ segments: [text("thinking")] });
        }
        return poll({ done: true, segments: [text("thinking")] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    // The whole request sequence, pinned: the opening `start`, the follow-up's
    // `send`, and polls. Anything that cancelled or restarted the run would
    // break Surya's mid-response drain — the CLI reads the queued message
    // inside the turn it is already running, which a respawn throws away.
    expect(made.agent.calls.filter((c) => c.action !== "poll").map((c) => c.action)).toEqual([
      "start",
      "send",
    ]);
    expect(made.agent.of("cancel")).toEqual([]);
    const sent = made.agent.of("send")[0]!.fields;
    // The same field set legacy posts (T:16138-16150).
    expect(Object.keys(sent).sort()).toEqual([
      "effort",
      "message",
      "model",
      "permission_mode",
      "read_dirs",
      "run_id",
    ]);
    expect(sent.run_id).toBe("r1");
    expect(sent.message).toBe("read this mid-reply");
  });

  test("a follow-up the inbox refused is rolled back — bubble, queue and all", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ error: "no such run" }),
      poll: async (_f, n) => {
        if (n === 0) {
          await controller.sendFollowUp("late");
          return poll({ segments: [text("still going")] });
        }
        return poll({ done: true, segments: [text("still going")] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(users(controller).map((t) => t.text)).toEqual(["go"]);
    expect(controller.getState().queued).toEqual([]);
    expect(controller.getState().trouble?.message).toBe(
      "Could not send: the session ended before this reached it.",
    );
    // The counter was NOT bumped, so the reply kept growing in one bubble
    // (Bugbot, PR #996).
    expect(assistants(controller).length).toBe(1);
  });

  test("`respawn` starts a fresh run and re-points the `run` param", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: (_f, n) => ({ run_id: n === 0 ? "r1" : "r2" }),
      send: () => ({ respawn: true as const }),
      poll: async (f, n) => {
        if (f.run_id === "r1" && n === 0) {
          await controller.sendFollowUp("with a new attachment dir");
          return poll({ done: true, segments: [text("first")] });
        }
        return poll({ done: true, segments: [text("second")] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(made.agent.of("start").length).toBe(2);
    expect(made.agent.of("start")[1].fields.message).toBe("with a new attachment dir");
  });

  test("a follow-up with no run to attach to reports rather than vanishing", async () => {
    const { controller } = makeController({ send: () => ({ sent: true as const }) });
    await controller.sendFollowUp("nowhere to go");
    expect(controller.getState().trouble?.message).toBe(
      "Could not send: no run to attach this message to.",
    );
    // The failure's own row, and NOTHING else: the follow-up never got a
    // bubble, because there was no run for it to join (T:13698 `addError`
    // appends the row; the user's text is handed back, not logged).
    expect(controller.getState().turns.map((t) => t.role)).toEqual(["error"]);
  });
});

// ---- ownership -------------------------------------------------------------

// ── feedback #30: the app-state PUSH channel ────────────────────────────────
describe("app state rides out with every message (T:16483)", () => {
  const BLOCK = "<live-app-state>\npane: sine.html\n</live-app-state>";

  test("the block is composed into the wire and the bubble carries the receipt", async () => {
    let controller!: ChatController;
    const made = makeController(
      {
        start: () => ({ run_id: "r1" }),
        send: () => ({ sent: true as const }),
        poll: async (_f, n) => {
          if (n === 0) {
            await controller.sendFollowUp("and this");
            return poll({ segments: [text("ok")] });
          }
          return poll({ done: true, segments: [text("ok")] });
        },
      },
      createMemoryParamsStore(),
      { appStateBlock: () => Promise.resolve(BLOCK) },
    );
    controller = made.controller;
    await controller.sendMessage("look at my app");

    // OUT ON THE WIRE, on the opening turn AND on the follow-up.
    expect(String(made.agent.of("start")[0]!.fields.message)).toContain(BLOCK);
    expect(String(made.agent.of("send")[0]!.fields.message)).toContain(BLOCK);
    const posted = users(controller);
    // The bubble still shows only what the user typed…
    expect(posted.map((t) => t.text)).toEqual(["look at my app", "and this"]);
    // …with the whole wire on `raw` for "what was sent"…
    expect(posted[0]!.raw).toContain(BLOCK);
    // …and the receipt legacy draws under it.
    expect(posted.map((t) => t.appState)).toEqual([true, true]);
  });

  test("nothing to say ⇒ no block, no receipt, and the message is unchanged", async () => {
    // A chat with no pane, or a pane the watcher has learned nothing from:
    // `blockForSend` answers "" and this path must add neither markers nor a
    // caption claiming context the agent never got.
    const made = makeController(
      {
        start: () => ({ run_id: "r1" }),
        poll: () => poll({ done: true, segments: [text("ok")] }),
      },
      createMemoryParamsStore(),
      { appStateBlock: () => Promise.resolve("") },
    );
    await made.controller.sendMessage("plain");
    expect(made.agent.of("start")[0]!.fields.message).toBe("plain");
    expect(users(made.controller)[0]!.appState).toBeUndefined();
  });

  test("a failed snapshot never costs the message", async () => {
    const made = makeController(
      {
        start: () => ({ run_id: "r1" }),
        poll: () => poll({ done: true, segments: [text("ok")] }),
      },
      createMemoryParamsStore(),
      { appStateBlock: () => Promise.reject(new Error("offload failed")) },
    );
    await made.controller.sendMessage("still goes");
    expect(made.agent.of("start")[0]!.fields.message).toBe("still goes");
    expect(made.controller.getState().trouble).toBeNull();
  });
});

// ── feedback #25: cards come from the SESSION, not only from `run=` ─────────
describe("live-run adoption on openSession (T:17506)", () => {
  const permRowFor = (id: string): PermissionRow => ({
    id,
    tool: "Bash",
    input: { command: "ls" },
    created_at: 0,
    decision: "",
    scope: "",
    mode: "",
    answers: {},
  });

  test("a session opened with no `run` adopts its live turn, and its card arrives", async () => {
    // The reported shape: a folder → chat with only `session_id`, a run
    // mid-flight blocked on a permission. Nothing was polling, so the chip said
    // "waiting for approval" and no card ever came.
    const made = makeController({
      history: () => ({ turns: [], transcript: null }),
      live_run: () => ({ run_id: "r9" }),
      poll: (_f, n) =>
        n === 0
          ? poll({ permissions: [permRowFor("p1")], segments: [text("working")] })
          : poll({ done: true, permissions: [permRowFor("p1")], segments: [text("working")] }),
    });
    await made.controller.openSession("s-abc");
    // openSession does not wait on the watch, so drain it.
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(made.agent.of("live_run")[0]!.fields.session_id).toBe("s-abc");
    expect(made.controller.getState().permissions.map((p) => p.id)).toEqual(["p1"]);
  });

  test("a session with nothing running is left exactly as it restored", async () => {
    const made = makeController({
      history: () => ({ turns: [], transcript: null }),
      live_run: () => ({ run_id: "" }),
      poll: () => poll({ done: true }),
    });
    await made.controller.openSession("s-quiet");
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(made.agent.of("poll")).toEqual([]);
    expect(made.controller.getState().status).toBe("idle");
    // Every lap asked, and none of them invented a run param.
    expect(made.agent.of("live_run").length).toBe(8);
    expect(made.params.get("run") || "").toBe("");
  });

  // ── feedback R2-11/R2-13: the DOUBLE FLASH ────────────────────────────────
  // `historyLoading` goes false the instant the transcript lands, so a task
  // parked on a question painted its prose, scrolled to the bottom, and then
  // took the card a poll later with a second scroll. On a cards-wall tile that
  // jump is the whole tile. `adopting` is the renderer's gate: it stays up
  // across the whole restore-then-adopt window so both paint in one frame.
  test("`adopting` is up from the first frame and comes down at the first poll", async () => {
    /** state.adopting as of each step, in order. */
    const seen: boolean[] = [];
    let controller!: ChatController;
    const made = makeController({
      history: () => {
        // The very first frame — before the transcript has even arrived.
        seen.push(controller.getState().adopting);
        return { turns: [], transcript: null };
      },
      live_run: () => {
        // History has landed and `historyLoading` is already false…
        seen.push(controller.getState().historyLoading);
        seen.push(controller.getState().adopting);
        return { run_id: "r9" };
      },
      poll: (_f, n) =>
        n === 0
          ? poll({ permissions: [permRowFor("p1")], segments: [text("working")] })
          : poll({ done: true, permissions: [permRowFor("p1")], segments: [text("working")] }),
    });
    controller = made.controller;
    await controller.openSession("s-abc");
    await new Promise<void>((r) => setTimeout(r, 0));
    // first frame adopting, historyLoading already false, still adopting
    expect(seen).toEqual([true, false, true]);
    // The poll that carried the card also settled the gate.
    expect(controller.getState().adopting).toBe(false);
    expect(controller.getState().permissions.map((p) => p.id)).toEqual(["p1"]);
  });

  test("`adopting` comes down on the FIRST answer with nothing live", async () => {
    // Not after the laps run out: waiting them out would hide a reopened idle
    // conversation for ~3 s. The later laps are for a run that starts after
    // the paint, which no first paint should be held for.
    let afterFirst = true;
    const made = makeController({
      history: () => ({ turns: [], transcript: null }),
      live_run: (_f, n) => {
        if (n === 1) afterFirst = made.controller.getState().adopting;
        return { run_id: "" };
      },
      poll: () => poll({ done: true }),
    });
    await made.controller.openSession("s-quiet");
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(afterFirst).toBe(false);
    expect(made.controller.getState().adopting).toBe(false);
  });

  test("a thrown lookup cannot leave the gate up forever", async () => {
    const made = makeController({
      history: () => ({ turns: [], transcript: null }),
      live_run: () => {
        throw new Error("agent unreachable");
      },
    });
    await made.controller.openSession("s-abc");
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(made.controller.getState().adopting).toBe(false);
  });

  test("a `run` already on the URL leaves the adoption to resumeRun", async () => {
    const params = createMemoryParamsStore();
    params.set({ run: "r-from-url" });
    const made = makeController(
      { history: () => ({ turns: [], transcript: null }) },
      params,
    );
    await made.controller.openSession("s-abc");
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(made.agent.of("live_run")).toEqual([]);
  });

  test("a failed lookup ends the watch rather than retrying into a wiped log", async () => {
    const made = makeController({
      history: () => ({ turns: [], transcript: null }),
      live_run: () => {
        throw new Error("agent unreachable");
      },
    });
    await made.controller.openSession("s-abc");
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(made.agent.of("live_run").length).toBe(1);
    expect(made.controller.getState().trouble).toBeNull();
  });
});

describe("ownership: only the NEWEST loop owns the chrome (Bugbot PR #653)", () => {
  test("a superseded loop's own `done` clears neither the param nor the Stop chrome", async () => {
    let controller!: ChatController;
    let releaseFirstPoll: (() => void) | null = null;
    const made = makeController({
      start: (_f, n) => ({ run_id: n === 0 ? "r1" : "r2" }),
      send: () => ({ respawn: true as const }),
      poll: async (f, n) => {
        if (f.run_id === "r1" && n === 0) {
          // Trigger the respawn — which cancels r1 and starts a NEW loop on r2
          // — and only THEN let this (now superseded) poll come back `done`
          // with the kill's error.
          const held = new Promise<void>((r) => {
            releaseFirstPoll = r;
          });
          void controller.sendFollowUp("respawn me").then(() => releaseFirstPoll?.());
          await held;
          return poll({ done: true, error: "claude exited unexpectedly", cancelled: true });
        }
        // r2 stays live for one lap so the newer loop is demonstrably the owner
        // when the older one's `done` lands, then finishes.
        return poll({ done: true, segments: [text("the new turn")] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");

    // r2's loop is the one that cleared the param — and it cleared it once.
    expect(made.params.get("run")).toBeUndefined();
    // The superseded loop must NOT have posted its "Stopped." note: the reply
    // on screen belongs to the newer turn.
    expect(notes(controller).length).toBe(0);
    expect(assistants(controller).map((t) => t.text)).toContain("the new turn");
  });

  test("newChat retires the loop: it stops writing params and the transcript", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) => {
        if (n === 0) {
          controller.newChat(); // ← the "← Chats" button, live mid-turn
          return poll({ session_id: "s-late", segments: [text("orphaned")] });
        }
        return poll({ done: true });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    // The loop broke on the generation mismatch: no session_id write, no turn.
    expect(made.params.get("session_id")).toBeUndefined();
    expect(controller.getState().turns.length).toBe(0);
    expect(made.agent.of("poll").length).toBe(1);
  });
});

// ---- stop ------------------------------------------------------------------

describe("stop (T:15901)", () => {
  test("a stop swallows the kill's error and posts the ⏹ note once", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      cancel: () => ({ cancelled: "r1", still_queued: ["never delivered"] }),
      poll: async (_f, n) => {
        if (n === 0) {
          await controller.stopRun();
          return poll({ segments: [text("half a thought")] });
        }
        return poll({
          done: true,
          error: "claude exited unexpectedly",
          segments: [text("half a thought")],
        });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(notes(controller).map((n) => [n.glyph, n.text])).toEqual([["⏹", "Stopped."]]);
    // The work before the kill is real and stays.
    expect(assistants(controller)[0].text).toBe("half a thought");
    expect(controller.getState().trouble).toBeNull();
    // The CLI's own report of what it never delivered goes back to the composer.
    expect(made.stranded).toEqual([["never delivered"]]);
  });

  // R2-12: two messages queued, Stop pressed. agent.py now discards the
  // undrained inbox itself and folds those messages into `still_queued`
  // alongside whatever the CLI's own queue was holding — so BOTH come back and
  // neither is left posted as a bubble the agent never read. Landing matters:
  // these were acked by the inbox, so `!entry.landed` is not the road that
  // saves them; the backend naming them is.
  test("a stop with two messages queued hands both back and un-posts both", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      cancel: () => ({ cancelled: "r1", still_queued: ["msg1", "msg2"] }),
      poll: async (_f, n) => {
        if (n === 0) {
          await controller.sendFollowUp("msg1");
          await controller.sendFollowUp("msg2");
          // Both acked by the inbox before the stop.
          expect(controller.getState().queued).toEqual(["msg1", "msg2"]);
          await controller.stopRun();
          return poll({ segments: [text("half a thought")] });
        }
        return poll({ done: true, error: "killed", segments: [text("half a thought")] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(made.stranded).toEqual([["msg1", "msg2"]]);
    // Only the opening message is still a user bubble — the two queued rows
    // were never read, so claiming them in the transcript would be a lie.
    expect(users(controller).map((t) => t.text)).toEqual(["go"]);
    expect(controller.getState().queued).toEqual([]);
  });

  // Bugbot PR #1061 (MED). `still_queued` names WIRE TEXTS, and two follow-ups
  // can carry the same one. Matched through a Set, the second identical entry
  // found its name already consumed, so it was neither handed back nor left
  // standing — its bubble was dropped with the queue and the text was gone.
  test("two IDENTICAL follow-ups the CLI dropped both come back", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      cancel: () => ({ cancelled: "r1", still_queued: ["again", "again"] }),
      poll: async (_f, n) => {
        if (n === 0) {
          await controller.sendFollowUp("again");
          await controller.sendFollowUp("again");
          // Both acked by the inbox, so `!entry.landed` is not what saves
          // them — the backend naming them twice is.
          expect(controller.getState().queued).toEqual(["again", "again"]);
          await controller.stopRun();
          return poll({ segments: [text("half a thought")] });
        }
        return poll({ done: true, error: "killed", segments: [text("half a thought")] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(made.stranded).toEqual([["again", "again"]]);
    // Neither is left posted as a bubble the agent never read.
    expect(users(controller).map((t) => t.text)).toEqual(["go"]);
    expect(controller.getState().queued).toEqual([]);
  });

  test("a stop that never reached the backend takes its claim back", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      cancel: () => Promise.reject(new Error("socket closed")),
      poll: async (_f, n) => {
        if (n === 0) {
          await controller.stopRun();
          // The run is still going, so a SECOND press must be allowed through.
          await controller.stopRun();
          return poll();
        }
        return poll({ done: true, segments: [text("finished anyway")] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(made.agent.of("cancel").length).toBe(2);
    // The real ending is not relabelled as a stop the user never got.
    expect(notes(controller).length).toBe(0);
    expect(controller.getState().trouble?.message).toContain("Could not stop the run");
  });

  test("stopRun with nothing live is a no-op", async () => {
    const { controller, agent } = makeController({ cancel: () => ({ cancelled: "" }) });
    await controller.stopRun();
    expect(agent.calls.length).toBe(0);
  });

  // QA round 3a, defect 2. The CLI answers `{"still_queued": []}` for anything
  // fed through the held-open stdin — it echoes the follow-up into `out.jsonl`
  // the moment the inbox drains, so by the time the interrupt lands it no longer
  // counts the message as queued. T has nothing else to consult and drops the
  // text; the controller keeps its own record and does not have to.
  // ── feedback #11: WHEN a stop owes the composer the queued text back ────────
  test("an empty `still_queued` means Claude read it — the bubble stays put", async () => {
    // The measured behaviour of the held-open stdin: a follow-up is echoed into
    // `out.jsonl` the moment the inbox drains, so an interrupt landing after
    // that finds nothing queued and answers `{"still_queued": []}`. Claude READ
    // the message (Surya's mid-response drain), so yanking it out of the
    // transcript and back into the box would deny a turn the reader watched
    // happen. This used to hand the whole queue back on exactly this response.
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      cancel: () => ({ cancelled: "r1", still_queued: [] }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ segments: [text("working on it")] });
        if (n === 1) {
          await controller.sendFollowUp("Claude already read this");
          // The bubble is up and the hint is showing, both BEFORE the stop.
          expect(users(controller).map((t) => t.text)).toEqual([
            "go",
            "Claude already read this",
          ]);
          expect(controller.getState().queued).toEqual(["Claude already read this"]);
          await controller.stopRun();
          return poll({ segments: [text("working on it")] });
        }
        return poll({
          done: true,
          error: "claude exited unexpectedly",
          segments: [text("working on it")],
        });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    // Nothing owed back…
    expect(made.stranded).toEqual([]);
    // …and the bubble is still a turn of the conversation.
    expect(users(controller).map((t) => t.text)).toEqual(["go", "Claude already read this"]);
    // Nothing is "queued for this turn" once the turn is over, either way.
    expect(controller.getState().queued).toEqual([]);
    expect(notes(controller).map((n) => n.text)).toEqual(["Stopped."]);
  });

  test("a send still in flight when Stop lands DOES come back — it never landed", async () => {
    // The second road: `send` had not yet answered `{sent: true}`, so the inbox
    // never acknowledged the message and the interrupt is racing that write.
    // "It did not land" is the honest reading, and handing it back is
    // recoverable where losing it is not.
    let controller!: ChatController;
    let releaseSend: () => void = () => {};
    const held = new Promise<void>((r) => {
      releaseSend = r;
    });
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: async () => {
        await held;
        return { sent: true as const };
      },
      cancel: () => ({ cancelled: "r1", still_queued: [] }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ segments: [text("working")] });
        if (n === 1) {
          // Not awaited: the `send` is parked inside it.
          void controller.sendFollowUp("never acknowledged");
          // Let `sendFollowUp` get as far as the held `send`.
          await Promise.resolve();
          await Promise.resolve();
          expect(controller.getState().queued).toEqual(["never acknowledged"]);
          await controller.stopRun();
          releaseSend();
          return poll({ segments: [text("working")] });
        }
        return poll({ done: true, error: "claude exited", segments: [text("working")] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(made.stranded).toEqual([["never acknowledged"]]);
    expect(users(controller).map((t) => t.text)).toEqual(["go"]);
  });

  // ── feedback #12: the run ENDS across the follow-up boundary ───────────────
  test("a stop with a queued message still flips the status to idle", async () => {
    // `_send` writes `pending_echo`, and `_poll` clears it only by SEEING the
    // echo — which an interrupt guarantees will never come, while leaving the
    // host alive so the liveness escape never fires either. agent.py's
    // `_cancel` now retires that file on a landed interrupt; this is the
    // near end of the same fix: once `done` arrives the chrome goes, whatever
    // the follow-up did.
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      cancel: () => ({ cancelled: "r1", still_queued: [] }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ segments: [text("A. ")], text: "A. " });
        if (n === 1) {
          await controller.sendFollowUp("also");
          await controller.stopRun();
          return poll({ segments: [], text: "" });
        }
        // The interrupt landed and the echo gate is gone, so the turn can end.
        return poll({
          done: true,
          cancelled: true,
          error: "claude exited before completing the reply",
          segments: [text("A. ")],
          text: "A. ",
        });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    const state = controller.getState();
    expect(state.status).toBe("idle");
    expect(state.runId).toBeNull();
    expect(state.working).toBeNull();
    expect(state.queued).toEqual([]);
    // The stop's own note, not the kill's error.
    expect(notes(controller).map((n) => n.text)).toEqual(["Stopped."]);
    expect(state.trouble).toBeNull();
    // And no bubble left mid-stream.
    expect(assistants(controller).every((t) => !t.streaming)).toBe(true);
  });

  test("a still_queued the CLI DOES name hands back the typed text, not the wire form", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      send: () => ({ sent: true as const }),
      cancel: (_f) => ({
        cancelled: "r1",
        // The wire form is what the CLI would echo back, blocks and all.
        still_queued: [String(made.agent.of("send")[0]?.fields.message ?? "")],
      }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ segments: [text("working")] });
        if (n === 1) {
          await controller.sendFollowUp("look at this", {
            blocks: ["<live-app-state>\npane: idle\n</live-app-state>"],
          });
          await controller.stopRun();
          return poll({ segments: [text("working")] });
        }
        return poll({ done: true, error: "claude exited", segments: [text("working")] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    // The composer gets the words the user owns — never the composed payload,
    // whose app-state block they never typed and cannot edit sensibly.
    expect(made.stranded).toEqual([["look at this"]]);
    expect(users(controller).map((t) => t.text)).toEqual(["go"]);
  });
});

// ---- permission cards ------------------------------------------------------

const permRow = (over: Partial<PermissionRow> = {}): PermissionRow => ({
  id: "p1",
  tool: "Bash",
  input: { command: "rm -rf /" },
  created_at: 0,
  decision: "",
  scope: "",
  mode: "",
  answers: {},
  ...over,
});

describe("permission cards: pinned open, parked once answered (T:14665-14775)", () => {
  test("an open card is pinned LAST; a resolved one parks into the live turn", async () => {
    const { controller } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) => {
        if (n === 0) {
          return poll({ segments: [text("about to run something")], permissions: [permRow()] });
        }
        if (n === 1) {
          return poll({
            segments: [text("about to run something")],
            permissions: [permRow({ decision: "allow", scope: "once" })],
          });
        }
        return poll({
          done: true,
          segments: [text("about to run something")],
          permissions: [permRow({ decision: "allow", scope: "once" })],
        });
      },
    });
    const seen: string[] = [];
    const off = controller.subscribe(() => {
      const p = controller.getState().permissions[0];
      if (p) seen.push(String(p.placement));
    });
    await controller.sendMessage("go");
    off();
    expect(seen).toContain("open");
    const row = controller.getState().permissions[0];
    expect(row.placement).toBe("parked");
    // Filed into the turn that was streaming when it was answered.
    expect(row.parkedIn).toBe(assistants(controller)[0].key);
    expect(row.liveMode).toBe("prompt");
  });

  test("open cards sort after parked ones, in request order", async () => {
    const { controller } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: () =>
        poll({
          done: true,
          permissions: [
            permRow({ id: "a", decision: "allow" }),
            permRow({ id: "b" }),
            permRow({ id: "c", decision: "deny" }),
            permRow({ id: "d" }),
          ],
        }),
    });
    await controller.sendMessage("go");
    expect(controller.getState().permissions.map((p) => p.id)).toEqual(["a", "c", "b", "d"]);
  });

  test("the CLI's tool_use_id is stamped through as `toolUseId` (#18)", async () => {
    // What files a resolved card back against the exact chip it answered
    // rather than the newest chip of the same tool name.
    const { controller } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: () =>
        poll({
          done: true,
          permissions: [{ ...permRow(), tool_use_id: "toolu_42" }],
          segments: [text("done")],
        }),
    });
    await controller.sendMessage("go");
    expect(controller.getState().permissions[0]!.toolUseId).toBe("toolu_42");
  });

  test("a request with no tool_use_id leaves the annotation off entirely", async () => {
    // The name-and-order fallback in the transcript reads the ABSENCE, so an
    // empty string must not be stamped as if it were an id.
    const { controller } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: () =>
        poll({ done: true, permissions: [permRow()], segments: [text("done")] }),
    });
    await controller.sendMessage("go");
    expect(controller.getState().permissions[0]!.toolUseId).toBeUndefined();
  });

  test("the replay is idempotent: the same list twice changes nothing", async () => {
    const { controller } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) =>
        n < 2 ? poll({ permissions: [permRow()] }) : poll({ done: true, permissions: [permRow()] }),
    });
    await controller.sendMessage("go");
    expect(controller.getState().permissions.length).toBe(1);
  });

  test("a decide writes back what LANDED on disk, and the picker follows", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      decide: () => ({
        decided: "p1",
        decision: "allow" as const,
        scope: "once" as const,
        mode: "auto" as const,
        answers: {},
      }),
      poll: async (_f, n) => {
        if (n === 0) {
          const p = poll({ permissions: [permRow()] });
          return p;
        }
        if (n === 1) {
          await controller.decidePermission("p1", "allow", "once", "auto");
          return poll({ permissions: [permRow({ decision: "allow", mode: "auto" })] });
        }
        return poll({ done: true, permissions: [permRow({ decision: "allow", mode: "auto" })] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(made.agent.of("decide")[0].fields).toEqual({
      run_id: "r1",
      request_id: "p1",
      decision: "allow",
      scope: "once",
      mode: "auto",
    });
    // "let Claude decide from here" moves the session's permission mode.
    expect(made.params.get("permission")).toBe("auto");
    expect(controller.getState().permissions[0].placement).toBe("parked");
  });

  test("a question's answers go as one string per question, plus `custom`", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      decide: () => ({
        decided: "q1",
        decision: "allow" as const,
        scope: "once" as const,
        mode: "" as const,
        answers: { "Which one?": "Blue, Other" },
      }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ permissions: [permRow({ id: "q1", tool: "AskUserQuestion" })] });
        if (n === 1) {
          await controller.answerQuestion(
            "q1",
            { "Which one?": ["Blue", "teal, actually"] },
            { "Which one?": "teal, actually" },
          );
          return poll({ permissions: [permRow({ id: "q1", decision: "allow" })] });
        }
        return poll({ done: true, permissions: [permRow({ id: "q1", decision: "allow" })] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(made.agent.of("decide")[0].fields).toEqual({
      run_id: "r1",
      request_id: "q1",
      decision: "allow",
      scope: "once",
      answers: JSON.stringify({ "Which one?": "Blue, teal, actually" }),
      custom: JSON.stringify({ "Which one?": "teal, actually" }),
    });
  });

  test("approving a plan always moves the picker off \"plan\" (T:14548)", async () => {
    let controller!: ChatController;
    const made = makeController(
      {
        start: () => ({ run_id: "r1" }),
        decide: () => ({
          decided: "pl1",
          decision: "allow" as const,
          scope: "once" as const,
          mode: "" as const,
          answers: {},
        }),
        poll: async (_f, n) => {
          if (n === 0) return poll({ permissions: [permRow({ id: "pl1", tool: "ExitPlanMode" })] });
          await controller.decidePlan("pl1", "allow");
          return poll({ done: true, permissions: [permRow({ id: "pl1", decision: "allow" })] });
        },
      },
      createMemoryParamsStore({ permission: "plan" }),
    );
    controller = made.controller;
    await controller.sendMessage("go");
    expect(made.params.get("permission")).toBe("prompt");
    expect(made.agent.of("decide")[0].fields).toMatchObject({ note: "", mode: "" });
  });

  test("\"Keep planning\" carries the note", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      decide: () => ({ error: "" }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ permissions: [permRow({ id: "pl1", tool: "ExitPlanMode" })] });
        await controller.decidePlan("pl1", "deny", undefined, "use sqlite instead");
        return poll({ done: true });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(made.agent.of("decide")[0].fields).toMatchObject({
      decision: "deny",
      note: "use sqlite instead",
    });
  });

  test("a failed decide brings the card back with the reason on it", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      decide: () => ({ error: "request already settled" }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ permissions: [permRow()] });
        await controller.decidePermission("p1", "allow");
        return poll({ done: true, permissions: [permRow()] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    const row = controller.getState().permissions[0];
    expect(row.placement).toBe("open");
    expect(row.sendError).toBe("Could not send that: request already settled");
  });

  test("open cards are pinned LAST, as one contiguous block (T:14680)", async () => {
    // Two requests open at once (a sub-agent's card beside the main one), with
    // a third already answered: the answered one keeps its place and the two
    // open ones end the list, in request order.
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) =>
        poll({
          done: n > 0,
          permissions: [
            permRow({ id: "p1", decision: "allow" }),
            permRow({ id: "p2" }),
            permRow({ id: "p3" }),
          ],
        }),
    });
    await made.controller.sendMessage("go");
    const rows = made.controller.getState().permissions;
    expect(rows.map((r) => r.id)).toEqual(["p1", "p2", "p3"]);
    expect(rows.map((r) => r.placement)).toEqual(["parked", "open", "open"]);
  });

  test("a card is parked into the turn it was answered in, ONCE, and stays there", async () => {
    // T:14728 `parkResolvedCard`: filed at the live turn's tail, and never
    // dragged to the tail again — "a receipt filed after everything it came
    // before" is the bug the parking exists to fix.
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      decide: () => ({
        decided: "p1",
        decision: "allow" as const,
        scope: "once" as const,
        mode: "" as const,
        answers: {},
      }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ segments: [text("first turn")], permissions: [permRow()] });
        if (n === 1) {
          await controller.decidePermission("p1", "allow");
          return poll({
            segments: [text("first turn")],
            permissions: [permRow({ decision: "allow" })],
          });
        }
        return poll({
          done: true,
          segments: [text("first turn")],
          permissions: [permRow({ decision: "allow" })],
        });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    const parkedIn = controller.getState().permissions[0].parkedIn;
    expect(parkedIn).toBeTruthy();
    // A SECOND turn streams, replaying the same resolved request every poll.
    await controller.sendMessage("again");
    expect(controller.getState().permissions[0].parkedIn).toBe(parkedIn);
    // …and the turn it names is still in the transcript, so the card renders in
    // chronological order instead of sliding into whatever is streaming now.
    expect(controller.getState().turns.some((t) => t.key === parkedIn)).toBe(true);
  });

  test("a parked card SURVIVES the run ending (T:14742)", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      decide: () => ({
        decided: "p1",
        decision: "deny" as const,
        scope: "once" as const,
        mode: "" as const,
        answers: {},
      }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ segments: [text("body")], permissions: [permRow()] });
        await controller.decidePermission("p1", "deny");
        return poll({
          done: true,
          segments: [text("body")],
          permissions: [permRow({ decision: "deny" })],
        });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    const state = controller.getState();
    const row = state.permissions[0];
    expect(row).toMatchObject({ decision: "deny", placement: "parked" });
    // The turn it is filed into is no longer STREAMING — which is exactly the
    // state that used to unmount the card and lose the receipt.
    const turn = state.turns.find((t) => t.key === row.parkedIn) as AssistantTurn | undefined;
    expect(turn).toBeDefined();
    expect(turn?.streaming).toBeFalsy();
  });

  test("dismiss DENIES and keeps the card — the receipt is what T leaves behind", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      decide: () => ({
        decided: "p1",
        decision: "deny" as const,
        scope: "once" as const,
        mode: "" as const,
        answers: {},
      }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ permissions: [permRow()] });
        controller.dismissCard("p1");
        await Promise.resolve();
        return poll({ done: true, permissions: [permRow()] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    // T:14118's `dismiss` is a real `deny` POST that RESOLVES the card
    // (T:14126-14140): the tool call was blocked, so "✗ Not answered" is the
    // record of how it was unblocked. Filtering the row out instead lost that,
    // and left a FAILED dismiss with a blocked run and nothing on screen.
    const rows = controller.getState().permissions;
    expect(rows.length).toBe(1);
    expect(rows[0]).toMatchObject({ id: "p1", decision: "deny", placement: "parked" });
    expect(made.agent.of("decide")[0].fields).toMatchObject({ decision: "deny", scope: "once" });
  });
});

// ---- skills and app_state --------------------------------------------------

describe("skills and app_state rows (T:15771-15837)", () => {
  test("a replayed skill call makes exactly one row and one note", async () => {
    const { controller } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) =>
        n < 2
          ? poll({ skills: [{ id: "s-1", skill: "fused-render-authoring" }] })
          : poll({ done: true, skills: [{ id: "s-1", skill: "fused-render-authoring" }] }),
    });
    await controller.sendMessage("go");
    expect(controller.getState().skills).toEqual([{ id: "s-1", skill: "fused-render-authoring" }]);
    expect(notes(controller).map((n) => [n.glyph, n.text])).toEqual([
      ["◆", "skill · fused-render-authoring"],
    ]);
  });

  test("an unanswered request is surfaced with its poll count, then answered once", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      app_state: () => ({ answered: "a1" }),
      poll: async (_f, n) => {
        const req = [{ id: "a1", reason: "check the console", created_at: 0 }];
        if (n === 0) return poll({ app_state: req });
        if (n === 1) {
          const row = controller.getState().appState[0];
          expect(row.pollsSeen).toBe(1);
          expect(row.waitedOut).toBe(false);
          await controller.answerAppState("a1", '{"url":"/x"}');
          // The claim is on disk now, so the replay must not re-surface it.
          return poll({ app_state: req });
        }
        return poll({ done: true, app_state: [] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(made.agent.of("app_state").length).toBe(1);
    expect(made.agent.of("app_state")[0].fields).toEqual({
      run_id: "r1",
      request_id: "a1",
      state: '{"url":"/x"}',
    });
    expect(controller.getState().appState).toEqual([]);
    expect(notes(controller).map((n) => n.text)).toEqual(["read app state — check the console"]);
  });

  test("`waitedOut` flips once the pane has had ~2 s (5 polls) to answer", async () => {
    let seen = false;
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) => {
        const req = [{ id: "a1", reason: "", created_at: 0 }];
        if (n > 0 && controller.getState().appState[0]?.waitedOut) seen = true;
        return n < 6 ? poll({ app_state: req }) : poll({ done: true, app_state: [] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(seen).toBe(true);
    expect(made.agent.of("poll").length).toBe(7);
  });

  test("a failed app_state write un-claims the id so the next poll retries", async () => {
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      app_state: (_f, n) => (n === 0 ? { error: "write failed", retry: true } : { answered: "a1" }),
      poll: async (_f, n) => {
        const req = [{ id: "a1", reason: "", created_at: 0 }];
        if (n === 0) {
          await controller.answerAppState("a1", "{}");
          return poll({ app_state: req });
        }
        if (n === 1) {
          await controller.answerAppState("a1", "{}");
          return poll({ app_state: req });
        }
        return poll({ done: true, app_state: [] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(made.agent.of("app_state").length).toBe(2);
    // …and only ONE note, however many attempts it took (T:15797).
    expect(notes(controller).map((n) => n.text)).toEqual(["read app state"]);
  });
});

// ---- refusals and throws ---------------------------------------------------

describe("poll refusals and throws", () => {
  test("an unknown run is a trouble card and the stale param goes", async () => {
    const params = createMemoryParamsStore({ run: "r-old" });
    const { controller } = makeController(
      {
        start: () => ({ run_id: "r1" }),
        poll: () => ({
          text: "",
          done: true,
          session_id: "",
          error: "unknown run_id",
          permissions: [],
          app_state: [],
          skills: [],
          retry: null,
          retry_total: 0,
          retry_status: 0,
          segments: [],
        }),
      },
      params,
    );
    await controller.sendMessage("go");
    expect(controller.getState().trouble).toEqual({ kind: "unknown-run", message: "unknown run_id" });
    expect(params.get("run")).toBeUndefined();
    expect(assistants(controller).length).toBe(0);
  });

  test("a run for another target is refused the same way", async () => {
    const { controller } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: () => ({
        text: "",
        done: true,
        session_id: "",
        error: "run is for another target",
        permissions: [],
        app_state: [],
        skills: [],
        retry: null,
        retry_total: 0,
        retry_status: 0,
        segments: [],
      }),
    });
    await controller.sendMessage("go");
    expect(controller.getState().trouble?.kind).toBe("unknown-run");
  });

  test("a THROWN poll drops the partial bubble but KEEPS `?run=` (T:16378)", async () => {
    const { controller, params } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) =>
        n === 0
          ? poll({ segments: [text("half a sen")] })
          : Promise.reject(new Error("Failed to fetch")),
    });
    await controller.sendMessage("go");
    expect(assistants(controller).length).toBe(0);
    expect(params.get("run")).toBe("r1");
    expect(controller.getState().trouble).toMatchObject({ kind: "network" });
    expect(controller.getState().status).toBe("idle");
  });

  test("an error the user did not ask for drops the reply and is reported", async () => {
    const { controller } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) =>
        n === 0
          ? poll({ segments: [text("half")] })
          : poll({ done: true, error: "claude exited unexpectedly", segments: [text("half")] }),
    });
    await controller.sendMessage("go");
    expect(assistants(controller).length).toBe(0);
    expect(controller.getState().trouble?.message).toBe("claude exited unexpectedly");
  });
});

// ---- history, resume, newChat ---------------------------------------------

describe("openSession / resumeRun / newChat", () => {
  test("openSession replaces the transcript and notes the watermark", async () => {
    const { controller, params } = makeController({
      history: () => ({
        turns: [
          { role: "user" as const, text: "make it blue", uuid: "u1" },
          { role: "assistant" as const, text: "done", stopped: true as const },
        ],
        transcript: { path: "/t.jsonl", mtime: 5, size: 9 },
      }),
    });
    await controller.openSession("s-42");
    expect(params.get("session_id")).toBe("s-42");
    expect(controller.getState().historyLoading).toBe(false);
    expect(controller.getState().transcript).toEqual({ path: "/t.jsonl", mtime: 5, size: 9 });
    expect(controller.getState().turns.map((t) => t.role)).toEqual(["user", "assistant"]);
    expect((controller.getState().turns[1] as AssistantTurn).stopped).toBe(true);
  });

  test("a failed restore leaves an empty log and no trouble card", async () => {
    const { controller } = makeController({ history: () => ({ error: "no such session" }) });
    await controller.openSession("s-42");
    expect(controller.getState().turns).toEqual([]);
    expect(controller.getState().trouble).toBeNull();
    expect(controller.getState().historyLoading).toBe(false);
  });

  test("resumeRun retries a run dir that is not visible yet, then streams it", async () => {
    const { controller, agent } = makeController({
      poll: (_f, n) => {
        if (n < 3) {
          return {
            text: "",
            done: true,
            session_id: "",
            error: "unknown run_id",
            permissions: [],
            app_state: [],
            skills: [],
            retry: null,
            retry_total: 0,
            retry_status: 0,
            segments: [],
          };
        }
        return poll({ done: true, segments: [text("re-attached")] });
      },
    });
    await controller.resumeRun("r-embedded");
    expect(agent.of("poll").length).toBe(4);
    expect(assistants(controller).map((t) => t.text)).toEqual(["re-attached"]);
  });

  test("resumeRun writes off a genuinely stale param", async () => {
    const params = createMemoryParamsStore({ run: "r-dead" });
    const { controller } = makeController(
      {
        poll: () => ({
          text: "",
          done: true,
          session_id: "",
          error: "unknown run_id",
          permissions: [],
          app_state: [],
          skills: [],
          retry: null,
          retry_total: 0,
          retry_status: 0,
          segments: [],
        }),
      },
      params,
    );
    await controller.resumeRun("r-dead");
    expect(params.get("run")).toBeUndefined();
    expect(controller.getState().trouble?.kind).toBe("unknown-run");
  });

  // Bugbot PR #1061 (HIGH). `openSession` raises `adopting` before the first
  // frame, and with a `run` on the URL it hands the LOWERING to `resumeRun`
  // (`adoptLiveRun` is skipped for exactly that reason). Only a live
  // `pollLoop`'s first poll used to clear it — so every road out of `resumeRun`
  // that never reaches the loop left the gate up, and `Transcript` reads the
  // gate as `is-settling`: `visibility: hidden` for the life of the page. A
  // restored conversation, rendered and invisible.
  test("a stale `run=` lowers the adoption gate — the transcript is never left hidden", async () => {
    const params = createMemoryParamsStore({ run: "r-dead" });
    const { controller } = makeController(
      {
        history: () => ({ turns: [{ role: "user" as const, text: "make it blue", uuid: "u1" }] }),
        poll: () => unknownRunPoll(),
      },
      params,
    );
    await controller.openSession("s-42");
    // The gate is up and NOTHING but `resumeRun` can bring it down here.
    expect(controller.getState().adopting).toBe(true);
    await controller.resumeRun("r-dead");
    expect(controller.getState().adopting).toBe(false);
    // …and the restored history is still there under the stale-param notice.
    expect(controller.getState().turns.map((t) => t.role)).toEqual(["user", "error"]);
    expect(controller.getState().trouble?.kind).toBe("unknown-run");
  });

  test("a run that FINISHED while the frame was away lowers the gate too", async () => {
    const params = createMemoryParamsStore({ run: "r1" });
    const { controller } = makeController(
      {
        history: () => ({ turns: [{ role: "user" as const, text: "hi", uuid: "u1" }] }),
        poll: () => poll({ done: true, segments: [text("finished offscreen")] }),
      },
      params,
    );
    await controller.openSession("s-42");
    expect(controller.getState().adopting).toBe(true);
    await controller.resumeRun("r1");
    expect(controller.getState().adopting).toBe(false);
  });

  test("a resumeRun refused by the send gate lowers the gate rather than hiding forever", async () => {
    // The third road: `openSession` still holds `sending` when the boot's
    // `resumeRun` arrives, so it bails before the probe. A bail is a reason to
    // paint the restored transcript, not to hide it.
    const params = createMemoryParamsStore({ run: "r1" });
    let controller!: ChatController;
    /** `state.adopting` as of the refused `resumeRun`, recorded not asserted:
     *  the assertion is outside the closure. */
    const duringRestore: boolean[] = [];
    const made = makeController(
      {
        history: async () => {
          // Inside the gate: `openSession` set `sending` before this await.
          await controller.resumeRun("r1");
          duringRestore.push(controller.getState().adopting);
          return { turns: [] };
        },
        poll: () => poll({ done: true }),
      },
      params,
    );
    controller = made.controller;
    await controller.openSession("s-42");
    // The probe was never sent — the gate was held — and the flag came down.
    expect(made.agent.of("poll").length).toBe(0);
    expect(duringRestore).toEqual([false]);
  });

  test("resumeRun repairs a run that finished while the frame was away", async () => {
    const { controller, params } = makeController({
      poll: () => poll({ done: true, segments: [text("finished offscreen")], text: "finished offscreen" }),
    });
    await controller.resumeRun("r1");
    expect(assistants(controller).map((t) => t.text)).toEqual(["finished offscreen"]);
    expect(params.get("run")).toBeUndefined();
    expect(params.get("session_id")).toBe("s1");
  });

  test("resumeRun streams a run still in flight", async () => {
    const { controller } = makeController({
      poll: (_f, n) =>
        n === 0
          ? poll({ segments: [text("mid")] })
          : poll({ done: true, segments: [text("mid and done")] }),
    });
    await controller.resumeRun("r1");
    expect(assistants(controller).map((t) => t.text)).toEqual(["mid and done"]);
    expect(controller.getState().status).toBe("idle");
  });

  test("newChat clears the transcript and both params", () => {
    const params = createMemoryParamsStore({ session_id: "s1", run: "r1", permission: "auto" });
    const { controller } = makeController({}, params);
    controller.newChat();
    expect(params.get("session_id")).toBeUndefined();
    expect(params.get("run")).toBeUndefined();
    // Only those two: the picker is not a per-transcript record.
    expect(params.get("permission")).toBe("auto");
    expect(controller.getState()).toMatchObject({ turns: [], permissions: [], sessionId: null });
  });
});

describe("escape has no claim on a run (Akshil, 2026-09-03)", () => {
  test("the contract offers Escape no protocol-level hook at all", () => {
    const { controller } = makeController({ start: () => ({ run_id: "r1" }) });
    // The rule is enforced by ABSENCE now, not by a stub that always answers
    // "none": Escape's claimants are every one of them UI-owned and stop the
    // event themselves (T:15947-15978), so a controller method would be a
    // contract the run loop cannot keep. A future claimant that genuinely needs
    // one adds it WITH the state it reads.
    expect("escapeAction" in controller).toBe(false);
  });
});

describe("working state comes off the poll (T:14774)", () => {
  test("phase, activity, retry and the token estimate are published verbatim", async () => {
    const seen: (number | string | null)[][] = [];
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) => {
        if (n === 0) {
          return poll({
            phase: "retrying",
            tokens: 0,
            // 40 chars ⇒ the estimate is 10 until usage arrives (T:16230).
            text: "x".repeat(40),
            retry: { attempt: 2, max_retries: 5, delay_ms: 100, status: 429, error: "busy" },
          });
        }
        return poll({ done: true, tokens: 999, text: "x".repeat(40) });
      },
    });
    controller = made.controller;
    const off = made.controller.subscribe(() => {
      const w = controller.getState().working;
      if (w) seen.push([w.phase, w.tokens, w.retry ? w.retry.status : null]);
    });
    await controller.sendMessage("go");
    off();
    expect(seen).toContainEqual(["retrying", 10, 429]);
    expect(controller.getState().working).toBeNull();
  });
});

// ---- the live permission mode ----------------------------------------------
//
// `permChoices` withholds "Allow, and let Claude decide from here" from a run
// already in `auto` and from a run mid-plan (T:13893). With no live mode in the
// state the card fell back to `DEFAULT_PERMISSION` and offered the escalation in
// both — a button that either does nothing or leaves plan mode by a side door.
describe("ChatState.permissionMode: the mode the RUN is in (T:13884-13886)", () => {
  test("seeded from the mode the turn was spawned in, before any poll", async () => {
    const spawned: string[] = [];
    const { controller } = makeController({
      start: (f) => {
        spawned.push(String(controller.getState().permissionMode));
        expect(f.permission_mode).toBe("plan");
        return { run_id: "r1" };
      },
      poll: () => poll({ done: true, mode: "plan", segments: [text("planning")] }),
    });
    await controller.sendMessage("go", { permission: "plan" });
    expect(spawned).toEqual(["plan"]);
    expect(controller.getState().permissionMode).toBe("plan");
  });

  test("the picker's param is the seed when the send names no mode", async () => {
    const params = createMemoryParamsStore({ permission: "acceptEdits" });
    const { controller } = makeController(
      { start: () => ({ run_id: "r1" }), poll: () => poll({ done: true, mode: "acceptEdits" }) },
      params,
    );
    await controller.sendMessage("go");
    expect(controller.getState().permissionMode).toBe("acceptEdits");
  });

  test("junk on the URL never reaches the union — it falls to the CLI default", async () => {
    const params = createMemoryParamsStore({ permission: "yolo" });
    const { controller, agent } = makeController(
      { start: () => ({ run_id: "r1" }), poll: () => poll({ done: true, mode: "prompt" }) },
      params,
    );
    await controller.sendMessage("go");
    expect(controller.getState().permissionMode).toBe("prompt");
    // …but the WIRE still carries it verbatim: agent.py is what rejects a mode.
    expect(agent.of("start")[0].fields.permission_mode).toBe("yolo");
  });

  test("poll's own `mode` outranks the seed: it is agent.py's `_live_mode`", async () => {
    const { controller } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) =>
        n === 0 ? poll({ mode: "auto" }) : poll({ done: true, mode: "auto", segments: [text("ok")] }),
    });
    await controller.sendMessage("go");
    expect(controller.getState().permissionMode).toBe("auto");
  });

  test("the escalation moves the live mode, not only the picker (T:13988-13992)", async () => {
    // Read the instant the decide resolves, not at the end of the run: what
    // matters is that the NEXT card in this turn is offered the right buttons.
    const after: string[] = [];
    let controller!: ChatController;
    const made = makeController({
      start: () => ({ run_id: "r1" }),
      decide: () => ({
        decided: "p1",
        decision: "allow" as const,
        scope: "once" as const,
        mode: "auto" as const,
        answers: {},
      }),
      poll: async (_f, n) => {
        if (n === 0) return poll({ permissions: [permRow()] });
        await controller.decidePermission("p1", "allow", "once", "auto");
        after.push(String(controller.getState().permissionMode));
        return poll({ done: true, mode: "auto", permissions: [permRow({ decision: "allow" })] });
      },
    });
    controller = made.controller;
    await controller.sendMessage("go");
    expect(after).toEqual(["auto"]);
    expect(made.params.get("permission")).toBe("auto");
  });

  test("approving a plan leaves plan mode here too (T:14548-14580)", async () => {
    const after: string[] = [];
    let controller!: ChatController;
    const made = makeController(
      {
        start: () => ({ run_id: "r1" }),
        decide: () => ({
          decided: "pl1",
          decision: "allow" as const,
          scope: "once" as const,
          mode: "" as const,
          answers: {},
        }),
        poll: async (_f, n) => {
          if (n === 0) {
            return poll({ mode: "plan", permissions: [permRow({ id: "pl1", tool: "ExitPlanMode" })] });
          }
          await controller.decidePlan("pl1", "allow");
          // No `setMode` rode along, so the landing mode is the CLI default —
          // and the live mode has to leave "plan" or every later card in this
          // turn still thinks it is planning and withholds the escalation.
          after.push(String(controller.getState().permissionMode));
          return poll({ done: true, mode: "prompt" });
        },
      },
      createMemoryParamsStore({ permission: "plan" }),
    );
    controller = made.controller;
    await controller.sendMessage("go");
    expect(after).toEqual(["prompt"]);
    expect(made.params.get("permission")).toBe("prompt");
  });
});

// ---- failures in the log ---------------------------------------------------

describe("addError: the slot AND the row (T:13698)", () => {
  const errors = (c: ChatController) => c.getState().turns.filter((t) => t.role === "error");

  test("a failure lands in the transcript where it happened, and in the slot", async () => {
    const { controller } = makeController({
      start: () => ({ run_id: "r1" }),
      poll: () => poll({ done: true, error: "claude exited", segments: [text("half a reply")] }),
    });
    await controller.sendMessage("go");
    expect(controller.getState().trouble?.message).toBe("claude exited");
    expect(errors(controller).map((t) => t.text)).toEqual(["claude exited"]);
    // The row carries the classification the card would use, so a renderer can
    // dress it the same way.
    expect(errors(controller)[0]).toMatchObject({ role: "error", kind: "generic" });
    // Chronological: after the user's bubble, not hoisted to the top or bottom.
    expect(controller.getState().turns.map((t) => t.role)).toEqual(["user", "error"]);
  });

  test("a SECOND failure stacks a row while the slot keeps only the newest", async () => {
    const { controller } = makeController({
      start: (_f, n) => (n === 0 ? { error: "first thing broke" } : { error: "then another" }),
      poll: () => poll({ done: true }),
    });
    await controller.sendMessage("one");
    await controller.sendMessage("two");
    expect(errors(controller).map((t) => t.text)).toEqual(["first thing broke", "then another"]);
    expect(controller.getState().trouble?.message).toBe("then another");
    // Each row has its own key, so the transcript can render both.
    const keys = errors(controller).map((t) => t.key);
    expect(new Set(keys).size).toBe(2);
  });

  test("the rows survive the next send, which clears the slot", async () => {
    const { controller } = makeController({
      start: (_f, n) => (n === 0 ? { error: "it broke" } : { run_id: "r1" }),
      poll: () => poll({ done: true, segments: [text("fine now")] }),
    });
    await controller.sendMessage("one");
    await controller.sendMessage("two");
    expect(controller.getState().trouble).toBeNull();
    expect(errors(controller).map((t) => t.text)).toEqual(["it broke"]);
  });

  test("a recognised failure keeps its kind, so the card copy still applies", async () => {
    const { controller } = makeController({
      start: () => ({ error: "unknown run_id" }),
      poll: () => poll({ done: true }),
    });
    await controller.sendMessage("go");
    expect(errors(controller)[0]).toMatchObject({ kind: "unknown-run" });
    expect(controller.getState().trouble?.kind).toBe("unknown-run");
  });
});

describe("trimPermCards", () => {
  const cards = (n: number, settled: (i: number) => boolean) => {
    const m = new Map<string, { decision?: string | null }>();
    for (let i = 0; i < n; i++) m.set("p" + i, settled(i) ? { decision: "allow" } : {});
    return m;
  };

  test("a real conversation is never trimmed at all", () => {
    // The cap is a bound on a pathological session, not a policy: perm cards are
    // RENDERED state, and an evicted one is a receipt gone out of the transcript.
    const m = cards(40, (i) => i < 30);
    trimPermCards(m);
    expect(m.size).toBe(40);
  });

  test("over the ceiling, the OLDEST SETTLED cards go first", () => {
    const m = cards(PERM_CARD_MAX + 3, (i) => i % 2 === 0);
    trimPermCards(m);
    expect(m.size).toBe(PERM_CARD_MAX);
    // p0, p2, p4 — settled and oldest. Nothing open was touched.
    expect(m.has("p0")).toBe(false);
    expect(m.has("p2")).toBe(false);
    expect(m.has("p4")).toBe(false);
    expect(m.has("p1")).toBe(true);
    expect(m.has("p6")).toBe(true);
  });

  test("an OPEN card is never evicted — it is the only control that unblocks a run", () => {
    const m = cards(PERM_CARD_MAX + 5, () => false);
    trimPermCards(m);
    expect(m.size).toBe(PERM_CARD_MAX + 5);
  });
});
