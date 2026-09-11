// THE AWAY TIMER behind the "While you were away" fold (Claude Code's "Session
// recap", `awaySummaryEnabled`; .claude-design/session-recap.md).
//
// Two events, not one, and each covers what the other cannot:
//   * `visibilitychange` is the tab being hidden — another tab, another Space,
//     the machine asleep. It does NOT fire when the window merely loses focus,
//     which is the ordinary way a reader steps away from a chat they left open;
//   * window `blur`/`focus` is that case — and it fires for things that are not
//     leaving at all (a devtools click, an OS dialog), which is exactly why the
//     threshold below is a MINUTE rather than a moment.
//
// So both are bound and they share one clock: whichever says "gone" first
// starts it, whichever says "back" first reads it. Coming back with the clock
// unset is a non-event — a `focus` with no `blur` before it happens on plenty
// of boots — and it is silently ignored rather than treated as a zero-length
// absence.
//
// AND IT IS ONE PAGE'S RETURN, not one mount's. The gates below are mostly
// about this chat; three of them are about the PAGE, because `focus` is heard
// by every chat the shell has mounted and not by the one the reader is looking
// at: the host has to opt in (ClaudeChat's `recap`), the mount has to be on
// screen (`recapRootVisible`), and the first to fire takes the return
// (`SAME_RETURN_MS`). Without them one return bought seven model calls, six for
// folds inside cards nobody opens.
//
// FETCHED ON RETURN, never on the way out. Claude Code generates on blur
// because it can fork its own warm cache; ours is a fresh model call on the
// server, so paying for a reader who never comes back is a cost with no reader
// at the end of it (design, "Data model (ours)").
import { useCallback, useEffect, useRef, useState } from "react";

import { fetchRecap as defaultFetchRecap, type Recap } from "../protocol/recap";

/** How long away is long enough (Claude Code's own threshold). A CONSTANT, and
 *  the hook takes an override, because there is no test-knob convention in this
 *  app — no URL param, no window global — and inventing one for a single
 *  feature would be a second way to configure the chat. */
export const AWAY_MS = 60_000;

/** Two failures and it stops asking FOR THIS MOUNT. A recap is a courtesy; a
 *  chat that re-attempts a failing model call on every return is a chat that
 *  spends the reader's money on nothing (Claude Code: "failing repeatedly this
 *  turn"). */
export const MAX_FAILURES = 2;

/**
 * ONE RECAP PER RETURN, PER PAGE — the gate no per-mount check can be.
 *
 * Every other gate in this hook is about THIS chat; this one is about the page.
 * A shell can hold more than one primary chat at once (the explorer's content
 * pane and its `?_side=claude` sidebar are two mounts of the same conversation
 * shape), and every mounted hook hears the same window `focus`. Two hooks
 * answering one return is two ~12s model calls for one reader, and the reader
 * only ever looks at one of them.
 *
 * So the fire is stamped MODULE-WIDE and the second hook inside the window
 * stands down. Deliberately short: this is a burst guard for a single `focus`,
 * not a rate limit — a reader who genuinely leaves and returns twice is two
 * absences a minute apart and gets a recap for each.
 */
export const SAME_RETURN_MS = 2_000;

/** The stamp itself. Module state because the thing being deduplicated is the
 *  page's return, which no mount owns. */
let lastFiredAt = 0;

/** Module state outlives a `create()`/`unmount()` pair, so a suite that runs
 *  several harnesses on one fake clock has to clear it between them. */
export function resetRecapFiredForTests(): void {
  lastFiredAt = 0;
}

/** As much of an element as the visibility check needs — a shape rather than
 *  `HTMLElement`, so a test can hand over a plain object the way the `view` and
 *  `doc` seams already let it. */
export interface RecapRoot {
  offsetParent?: unknown;
  getBoundingClientRect(): { width: number; height: number };
}

