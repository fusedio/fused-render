// The poller behind the heads-up card (server: claude_agent/headsup.py).
//
// The SERVER decides when a side check is due; this hook only keeps asking
// while a turn runs, so a closed tab means no checks. Polling stops as soon as
// the run is over and nothing is still being worked out. One ask on mount
// restores a note the server still holds after a page refresh.
import { useCallback, useEffect, useRef, useState } from "react";

import { getJson, postJson } from "@platform/lib/api";

import { isInterruptMark } from "../protocol/wire";

export const RUNNING_MS = 10_000;
export const CHECKING_MS = 5_000;
/** One more look this long after a run ends: the turn-end check starts then. */
export const SETTLE_MS = 1_500;
/** The card goes away on its own after this many of the user's own messages. */
export const CLEAR_AFTER_USER_TURNS = 2;

export interface HeadsUpNote {
  learn: string;
  tag: "You should know" | "Heads up";
  title: string;
  bullets: string[];
}

/** How the user answered a card; the server's later checks learn from it. */
export type HeadsUpAnswer = "got_it" | "dismiss" | "chat";

export type HeadsUpState = "disabled" | "idle" | "checking" | "note";

export interface HeadsUpResponse {
  state: HeadsUpState;
  id?: string;
  note?: HeadsUpNote;
}

export interface HeadsUpTiming {
  running: number;
  checking: number;
  settle: number;
}

const DEFAULT_TIMING: HeadsUpTiming = { running: RUNNING_MS, checking: CHECKING_MS, settle: SETTLE_MS };

/** How long to wait before the next ask, or null to stop. `ended` is "the run
 *  just went from running to stopped", which earns exactly one settle poll. */
export function nextPollDelay(
  s: { running: boolean; state: HeadsUpState; ended: boolean },
  t: HeadsUpTiming = DEFAULT_TIMING,
): number | null {
  if (s.state === "disabled") return null;
  if (s.ended) return t.settle;
  if (s.state === "checking") return t.checking;
  return s.running ? t.running : null;
}

export interface UseHeadsUpArgs {
  runId: string;
  running: boolean;
  enabled: boolean;
  /** How many messages the user has sent in this conversation so far. */
  userTurns: number;
  /** The page's own send path (latch, outbox, queue, optimistic bubble). */
  controller: { sendFollowUp(t: string): Promise<void>; sendMessage(t: string): Promise<void> };
  /** Test seam: shorter intervals. */
  timing?: HeadsUpTiming;
}

export interface HeadsUpApi {
  note: HeadsUpNote | null;
  id: string;
  dismiss(): Promise<void>;
  gotIt(): Promise<void>;
  askInChat(): Promise<void>;
}

/**
 * User rows the run actually received: the CLI's interrupt marker is a user row
 * nobody typed, and a row with `pending` is still queued / unsent (the run
 * controller drops `pending` when the real send adopts the row).
 */
export function countUserTurns(turns: ReadonlyArray<{ role: string; text?: string; pending?: unknown }>): number {
  return turns.reduce((n, t) => (t.role === "user" && !t.pending && !isInterruptMark(t.text) ? n + 1 : n), 0);
}

export function useHeadsUp({ runId, running, enabled, userTurns, controller, timing }: UseHeadsUpArgs): HeadsUpApi {
  const [shown, setShown] = useState<{ runId: string; id: string; note: HeadsUpNote; baseTurns: number } | null>(null);
  const lastState = useRef<HeadsUpState>("idle");
  const wasRunning = useRef(running);
  const turnsRef = useRef(userTurns);
  turnsRef.current = userTurns;
  const dismissed = useRef(new Set<string>());
  const t = timing ?? DEFAULT_TIMING;
  const { running: runMs, checking: checkMs, settle: settleMs } = t;

  // A note belongs to one conversation: never carry it (or its poll state) over.
  useEffect(() => {
    lastState.current = "idle";
    setShown(null);
  }, [runId]);

  useEffect(() => {
    const ended = wasRunning.current && !running;
    wasRunning.current = running;
    if (!enabled || !runId) return;
    // A fresh enable (or run) polls again, even after a `disabled` answer.
    lastState.current = "idle";
    const clock: HeadsUpTiming = { running: runMs, checking: checkMs, settle: settleMs };
    let dead = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      let state: HeadsUpState = lastState.current;
      try {
        const r = await getJson<HeadsUpResponse>(
          `/api/claude-sessions/headsup?run_id=${encodeURIComponent(runId)}`,
        );
        state = r.state;
        if (!dead && r.state === "note" && r.id && r.note && !dismissed.current.has(r.id)) {
          const note = r.note;
          const id = r.id;
          setShown((cur) => (cur?.id === id && cur.runId === runId ? cur : { runId, id, note, baseTurns: turnsRef.current }));
        }
        if (!dead && !(r.state === "note" && r.id && !dismissed.current.has(r.id))) {
          // The server no longer holds the note we show (its turn ended, so it
          // withdrew it): drop the card. The dismissed set is left alone.
          const held = r.state === "note" ? r.id : undefined;
          setShown((cur) => (cur?.runId === runId && cur.id !== held ? null : cur));
        }
      } catch {
        // A failed poll is "nothing new"; the next tick asks again.
      }
      if (dead) return;
      lastState.current = state;
      const delay = nextPollDelay({ running, state, ended: false }, clock);
      if (delay != null) timer = setTimeout(() => void tick(), delay);
    };
    // The server holds a note across a refresh, so every mount / run / enable
    // asks once right away, even when idle. A run that just stopped keeps its
    // settle delay (the turn-end check starts then).
    if (ended) timer = setTimeout(() => void tick(), clock.settle);
    else void tick();
    return () => {
      dead = true;
      if (timer) clearTimeout(timer);
    };
  }, [enabled, runId, running, runMs, checkMs, settleMs]);

  const visible =
    enabled &&
    shown && shown.runId === runId && userTurns - shown.baseTurns < CLEAR_AFTER_USER_TURNS && !dismissed.current.has(shown.id)
      ? shown
      : null;

  const hide = useCallback(async (answer: HeadsUpAnswer) => {
    const cur = visible;
    if (!cur) return;
    dismissed.current.add(cur.id);
    setShown(null);
    try {
      await postJson("/api/claude-sessions/headsup/dismiss", { run_id: runId, id: cur.id, answer });
    } catch {
      // Hidden locally either way; the server note just lingers unseen.
    }
  }, [visible, runId]);

  const askInChat = useCallback(async () => {
    const cur = visible;
    if (!cur) return;
    const text = `Tell me more about this: ${cur.note.title}\n\n${cur.note.bullets.join("\n")}`;
    try {
      if (running) await controller.sendFollowUp(text);
      else await controller.sendMessage(text);
    } catch (err) {
      // The card stays so the user can retry; callers fire this with `void`.
      console.warn("heads-up: could not send the follow-up", err);
      return;
    }
    await hide("chat");
  }, [visible, running, controller, hide]);

  return { note: visible?.note ?? null, id: visible?.id ?? "", dismiss: () => hide("dismiss"), gotIt: () => hide("got_it"), askInChat };
}
