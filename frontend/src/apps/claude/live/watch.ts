// THE STANDING LIVE WATCH (D415, inventory 05 §D, T:17550-17745). Pure TS: the
// triggers are wired to `window`/`document` by `start()`, everything else is
// injected, so the whole policy is testable with a scripted subscription.
//
// PR1 shipped the WINDOW around opening a chat — `adoptLiveRun`'s ~3 s probe,
// which is the whole of the page's answer to "was something already running
// when I got here?". It covers arriving late and covers nothing after: a
// session that becomes busy while this chat sits open lit no indicator and
// streamed nothing, whoever started the turn. The reported case is the
// harness's own — a background shell finishes, the run wakes for another turn —
// and it is not the only one: the same chat open in a second pane, a `claude`
// the user resumed from a terminal, a spawn from the canvases workspace. None
// of them go through `sendMessage`, which is the only path that arms the
// running chrome.
//
// So the question is asked for the life of the page, and asked of the SERVER
// — the one party that knows a run is alive — rather than inferred from what
// this frame happens to have sent. It is a SUBSCRIPTION now (D3): `claude.live
// {file, session_id, path}` pushes `{live: <live_run>, liveness: <the
// transcript's stat>}` whenever either moves, and every frame is one lap.
//
// A BLIND WINDOW LETS A SHORT TURN FIT INSIDE IT (Akshil, 2026-08-21, two tabs
// on one chat): a 10 s interval found runs that had already FINISHED, so
// `resumeRun` took its done-branch and repaired the transcript in silence. The
// fix was to ask SOONER, not to arm harder — and the bus is the soonest:
//
//   * A POKE FROM THE TAB THAT STARTED IT. `stampChatActivity` already writes
//     `localStorage` at the START of every turn this app runs, for the shell's
//     tasks store — and a `storage` event fires in every OTHER document on this
//     origin and never in the writer, which is exactly the shape needed. It
//     asks the bus for a fresh snapshot (`resyncTopic`) — an event, not a
//     timer — so the two-tab case lands in milliseconds.
//   * BECOMING VISIBLE / FOCUSED asks the same way. (The client already drops
//     this `hidden_ok` subscription while the document is hidden and
//     resubscribes on visible, so the snapshot that answers IS the catch-up.)
//   * THE SERVER'S OWN CADENCE — 400 ms for ~3.5 s after a subscribe, then
//     every 5 s (`LIVE_WATCH_MS`), sending a frame only when the answer moved.
//     It is the backstop for the one starter that pokes nothing this page can
//     hear — a run spawned by the server (a scheduled message, the canvases
//     workspace).
//
// Hidden tabs give up the frames deliberately: chrome nobody is looking at is
// worth nothing, and becoming visible resubscribes at once.
//
// WHICH KEY. The host hands this file the session; the target (`file`) and the
// transcript's `path` come from the controller that owns them
// (`chatTargetFor`, unless `deps.file` names the target), and the subscription
// RE-KEYS when either moves — the watermark lands with the history, after the
// watch is armed, and its `path` is what turns the liveness half on.
import { TASKS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
import { canonicalKey, resyncTopic, subscribeTopic, type SubscribeLike } from "@platform/lib/events";
import { chatTargetFor, onChatTargets, type ClaudeLiveBody } from "../protocol/run-controller";
import type { TranscriptStat } from "../protocol/types";

/** T:17601 — the standing watch's backstop cadence, which is now the SERVER's
 *  (`topics.py ClaudeLiveTopic.poll_interval_s`) once its fast probe window is
 *  over. Nothing on the page arms it. */
export const LIVE_WATCH_MS = 5000;

/** `GET /api/claude-sessions/liveness` (T:17691-17694). */
export interface LivenessProbe {
  exists: boolean;
  mtime: number;
  size: number;
  running: boolean;
}

export interface FollowVerdict {
  refresh: boolean;
  running: boolean;
}

/**
 * PURE, so the rule can be read (and tested) without a server (T:17675-17683):
 * given what the last render was good up to and what the file looks like now,
 * does the page re-render, and is somebody mid-turn in there?
 *
 * The PAIR rather than mtime alone, because a coarse filesystem clock can put
 * two appends in one tick where the size cannot repeat.
 *
 * `ownEnd` is the guard that keeps this page from reporting its OWN turn back to
 * itself, in EPOCH SECONDS to match `probe.mtime`: the transcript is freshest
 * right after a run this frame streamed, and `session_liveness` will still call
 * the session running for a few seconds afterwards. A probe no newer than our
 * own last turn is our own echo.
 */
export function followDecision(
  mark: TranscriptStat | null | undefined,
  probe: LivenessProbe | null | undefined,
  ownEnd: number,
): FollowVerdict {
  if (!mark || !mark.path || !probe || !probe.exists) {
    return { refresh: false, running: false };
  }
  return {
    refresh: probe.mtime !== mark.mtime || probe.size !== mark.size,
    running: !!probe.running && probe.mtime > (ownEnd || 0),
  };
}

export interface LiveWatchDeps {
  /** The session on screen, `""` when there is none. Re-read after every await:
   *  it is this port's stand-in for T's `logGen` — the reader going home clears
   *  it, and nothing on this page is then ours to redraw. */
  sessionId(): string;
  /** `activeRun || sending` — a run this app spawned owns the chrome. */
  busy(): boolean;
  /**
   * `!!activeRun` ALONE, which is a narrower question than `busy()` and the
   * only one the external line's OFF edge may ask (T:17709 gates on
   * `logGen === gen && !activeRun`). `sending` is a send in flight, not a turn
   * on screen: leave it in this gate and a line that says somebody else is
   * working cannot be taken down for the whole duration of the user's own send,
   * so it sits under their turn until the run ends.
   *
   * Optional, defaulting to `busy` — a caller that cannot tell the two apart
   * keeps the old behaviour rather than losing the guard.
   */
  hasActiveRun?(): boolean;
  /** `adoptLiveRun(id, {laps: 1, quiet: true})`. Asked only when the frame
   *  names a live run (or when the frame cannot say — no target known); the
   *  controller then attaches through its own one-frame probe. */
  adopt(sessionId: string): Promise<void>;
  /** The watermark the visible render is good to — `ChatState.transcript`,
   *  written by `history` (T:17663-17665). Compared by IDENTITY after the await,
   *  so a render that landed while we asked discards our answer. Its `path` is
   *  the subscription's. */
  transcriptMark(): TranscriptStat | null;
  /** `ChatState.ownRunEndedAt`, in EPOCH SECONDS. */
  ownRunEndedAt(): number;
  /** The one-off stat for a lap with no frame behind it (`tick()` called
   *  directly). A frame carries its own `liveness` and this is not asked. */
  liveness(path: string): Promise<LivenessProbe>;
  /** `loadHistory(id, {refresh: true})` — no skeleton, scroll kept. */
  refreshHistory(sessionId: string): Promise<void>;
  /** The working line for a turn happening somewhere else. */
  setExternalWorking(on: boolean): void;
  /** The `storage` key `stampChatActivity` writes (T:16435). */
  activityKey: string;
  /**
   * THE SAME NEWS, THROWN OVER A WALL INSIDE ONE DOCUMENT (P4-06 / C G-2).
   *
   * T's cards and Peek were separate iframe DOCUMENTS, so a turn started in one
   * stamped `localStorage` and every sibling's `storage` listener fired within
   * a millisecond (T:17578-17584, 17739-17741). Native renders the cards wall,
   * Peek and the split pane in ONE document — and `storage` never fires in the
   * document that wrote it, so the sibling mounts lost that poke entirely and a
   * run started in tile A was adopted by tile B only on the 5 s interval.
   *
   * `run-controller.ts`'s `noteChatActivity` already dispatches
   * `TASKS_CHANGED_EVENT` on the window at both turn boundaries — the shell's
   * tasks store is its other listener — so this listens for it beside
   * `storage`, and asks the bus for a fresh snapshot on it. The WRITER does not
   * re-adopt its own turn: the existing `busy()` gate is what stops that,
   * exactly as it does for every other frame.
   */
  localEvent?: string | null;
  /** The chat's target, for the subscription's `file`. Optional: absent, the
   *  controller that owns this session says (`chatTargetFor`). */
  file?(): string;
  /** The bus seam (`subscribeTopic` by default) — the suites hand in a
   *  scripted one. */
  subscribe?: SubscribeLike;
  /** Ask for a fresh snapshot now (`resyncTopic` by default). */
  resync?: (topic: string, params: Record<string, unknown>) => void;
  /** Hear the controllers' targets move (`onChatTargets` by default). */
  onTargets?: (cb: () => void) => () => void;
  /** Injectable for tests. */
  isHidden?: () => boolean;
  /** The two event targets, injectable because the suites have no real ones:
   *  the DOM shim's `window` and `document` are no-op stubs, so a test that has
   *  to prove the storage key is FILTERED (and that the disarm really removes
   *  the listeners) has to hand its own in. */
  win?: EventTargetLike | null;
  doc?: EventTargetLike | null;
}

/** The two methods this file uses of `window` / `document`. */
export interface EventTargetLike {
  addEventListener(type: string, fn: (ev: never) => void, capture?: boolean): void;
  removeEventListener(type: string, fn: (ev: never) => void, capture?: boolean): void;
}

/** One `claude.live` frame as a lap reads it. `live` is `undefined` when the
 *  subscription could not name the target — the frame then says nothing about
 *  run dirs, and the lap asks `adopt` as it always did. */
export interface LiveFrame {
  live?: ClaudeLiveBody["live"];
  liveness: LivenessProbe | null;
}

export interface LiveWatch {
  /** One lap — on a frame (what `start()` does with every one), or with no
   *  frame behind it, when it asks `adopt` and `deps.liveness` itself. Its
   *  busy seat makes a burst of them ONE lookup rather than three (T:17604). */
  tick(frame?: LiveFrame | null): Promise<void>;
  /** Subscribe and arm the event triggers. Returns the disarm. */
  start(): () => void;
}

export function createLiveWatch(deps: LiveWatchDeps): LiveWatch {
  let busy = false; // one lap at a time; a lap can outlive the next frame
  let stopped = false;

  /**
   * FOLLOWING A CONVERSATION THIS APP IS NOT DRIVING (T:17685-17709).
   *
   * The gap D415 recorded: an interactive `claude` in a terminal, resuming the
   * very session the chat is open on. It creates NO run dir, so `live_run` is
   * blind to it by construction and the chat showed nothing at all until the
   * reader hit reload — at which point the missing turns appeared correctly,
   * because the reload path was never the problem. So the RELOAD is what is
   * automated, and deliberately nothing more: the server stats the transcript,
   * a move re-renders through `history` (the page does not decide what is new,
   * it re-asks what the conversation IS), and granularity is a whole turn.
   *
   * The stat is the frame's `liveness` when there is a frame; a frame without
   * one (no `path` in its key yet, or the stat failed server-side) is a lap
   * with nothing to compare.
   */
  async function followTranscript(
    sessionId: string,
    ownEndBefore: number,
    frame: LiveFrame | null,
  ): Promise<void> {
    const mark = deps.transcriptMark();
    if (!mark || !mark.path) return; // no render to compare against yet
    let probe: LivenessProbe;
    if (frame) {
      if (!frame.liveness) return;
      probe = frame.liveness;
    } else {
      try {
        probe = await deps.liveness(mark.path);
      } catch {
        return; // a failed stat leaves the transcript exactly as it rendered
      }
    }
    // Everything can have changed across the await, and each of these outranks a
    // stale answer: the reader left, or a real run attached and owns the chrome.
    if (stopped || deps.sessionId() !== sessionId || deps.busy()) return;
    // ...OR ONE ENDED WHILE WE ASKED. `busy()` is false the instant a turn
    // finishes, and the rows it wrote are exactly what this probe is about to
    // read back as somebody else's news (Bugbot 3975677791). The stamp is the
    // fact this cannot ask any other way — see `tick`.
    if (deps.ownRunEndedAt() !== ownEndBefore) return;
    if (deps.transcriptMark() !== mark) return; // a render landed while we asked
    const verdict = followDecision(mark, probe, deps.ownRunEndedAt());
    // Refresh FIRST, then the line: the history render replaces the log
    // wholesale, so a line added before it would be swept by the very render it
    // announces. The refresh writes the new watermark itself, from its own
    // pre-read stat — never from this probe, which is older than the rows the
    // refetch brings.
    if (verdict.refresh) await deps.refreshHistory(sessionId);
    // Re-read after the await too (Bugbot 3977975835): a run of this frame's
    // that began and ended during the refresh has stamped `ownRunEndedAt`, and
    // the verdict above was reached about a transcript that predates its rows —
    // "running outside this app" over this page's own reply.
    if (deps.ownRunEndedAt() !== ownEndBefore) return;
    const running = deps.hasActiveRun ? deps.hasActiveRun() : deps.busy();
    if (!stopped && deps.sessionId() === sessionId && !running) {
      deps.setExternalWorking(verdict.running);
    }
  }

  async function tick(frame: LiveFrame | null = null): Promise<void> {
    if (stopped || busy || deps.busy()) return;
    const sessionId = deps.sessionId();
    // Only with a session on screen. Without one there is no conversation to
    // adopt a turn INTO — the landing page's own send makes the session — and
    // `live_run` with no session matches on the target alone, which would drag a
    // run belonging to some other chat about this folder onto this screen.
    if (!sessionId) return;
    busy = true;
    try {
      /**
       * WHAT "NOTHING ATTACHED" ACTUALLY MEANS (Bugbot 3975677791).
       *
       * `!busy()` was standing in for it and is a narrower fact: it is false
       * only while a turn is IN FLIGHT. An `adopt` that attached and ran to
       * completion inside the await below — and `pollLoop` ending under it, and
       * the done-repair road that reconciles a run that finished while the frame
       * was away — all leave the controller idle with the transcript's tail
       * freshly written by THIS page. `followTranscript` then ran against a
       * watermark predating those rows, refreshed the very log we had just
       * streamed, and (the visible half) raised the "somebody else is working"
       * line off our own echo.
       *
       * The stamp is the one signal that carries it: `ownRunEndedAt` is written
       * at every turn boundary this frame owns — `pollLoop`'s finally and, as of
       * this change, the done-repair — so a lap across which it MOVED is a lap
       * that took a run, whether or not one is still running now. Both halves
       * are asked: `busy()` for the turn still going, the stamp for the one that
       * has just ended.
       */
      const ownEndBefore = deps.ownRunEndedAt();
      // A frame that KNOWS nothing is live saves the controller its probe: the
      // answer `adopt` would fetch is the one in hand. Anything else — a run
      // named, or a frame that could not name the target — asks.
      const nothingLive = !!frame && frame.live !== undefined && !(frame.live && frame.live.run_id);
      if (!nothingLive) await deps.adopt(sessionId);
      // RUN DIRS FIRST, ALWAYS. A run this app spawned streams token by token
      // and owns the chrome; the transcript is the coarser, blinder fallback and
      // only speaks for the turns no run dir can account for. So it is asked
      // strictly after, and only if nothing attached.
      const took = deps.busy() || deps.ownRunEndedAt() !== ownEndBefore;
      if (!stopped && !took && deps.sessionId() === sessionId) {
        await followTranscript(sessionId, ownEndBefore, frame);
      }
    } finally {
      busy = false;
    }
  }

  return {
    tick,
    start() {
      stopped = false;
      const subscribe = deps.subscribe || subscribeTopic;
      const resync =
        deps.resync ||
        ((topic: string, params: Record<string, unknown>) => {
          resyncTopic(topic, params);
        });
      const hidden = deps.isHidden || (() => typeof document !== "undefined" && document.hidden);

      /** The subscription's key, its params, its unsubscribe, and whether it
       *  carried the target (a frame from one that did not cannot speak for
       *  run dirs). */
      let key = "";
      let params: Record<string, unknown> | null = null;
      let fileKnown = false;
      let off: (() => void) | null = null;

      /**
       * THE NEWEST FRAME, LAPPED ONE AT A TIME. A frame that lands while a lap
       * is running is held, not dropped: the bus sends only what MOVED, so a
       * dropped frame is news nobody will repeat. Latest wins — each frame is
       * the whole answer.
       */
      let pending: LiveFrame | null = null;
      let draining = false;
      const drain = async () => {
        if (draining) return;
        draining = true;
        try {
          while (pending && !stopped) {
            const frame = pending;
            pending = null;
            await tick(frame);
          }
        } finally {
          draining = false;
        }
      };

      const paramsNow = (): Record<string, unknown> | null => {
        const sessionId = deps.sessionId();
        if (!sessionId) return null;
        const owner = chatTargetFor(sessionId);
        const file = deps.file ? deps.file() : owner ? owner.file : "";
        const mark = deps.transcriptMark();
        const path = (mark && mark.path) || "";
        return { file, session_id: sessionId, ...(path ? { path } : {}) };
      };
      /** Subscribe for the key as it stands now, if it moved. */
      const rekey = () => {
        if (stopped) return;
        const next = paramsNow();
        const nextKey = next ? canonicalKey(next) : "";
        if (nextKey === key) return;
        off?.();
        off = null;
        key = nextKey;
        params = next;
        fileKnown = !!(next && next.file);
        if (!next) return;
        const carriesFile = fileKnown;
        let mine = true;
        const unsubscribe = subscribe(
          "claude.live",
          next,
          (raw, _delta, meta) => {
            // A refusal is a lap with nothing in it: the transcript stays
            // exactly as it rendered, as for a failed stat.
            const snap = raw as ClaudeLiveBody | null;
            if (stopped || !mine || !snap || (meta && meta.error)) return;
            pending = {
              ...(carriesFile ? { live: snap.live ?? null } : {}),
              liveness: snap.liveness ?? null,
            };
            // The key may have moved since (a watermark landed): follow it
            // before lapping, so the next frame is the right one. Off the
            // client's call stack: a replayed snapshot is delivered inside
            // `subscribe`, which can itself be inside a controller's emit.
            void Promise.resolve().then(() => {
              rekey();
              return drain();
            });
          },
        );
        off = () => {
          mine = false;
          unsubscribe();
        };
      };
      /** A trigger: re-key if the target moved, else ask for a fresh frame. */
      const poke = () => {
        if (stopped) return;
        const before = key;
        rekey();
        // A new key's subscribe IS the fresh snapshot; an unchanged one asks.
        if (key && key === before && params) resync("claude.live", params);
      };

      const onStorage = (ev: StorageEvent) => {
        // KEYED, because every other `storage` event on this origin (the queue
        // mirror, the shell's own stores) is not news about a turn.
        if (ev.key === deps.activityKey) poke();
      };
      /** The in-document twin of `onStorage` — see `deps.localEvent`. Unkeyed
       *  because the event NAME is the key: unlike `storage`, nothing else is
       *  delivered on it. */
      const onLocal = () => poke();
      const localEvent =
        deps.localEvent !== undefined ? deps.localEvent : TASKS_CHANGED_EVENT;
      const onVisible = () => {
        if (!hidden()) poke();
      };
      const onFocus = () => poke();
      const win =
        deps.win !== undefined
          ? deps.win
          : typeof window !== "undefined"
            ? (window as unknown as EventTargetLike)
            : null;
      const doc =
        deps.doc !== undefined
          ? deps.doc
          : typeof document !== "undefined"
            ? (document as unknown as EventTargetLike)
            : null;
      win?.addEventListener("storage", onStorage as (ev: never) => void);
      if (localEvent) win?.addEventListener(localEvent, onLocal as (ev: never) => void);
      doc?.addEventListener("visibilitychange", onVisible as (ev: never) => void);
      win?.addEventListener("focus", onFocus as (ev: never) => void);
      // The controller says when the target or the watermark moved — from
      // inside its emit, so the re-key runs after it.
      const offTargets = (deps.onTargets || onChatTargets)(() => {
        void Promise.resolve().then(rekey);
      });
      rekey();
      return () => {
        stopped = true;
        offTargets();
        off?.();
        off = null;
        key = "";
        params = null;
        pending = null;
        win?.removeEventListener("storage", onStorage as (ev: never) => void);
        if (localEvent) win?.removeEventListener(localEvent, onLocal as (ev: never) => void);
        doc?.removeEventListener("visibilitychange", onVisible as (ev: never) => void);
        win?.removeEventListener("focus", onFocus as (ev: never) => void);
      };
    },
  };
}
