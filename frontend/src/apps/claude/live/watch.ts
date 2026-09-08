// THE STANDING LIVE WATCH (D415, inventory 05 §D, T:17550-17745). Pure TS: the
// triggers are wired to `window`/`document` by `start()`, everything else is
// injected, so the whole policy is testable with fake timers.
//
// PR1 shipped the WINDOW around opening a chat — `adoptLiveRun`'s ~3 s of laps,
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
// So the question is asked on a timer for the life of the page, and asked of
// the SERVER — the one party that knows a run is alive — rather than inferred
// from what this frame happens to have sent.
//
// A TIMER ALONE IS A BLIND WINDOW AND A SHORT TURN FITS INSIDE IT (Akshil,
// 2026-08-21, two tabs on one chat): a 10 s interval found runs that had
// already FINISHED, so `resumeRun` took its done-branch and repaired the
// transcript in silence. The fix is to ask SOONER, not to arm harder — three
// triggers, cheapest and fastest first:
//
//   * A POKE FROM THE TAB THAT STARTED IT. `stampChatActivity` already writes
//     `localStorage` at the START of every turn this app runs, for the shell's
//     tasks store — and a `storage` event fires in every OTHER document on this
//     origin and never in the writer, which is exactly the shape needed. The
//     two-tab case costs no polling at all and lands in milliseconds.
//   * BECOMING VISIBLE / FOCUSED. A tab that was in the background while
//     something ran should not wait out an interval to catch up.
//   * THE INTERVAL, 5 s and SKIPPED while the document is hidden. It is the
//     backstop for the one starter that pokes nothing this page can hear — a
//     run spawned by the server (a scheduled message, the canvases workspace).
//
// Hidden tabs give up the interval deliberately: chrome nobody is looking at is
// worth nothing, the storage poke still reaches them, and becoming visible laps
// at once.
import type { TranscriptStat } from "../protocol/types";

/** T:17601. */
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
  /** `adoptLiveRun(id, {laps: 1, quiet: true})`. */
  adopt(sessionId: string): Promise<void>;
  /** The watermark the visible render is good to — `ChatState.transcript`,
   *  written by `history` (T:17663-17665). Compared by IDENTITY after the await,
   *  so a render that landed while we asked discards our answer. */
  transcriptMark(): TranscriptStat | null;
  /** `ChatState.ownRunEndedAt`, in EPOCH SECONDS. */
  ownRunEndedAt(): number;
  liveness(path: string): Promise<LivenessProbe>;
  /** `loadHistory(id, {refresh: true})` — no skeleton, scroll kept. */
  refreshHistory(sessionId: string): Promise<void>;
  /** The working line for a turn happening somewhere else. */
  setExternalWorking(on: boolean): void;
  /** The `storage` key `stampChatActivity` writes (T:16435). */
  activityKey: string;
  /** Injectable for tests. */
  setInterval?: (fn: () => void, ms: number) => unknown;
  clearInterval?: (handle: unknown) => void;
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

export interface LiveWatch {
  /** One lap. Every trigger goes through this, whose busy seat makes a burst of
   *  them (a poke landing on an interval landing on a focus) ONE lookup rather
   *  than three (T:17604). */
  tick(): Promise<void>;
  /** Arm the interval and the three event triggers. Returns the disarm. */
  start(): () => void;
}

export function createLiveWatch(deps: LiveWatchDeps): LiveWatch {
  let busy = false; // one lap at a time; a lap can outlive its interval
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
   */
  async function followTranscript(sessionId: string): Promise<void> {
    const mark = deps.transcriptMark();
    if (!mark || !mark.path) return; // no render to compare against yet
    let probe: LivenessProbe;
    try {
      probe = await deps.liveness(mark.path);
    } catch {
      return; // a failed stat leaves the transcript exactly as it rendered
    }
    // Everything can have changed across the await, and each of these outranks a
    // stale answer: the reader left, or a real run attached and owns the chrome.
    if (stopped || deps.sessionId() !== sessionId || deps.busy()) return;
    if (deps.transcriptMark() !== mark) return; // a render landed while we asked
    const verdict = followDecision(mark, probe, deps.ownRunEndedAt());
    // Refresh FIRST, then the line: the history render replaces the log
    // wholesale, so a line added before it would be swept by the very render it
    // announces. The refresh writes the new watermark itself, from its own
    // pre-read stat — never from this probe, which is older than the rows the
    // refetch brings.
    if (verdict.refresh) await deps.refreshHistory(sessionId);
    const running = deps.hasActiveRun ? deps.hasActiveRun() : deps.busy();
    if (!stopped && deps.sessionId() === sessionId && !running) {
      deps.setExternalWorking(verdict.running);
    }
  }

  async function tick(): Promise<void> {
    if (stopped || busy || deps.busy()) return;
    const sessionId = deps.sessionId();
    // Only with a session on screen. Without one there is no conversation to
    // adopt a turn INTO — the landing page's own send makes the session — and
    // `live_run` with no session matches on the target alone, which would drag a
    // run belonging to some other chat about this folder onto this screen.
    if (!sessionId) return;
    busy = true;
    try {
      await deps.adopt(sessionId);
      // RUN DIRS FIRST, ALWAYS. A run this app spawned streams token by token
      // and owns the chrome; the transcript is the coarser, blinder fallback and
      // only speaks for the turns no run dir can account for. So it is asked
      // strictly after, and only if nothing attached.
      if (!stopped && !deps.busy() && deps.sessionId() === sessionId) {
        await followTranscript(sessionId);
      }
    } finally {
      busy = false;
    }
  }

  return {
    tick,
    start() {
      stopped = false;
      const every = deps.setInterval || ((fn, ms) => setInterval(fn, ms));
      const clear = deps.clearInterval || ((h) => clearInterval(h as never));
      const hidden = deps.isHidden || (() => typeof document !== "undefined" && document.hidden);
      const handle = every(() => {
        if (!hidden()) void tick();
      }, LIVE_WATCH_MS);
      const onStorage = (ev: StorageEvent) => {
        // KEYED, because every other `storage` event on this origin (the queue
        // mirror, the shell's own stores) is not news about a turn.
        if (ev.key === deps.activityKey) void tick();
      };
      const onVisible = () => {
        if (!hidden()) void tick();
      };
      const onFocus = () => void tick();
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
      doc?.addEventListener("visibilitychange", onVisible as (ev: never) => void);
      win?.addEventListener("focus", onFocus as (ev: never) => void);
      return () => {
        stopped = true;
        clear(handle);
        win?.removeEventListener("storage", onStorage as (ev: never) => void);
        doc?.removeEventListener("visibilitychange", onVisible as (ev: never) => void);
        win?.removeEventListener("focus", onFocus as (ev: never) => void);
      };
    },
  };
}