/**
 * Is this chat ON SCREEN? `offsetParent` is null for anything inside a
 * `display:none` subtree — which is how a hidden tab, a held-off preview pane
 * and a collapsed sidebar all present themselves — and a zero box catches the
 * rest (a pane laid out at 0 width, a mount that has not been given room yet).
 *
 * No `IntersectionObserver`: this is asked ONCE, on a return, about an element
 * that is already laid out. An observer would be a subscription per mount for
 * an answer we want twice an hour.
 */
export function recapRootVisible(el: RecapRoot | null | undefined): boolean {
  if (!el) return false;
  if (el.offsetParent === null) return false;
  const box = el.getBoundingClientRect();
  return box.width > 0 && box.height > 0;
}

/** The two halves of `window` this hook binds — injected in tests, exactly as
 *  `useDismissOnWindow` does it. */
export interface AwayRecapEnv {
  view?: Pick<Window, "addEventListener" | "removeEventListener"> | null;
  doc?: {
    hidden?: boolean;
    addEventListener(type: string, fn: () => void): void;
    removeEventListener(type: string, fn: () => void): void;
  } | null;
  /** The clock, for a suite that does not want to wait a minute. */
  now?: () => number;
}

export interface AwayRecapOptions extends AwayRecapEnv {
  /** The chat's target and session — the same pair `fetchHistory` sends. */
  file: string | null;
  sessionId: string | null;
  /** Where the transcript stands (`protocol/recap.recapAnchor`). `null` when
   *  there is nothing worth summarising. */
  forUuid: string | null;
  /** A turn is in flight. */
  running: boolean;
  /** Whether the reader has typed something they have not sent. A FUNCTION,
   *  read at the moment of the check: the composer's text is its own React
   *  state and the host reads it off the textarea ref, so there is nothing to
   *  put in a dependency array and nothing that would re-render this hook. */
  hasDraft: () => boolean;
  /** `prefs.chat.recap`. */
  enabled: boolean;
  /** Override for `AWAY_MS`; tests pass 0. */
  awayMs?: number;
  /**
   * The chat's ROOT element, as a getter read at the moment of the check.
   *
   * A chat in a hidden tab or a held-off pane is still mounted and still hears
   * `focus`; a recap it fetches is a model call for a fold nobody can see. The
   * getter, not the element: a ref is null on the render that binds the
   * listeners and only the CHECK happens late enough to have it.
   *
   * Omitted (tests that are not about visibility) means "do not ask"; present
   * and returning `null` means NOT visible, because an unattached ref is a
   * mount that is not drawing anything.
   */
  root?: () => RecapRoot | null;
  /** The read, for tests. */
  fetchRecap?: typeof defaultFetchRecap;
}

export interface AwayRecapResult {
  /** The fold to draw, or `null`. Already matched to the CURRENT position —
   *  see the auto-dismiss note below. */
  recap: Recap | null;
  /** The ×. Dismisses for this position and never asks again for it. */
  dismiss: () => void;
}

/**
 * Watch for the reader leaving and coming back; fetch a recap when they do.
 *
 * AUTO-DISMISS IS A READ, NOT A WRITE. The fold is held keyed to the position
 * it describes and only rendered while that is still the position — so sending
 * the next message (which makes a new user turn, hence a new anchor) and
 * starting a new turn both take it away without a listener, an effect or a
 * race. A recap that arrives after the reader has already sent is dropped on
 * the same test.
 */
