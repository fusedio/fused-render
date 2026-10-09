// TEST-ONLY: the events bus as the run-controller suites script it.
//
// The controller reads the run stream (`claude.run`) and the live-run probe
// (`claude.live`) as subscriptions now (see run-controller.ts's protocol
// note), but the suites were written — and still read best — as a scripted
// agent.py: `poll: (fields, n) => …`, `live_run: (fields, n) => …`, one entry
// per lap. This adapter keeps that meaning: every time the controller's feed
// waits for a frame it calls `pull()`, and each pull is ONE call of the
// scripted `run` whose answer is pushed as ONE frame. So entry `n` of a
// `poll` script is still the `n`th thing the loop sees, and `agent.of("poll")`
// still counts what the loop consumed.
//
//   * `claude.run {run_id, file, native, queue}` → `run("poll", params)`; the
//     answer is the frame's body, exactly as the server's topic sends it.
//   * `claude.live {file, session_id}` → `run("live_run", {file, session_id})`;
//     the body is `{live: <answer>, liveness: null}`.
//   * A REJECTED script entry is an `err` frame, shaped the way the controller's
//     `frameError` reads it back: an `AgentError` `"Timeout"` / `"NotRun"` is a
//     504, any other `AgentError` a 500 with its message, and anything else (a
//     `fetch` that never left the machine) carries no status at all.
//
// The real bus pushes on its own clock and has no `pull`; nothing in
// production imports this file.
import type { FusedEventsMeta, SubscribeLike } from "@platform/lib/events";
import type { FusedEvents } from "@static/events-client.js";
import { AgentError, runAgent } from "./agent";

/** The scripted transport — `fakeAgent().run`, typed as `runAgent` is. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Run = (action: any, fields: any) => unknown;

/** Every subscription the adapter has seen, for the "one subscription per
 *  turn" assertions. */
export interface BusLog {
  topic: string;
  params: Record<string, unknown>;
  closed: boolean;
}

function toMeta(err: unknown): FusedEventsMeta {
  if (err instanceof AgentError) {
    const timeout = err.type === "Timeout" || err.type === "NotRun";
    return { error: err.message, status: timeout ? 504 : 500 };
  }
  return { error: err instanceof Error ? err.message : String(err) };
}

/** A `deps.subscribe` that turns the scripted `run` into frames, one per ask. */
export function scriptedBus(run: Run, log: BusLog[] = []): SubscribeLike {
  return ((topic, params, cb) => {
    const p = { ...(params || {}) } as Record<string, unknown>;
    const entry: BusLog = { topic, params: p, closed: false };
    log.push(entry);
    let inflight = false;
    const ask = () => {
      if (entry.closed || inflight) return;
      let action: string;
      let fields: Record<string, unknown>;
      if (topic === "claude.run") {
        action = "poll";
        fields = { ...p };
      } else if (topic === "claude.live") {
        action = "live_run";
        fields = { file: p.file, session_id: p.session_id };
      } else {
        return; // nothing else is scripted: a topic that never answers
      }
      inflight = true;
      let answer: Promise<unknown>;
      try {
        answer = Promise.resolve(run(action, fields));
      } catch (err) {
        answer = Promise.reject(err);
      }
      answer.then(
        (body) => {
          inflight = false;
          if (entry.closed) return;
          const snap = topic === "claude.live" ? { live: body, liveness: null } : body;
          (cb as (s: unknown, d: unknown, m: FusedEventsMeta) => void)(snap, null, { gen: null });
        },
        (err) => {
          inflight = false;
          if (entry.closed) return;
          cb(null, null, toMeta(err));
        },
      );
    };
    // The snapshot on subscribe — the first frame always arrives.
    ask();
    return Object.assign(
      () => {
        entry.closed = true;
      },
      { pull: ask },
    );
  }) as SubscribeLike;
}

/**
 * THE SAME FRAMES FOR A WHOLE MOUNTED CHAT, for the `ClaudeChat.*` suites
 * that script agent.py by stubbing `fetch` for `/api/claude/agent`. Installed
 * with `setEventsClientForTests`: `claude.run` / `claude.live` are answered by
 * the real `runAgent` (so the suite's stub, and its `runs` log, see exactly the
 * `poll` / `live_run` POSTs the old loop made); every other topic never answers,
 * as in bun with no client. Asks after the first are spaced by the old 400 ms
 * cadence, so a stub that keeps a run open is not spun in a tight loop.
 */
export function agentBackedEventsClient(gapMs = 400): Partial<FusedEvents> {
  const run = (action: string, fields: Record<string, unknown>) =>
    (runAgent as unknown as (a: string, f: Record<string, unknown>) => Promise<unknown>)(action, fields);
  const inner = scriptedBus(run);
  const subscribe = ((topic, params, cb, opts) => {
    if (topic !== "claude.run" && topic !== "claude.live") return () => {};
    const off = inner(topic, params, cb, opts) as (() => void) & { pull?: () => void };
    let timer: ReturnType<typeof setTimeout> | null = null;
    return Object.assign(
      () => {
        if (timer) clearTimeout(timer);
        timer = null;
        off();
      },
      {
        pull: () => {
          if (timer) return;
          timer = setTimeout(() => {
            timer = null;
            off.pull?.();
          }, gapMs);
        },
      },
    );
  }) as SubscribeLike;
  return {
    subscribe: subscribe as unknown as FusedEvents["subscribe"],
    resync: () => false,
  };
}
