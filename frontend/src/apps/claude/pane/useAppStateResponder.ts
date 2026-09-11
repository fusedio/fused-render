// THE PULL CHANNEL: answer the agent's `app_state` reads (T:15731-15840).
//
// agent.py parks each `app_state` tool call as a file and hands the unanswered
// ones to every poll (`ChatState.appState`); this window is the only thing that
// can see the app, so it is the only thing that can answer. No card and no
// confirmation: it is a read of the user's own screen, for the agent they are
// already talking to, and the tool call is BLOCKED until it is answered. The
// one-line note in the transcript is the whole transparency story, and it is the
// right size for it.
//
// The poll replays every unanswered request on each 400 ms tick, so the dedupe
// sets below are what stop one tool call from being answered — and noted — a
// dozen times.
import { useEffect, useRef } from "react";
import type { AppStateRow } from "../protocol/types";
import type { AppStateWatcher } from "./appState";

/**
 * Polls a request may see an empty snapshot before we stop waiting and answer
 * with `pull()`'s explicit sentence instead. The poll loop is 400 ms, so this is
 * ~2 s — comfortably longer than a live-reload of the left pane, and far shorter
 * than the tool's own timeout (minutes), which would be the alternative backstop
 * and a much worse answer for the model (T:15744).
 */
export const APP_STATE_NULL_POLLS = 5;

/**
 * Cap on all three memos. One entry per `app_state` call for the page's lifetime
 * is bounded in practice and unbounded in principle; dropping the oldest is safe
 * because agent.py only ever lists UNANSWERED requests, so a forgotten id cannot
 * come back — and if it somehow did, the on-disk latch makes a second answer a
 * no-op (T:15757).
 */
export const APP_STATE_MEMO_MAX = 200;

/** Insertion-ordered eviction: `Set`/`Map` iteration order is insertion order,
 *  so the first key is the oldest (T:15758). */
export function appStateTrim(memo: Set<string> | Map<string, number>): void {
  while (memo.size > APP_STATE_MEMO_MAX) {
    const oldest = memo.keys().next().value;
    if (oldest === undefined) return;
    memo.delete(oldest);
  }
}

export interface AppStateResponderOptions {
  /** `ChatState.appState` — the UNANSWERED requests, replayed every poll. */
  rows: readonly AppStateRow[];
  /** The pane's snapshot source. `null` while the pane has not mounted; the
   *  rows are then left unclaimed so a later mount still answers them. */
  watcher: AppStateWatcher | null;
  /**
   * `ChatController.answerAppState(id, block)`. `block` is the snapshot as a
   * JSON STRING, not a nested object: params cross into Python string-shaped and
   * nothing on that side reads inside the snapshot. Never the bare `null` a
   * snapshot can be — agent.py reads a non-dict as a permanent failure
   * (T:15816-15822).
   */
  answerAppState: (id: string, block: string) => Promise<{ error?: string; retry?: boolean } | void>;
  /** The transcript's one-line note, once per REQUEST (T:15806). */
  onNote?: (text: string) => void;
}

/**
 * Answers each row exactly once, with T's retry semantics:
 *
 *   * a NULL snapshot leaves the id UNCLAIMED for up to `APP_STATE_NULL_POLLS`
 *     polls (almost always the pane mid-reload — and an edit is exactly what
 *     triggers a reload, so this is the common case right after the model edits
 *     something and then asks what happened), then falls through and answers
 *     with `pull()`'s sentence rather than spinning to the tool timeout;
 *   * the id is CLAIMED before the await, because polls overlap;
 *   * a THROW un-claims it so the next poll retries — the tool call is still
 *     blocked, and the tool's own timeout is the backstop;
 *   * a RESOLVED error un-claims it only when agent.py flags it `retry`: a write
 *     that did not reach disk can be helped by another go, an unknown run or
 *     request never will, and retrying that every 400 ms until the run ends is
 *     worse than letting the timeout settle it.
 */
export function useAppStateResponder(opts: AppStateResponderOptions): void {
  const answered = useRef<Set<string>>(new Set());
  // Separate from `answered` because that one is RELEASED when an attempt fails
  // so the next poll retries — and a retry must not append a second "read app
  // state" line for one read (T:15741).
  const noted = useRef<Set<string>>(new Set());
  const nullPolls = useRef<Map<string, number>>(new Map());

  // Held in a ref so the effect below depends only on the rows: the callbacks
  // are rebuilt every render by their owners, and re-running the answer loop on
  // a render would double-answer nothing (the sets prevent that) but would spend
  // a snapshot walk per keystroke.
  const live = useRef(opts);
  live.current = opts;

  /**
   * WHEN THE ANSWER LOOP RUNS. `T` has no effect graph: `answerAppState` is
   * called from the 400 ms poll itself, so it gets a go at every unanswered row
   * on every tick (T:15758-15869). Two things were lost by keying this on the
   * row IDS alone, and both are silent hangs of a BLOCKED tool call:
   *
   *   * a pane that mounts AFTER the first request lands never answered it. The
   *     ids had not changed, so the effect never re-ran, and the request sat
   *     unanswered until the model happened to ask again. Hence `ready`: the
   *     readiness flip is itself a reason to run.
   *   * `APP_STATE_NULL_POLLS` never counted past 1. A null snapshot leaves the
   *     id unclaimed for the NEXT poll to retry — but there was no next run, so
   *     the fall-through that answers with `pull()`'s explicit sentence was
   *     unreachable. Hence `pollsSeen` in the key: the controller re-stamps it
   *     every poll (`surfaceAppState`), so each tick is a fresh key and the
   *     count advances exactly as it does in T.
   */
  const rowsKey = opts.rows.map((r) => `${r.id}:${r.pollsSeen ?? 0}`).join(",");
  const ready = !!opts.watcher;

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const { rows, watcher, answerAppState, onNote } = live.current;
      if (!watcher) return;
      for (const req of rows) {
        if (cancelled) return;
        if (!req || !req.id || answered.current.has(req.id)) continue;
        // Taken HERE, not at send time: the whole point of the pull channel is
        // that the pushed snapshot has gone stale (T:15786).
        const state = watcher.snapshot();
        if (!state) {
          const seen = (nullPolls.current.get(req.id) ?? 0) + 1;
          nullPolls.current.set(req.id, seen);
          appStateTrim(nullPolls.current);
          if (seen <= APP_STATE_NULL_POLLS) continue;
          // Still nothing: this is not a reload, it is a pane with no app to
          // read. Fall through and answer, so the model is told that instead of
          // waiting out the tool timeout for the same news.
        }
        answered.current.add(req.id);
        appStateTrim(answered.current);
        // Once per REQUEST, not once per attempt.
        if (!noted.current.has(req.id)) {
          noted.current.add(req.id);
          appStateTrim(noted.current);
          onNote?.(req.reason ? "read app state — " + req.reason : "read app state");
        }
        let res: { error?: string; retry?: boolean } | void;
        try {
          res = await answerAppState(req.id, JSON.stringify(state ?? watcher.pull()));
        } catch {
          answered.current.delete(req.id);
          continue;
        }
        if (res && res.error && res.retry) answered.current.delete(req.id);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [rowsKey, ready]);
}