export function useAwayRecap(opts: AwayRecapOptions): AwayRecapResult {
  // `awayMs` is deliberately NOT destructured: every gate below reads through
  // `latest` so the listeners can be bound once, and a second copy taken here
  // would be the one thing in the check that could go stale.
  const { forUuid, view, doc, now = Date.now } = opts;

  const [recap, setRecap] = useState<Recap | null>(null);
  /** Positions this mount has already answered for — shown, dismissed, or told
   *  there was nothing to show. One set for all three, because the question
   *  "should I ask about this position" has one answer. */
  const spent = useRef(new Set<string>());
  const failures = useRef(0);
  /** When the reader went away, or `null` while they are here. */
  const awayAt = useRef<number | null>(null);
  /** A read in flight, so a focus storm cannot start a second one. */
  const inflight = useRef(false);

  /** Everything the check needs, read at the moment it runs rather than
   *  captured: the listeners are bound ONCE (see the effect) so that a blur is
   *  never missed in the frame between two renders. */
  const latest = useRef(opts);
  latest.current = opts;

  const dismiss = useCallback(() => {
    setRecap((current) => {
      if (current) spent.current.add(current.forUuid);
      return null;
    });
  }, []);

  useEffect(() => {
    const win = view ?? (typeof window !== "undefined" ? window : null);
    const document_ = doc ?? (typeof globalThis.document !== "undefined" ? globalThis.document : null);
    if (!win && !document_) return;

    let live = true;
    const gone = () => {
      // FIRST one wins: `visibilitychange` and `blur` both fire for a tab
      // switch, and the second of them must not restart the clock the first
      // started — that is a full minute of absence rounded down to nothing.
      if (awayAt.current === null) awayAt.current = now();
    };

    const back = () => {
      const at = awayAt.current;
      awayAt.current = null;
      if (!live || at === null) return;
      if (now() - at < (latest.current.awayMs ?? AWAY_MS)) return;
      const o = latest.current;
      // EVERY GATE, in the order that costs least to ask. `spent` is why a
      // reader who dismissed the fold and stepped away twice more does not get
      // it back; `hasDraft` is why one is never dropped under a half-typed
      // message the reader came back to finish.
      if (!o.enabled) return;
      if (!o.file || !o.sessionId || !o.forUuid) return;
      if (o.running) return;
      if (spent.current.has(o.forUuid)) return;
      if (failures.current >= MAX_FAILURES) return;
      if (inflight.current) return;
      if (o.hasDraft()) return;
      // THE TWO PAGE-WIDE GATES, last because they are the only ones that
      // either cost a layout read or reach outside this mount.
      if (o.root && !recapRootVisible(o.root())) return;
      // A clock that has moved BACKWARDS (a test's fake clock, an OS time
      // correction) is not "within the window" — it is no information at all,
      // and refusing on it would silence the feature until the clock caught up.
      const sinceLast = now() - lastFiredAt;
      if (sinceLast >= 0 && sinceLast < SAME_RETURN_MS) return;
      lastFiredAt = now();

      const asked = o.forUuid;
      inflight.current = true;
      const read = o.fetchRecap ?? defaultFetchRecap;
      void read(o.file, o.sessionId, asked)
        .then((answer) => {
          inflight.current = false;
          if (!live) return;
          // Spent EITHER WAY: an empty answer is the server saying there is
          // nothing to show for this position, and asking again would buy the
          // same nothing at the same price.
          spent.current.add(asked);
          const text = (answer && answer.text ? answer.text : "").trim();
          if (!text) return;
          // The world may have moved while the model was thinking (3-25s): a
          // message sent, a turn started. Re-asked here rather than assumed,
          // and the render gate below re-asks once more.
          const fresh = latest.current;
          if (fresh.forUuid !== asked || fresh.running || fresh.hasDraft()) return;
          setRecap({ text, forUuid: asked });
        })
        .catch(() => {
          inflight.current = false;
          if (!live) return;
          // NOT spent — a transport failure says nothing about this position,
          // and the next return may well succeed. The failure COUNT is what
          // stops it going on forever.
          failures.current += 1;
        });
    };

    const onVisibility = () => {
      if (document_ && document_.hidden) gone();
      else back();
    };
    document_?.addEventListener("visibilitychange", onVisibility);
    win?.addEventListener("blur", gone);
    win?.addEventListener("focus", back);
    return () => {
      live = false;
      document_?.removeEventListener("visibilitychange", onVisibility);
      win?.removeEventListener("blur", gone);
      win?.removeEventListener("focus", back);
    };
    // Bound once per env, never per render: a listener torn down and re-added
    // mid-absence would forget the clock it was keeping.
  }, [view, doc, now]);

  // THE AUTO-DISMISS, and the late-answer drop, in one expression: the fold is
  // only ever drawn against the position it was written about.
  const showing = recap && recap.forUuid === forUuid && !opts.running ? recap : null;
  return { recap: showing, dismiss };
}

export default useAwayRecap;
