// THE STATE MACHINE (inventory §D; T:7597-7760, 8401-8420, 8479, 15948-15982).
//
// ONE DOOR IN AND OUT. T extracted `annSetMode` for exactly this reason —
// Escape leaves the mode too, and a second copy of the transition would be a
// second chance for the bar's words to disagree with `annOn`. Here the mode is
// one value rather than three booleans, so the illegal combinations (armed and
// recording and settling) cannot be reached at all.
//
// Arming has SIX entry points (the strip's Comment seat, the mic, the boot
// default, the `annmode` param, the hosted re-arm when a target appears, and
// the app menu's exit action), which is why `capable()` is enforced HERE rather
// than at each of them: a check at each is a check the seventh one forgets.
import { isSendable, type AnnStore } from "./store";
import type { AnnMode, AnnRecorder } from "./types";

export interface AnnModeDeps {
  store: AnnStore;
  /** T:6122 `annCapable` — is there anything to annotate right now. */
  capable(): boolean;
  /** The voice recorder (`ann/rec*`), or null where audio is unavailable. */
  recorder?: () => AnnRecorder | null;
  /** T:6875 — every transition ends in a repaint. */
  render(): void;
  /** T:6867 `annNavLock` — `.chat-root.annlock`, `#back.disabled`, and the
   *  composer block state. A MODE HOLDS THE READER ON THIS CHAT (Akshil,
   *  2026-09-07): the notes are about the app beside it, and ← Chats or a recent
   *  row would carry them off to a chat they are not about. */
  onLock(locked: boolean): void;
  /** T:7654 — the picker follows the MODE: arming slides it in, disarming puts
   *  it away, whichever door armed the mode. A cross-origin target has no choice
   *  to offer, so it stays away there (D355). */
  onToolVisible(show: boolean): void;
  /** The open composer: `annDone` commits its words FIRST — Done means "send
   *  what I said" — and a disarm closes it. */
  composerOpen(): boolean;
  /** What is TYPED in the open composer (T:7714 reads `annTa.value.trim()`).
   *  Part of the Done condition rather than only of `commitDraft`'s own guard,
   *  so the two cannot drift into disagreeing about what an empty card means.
   *  Optional: a caller that cannot see the text gets the old "commit and let
   *  the commit decide" behaviour. */
  composerText?(): string;
  commitDraft(): Promise<void> | void;
  closeComposer(): void;
  /** Nothing to hide is the same outcome as hidden (T:7688). */
  hideHl(): void;
  /** T:8487 `annAutoSubmit` — the notes' ONE door to Claude. */
  autoSubmit(): void;
  /**
   * T:7720 — a live run is NOT a reason to hold the notes back (Akshil,
   * 2026-09-04): an annotation-only send goes to the running claude as a
   * follow-up, the same way words typed during a run go. Only the `sending`
   * window before a run has an id leaves them pending for the next message.
   * `activeRun || !sending`.
   */
  canSend(): boolean;
  xo(): boolean;
  /** T:7670 — arming over a cross-origin target is the natural moment to raise
   *  the ONE tab-share prompt the screenshots need: the click that armed the mode
   *  IS the user activation `getDisplayMedia` requires. Only when the native
   *  screen shot is unavailable — with it, there is no prompt to raise. */
  onXOArm?(): void;
  now?(): number;
}

export interface AnnModeMachine {
  mode(): AnnMode;
  /** T:6543 `annOn` — armed, comment or recording. */
  armed(): boolean;
  /** T:7597 `annArmEpoch`. */
  epoch(): number;
  /** T:6864 `annNavLocked`. */
  locked(): boolean;
  /** T:6874 — re-assert the lock from the paint, as `renderAnn` does. Derived
   *  state, so the repaint is the place that cannot forget it. */
  relock(): void;
  subscribe(cb: (mode: AnnMode) => void): () => void;

  /** T:7599 `annSetMode`. */
  set(on: boolean): void;
  /** THE TEARDOWN'S DISARM — T:8797's bare `annOn = false`, and nothing else.
   *
   *  `set(false)` is the wrong door on the way down: its disarm branch ends a
   *  live recording with `end()`, which goes on into `transcribe` → `deliver` →
   *  the automatic send, i.e. a transcription and a message into a chat that is
   *  already gone (the exact defect the recorder's `abandon()` seam was added to
   *  stop). It also writes the `annmode` param, closes the composer and repaints
   *  — all during `pagehide`/unmount, where T does none of it.
   *
   *  So: stop the mic by ABANDONING it (the file is still kept), flip `on`, and
   *  re-derive the lock and the published mode. Nothing else. */
  forceOff(): void;
  /** T:7743 `annBootMode` — OFF unless the URL says exactly "1". */
  bootFromParam(): void;
  /** T:7710 `annDone`. */
  done(): Promise<void>;
  /** T:8419 `annDiscard` — one dispatcher for the bar's trash, by mode. */
  discard(): void;
  /** T:8401 `annNotesDiscard`. */
  notesDiscard(): void;
  /** T:15959's ann branches, once the viewer and the composer have had their
   *  turn (`escapeAction`). */
  escape(): void;
  /** T:8940 — arriving at the narrow CHAT view disarms (not at boot). */
  arriveNarrowChat(): void;
  /** T:8479 — the hosted poll found no target any more. */
  targetGone(): void;

  /** The recorder's own reporting seam. `null` clears the settle. */
  setPhase(phase: "settling" | "transcribing" | null): void;
  /** T:6863 `annBusyHold` — the settle's claim on the nav lock. A DISARM
   *  releases it (Esc during Transcribing…): the reader has said they are
   *  leaving, and a transcription that never answers must not hold them here
   *  for ever. */
  setBusyHold(held: boolean): void;
  busyHold(): boolean;
}

/**
 * DOES THE WALKTHROUGH OWN THE MODE RIGHT NOW — every state but `off` and
 * `comment`, which is to say the mic's start window, the recording, Stopping…
 * and Transcribing… (Bugbot, PR #1074).
 *
 * One spelling for the four readers of that fact: the seats it draws inert
 * (`seatsAria`), the two doors that refuse (`done`, `notesDiscard`), and the
 * send-time question `ann/store.isSendableNow` asks — are this mark's words
 * still coming. They were three separate `recording()` tests and each one was a
 * chance to disagree about the settle.
 */
export function walkthroughOwns(mode: AnnMode): boolean {
  return mode !== "off" && mode !== "comment";
}

export function createAnnMode(deps: AnnModeDeps): AnnModeMachine {
  const now = deps.now ?? (() => Date.now());
  const rec = () => (deps.recorder ? deps.recorder() : null);
  let on = false;
  let phase: "settling" | "transcribing" | null = null;
  let hold = false;
  let armEpoch = 0;
  let doneBusy = false;
  const subs = new Set<(m: AnnMode) => void>();

  /**
   * THE SETTLE, READ OFF THE RECORDER AND ONLY THEN OFF THE ECHO
   * (Bugbot, PR #1074).
   *
   * `phase` is a COPY: `ClaudeChat`'s effect writes it from `recSnap` after the
   * recorder's own flag has already moved. So for the width of one React commit
   * — `recording()` down, the effect not yet run — every direct caller of
   * `mode()` read `comment`, and in that window the bar's trash took the typed
   * round's exit: `notesDiscard` saw `phase === null`, DELETED the marks and
   * never asked the recorder to stop, so the transcription it left running
   * could still land and auto-send words with nothing to anchor them to. The
   * same window let ✓ Done through (`walkthroughOwns`) and dropped the nav
   * lock's hold.
   *
   * `settling()` is the recorder's own synchronous answer (T:8114 — stopping,
   * transcribing or discarding in flight), so it closes the window in the one
   * place all four readers already come through. The echo still WINS when it is
   * there, because it is the finer of the two: it can say `transcribing` where
   * `settling()` only says "busy". Conversely a `phase` left standing after the
   * recorder has gone quiet keeps its word until the effect clears it — the lag
   * in that direction is a face held one frame too long, not a destructive door
   * opened one frame too early.
   */
  const settlePhase = (): "settling" | "transcribing" | null => {
    if (phase) return phase;
    const r = rec();
    return r && r.settling() ? "settling" : null;
  };

  const mode = (): AnnMode => {
    const r = rec();
    if (r && r.recording()) return "recording";
    const p = settlePhase();
    if (p) return p;
    return on ? "comment" : "off";
  };
  const locked = () => on || (hold && settlePhase() !== null);
  let lastAnnounced: AnnMode | null = null;
  const announce = () => {
    const m = mode();
    if (m === lastAnnounced) return;
    lastAnnounced = m;
    for (const cb of subs) cb(m);
  };

  function set(want: boolean): void {
    // A disarm, whoever asks and whether or not it lands below, releases the
    // settle's hold on the chat: a claim the MODE made, and the mode is going.
    if (!want) hold = false;
    // NOTHING TO POINT AT CAN NEVER BE ARMED. Returns BEFORE the param write,
    // deliberately: `annmode=1` left in a URL from a folder that used to have an
    // app entry is IGNORED, not rewritten (T:7615).
    if (!deps.capable()) {
      on = false;
      const r = rec();
      // The target this recording was pointed at just went away — release the
      // mic rather than leave it running against a document nobody can see.
      if (r && r.recording()) r.end();
      // AND HAND THE CHAT BACK. This return used to skip the lock, so a target
      // that vanished while the mode was armed left `.chat-root.annlock` on and
      // ← Chats disabled with no way to undo either: the one door out of the
      // mode was the door this branch takes.
      deps.onLock(locked());
      announce();
      return;
    }
    // Every ARM gets a number, so a disarm decided long ago (a stop's, after an
    // await) can tell "the mode I armed" from "a mode the user re-armed while I
    // was transcribing" and leave the second one alone (Bugbot, #644).
    if (want) {
      armEpoch += 1;
      deps.store.startRound(now());
    }
    on = want;
    // THE ONE PARAM WRITER — and it does not write when the URL already MEANS
    // this: the boot default calls through here with `on` derived from that very
    // reading, so on a freshly loaded entry the write was a semantic no-op that
    // cost a HISTORY ENTRY anyway (expanding the preview to full screen took TWO
    // presses of Back to undo). Writing "1" over an ABSENT param is not a no-op
    // and still writes: arming is a state change the user asked for.
    const r0 = rec();
    deps.store.syncModeParam(r0 && r0.recording() ? "2" : on ? "1" : "0");
    if (!on || deps.xo()) deps.onToolVisible(false);
    else deps.onToolVisible(true);
    if (on && deps.xo()) deps.onXOArm?.();
    deps.render();
    if (!on) {
      deps.hideHl();
      deps.closeComposer();
      // Leaving the mode ends a live recording too — the click handler that feeds
      // it is about to stop firing, so a mic left running would just be capturing
      // audio nothing can ever anchor.
      const r = rec();
      if (r && r.recording()) r.end();
    }
    deps.onLock(locked());
    announce();
  }

  /** T:8401 `annNotesDiscard` — Comment mode's discard: the ROUND's unsent notes
   *  are deleted (an open draft with them) and the mode goes away. Earlier
   *  rounds' notes are not this round's to throw; sent ones are already
   *  Claude's. */
  function notesDiscard(): void {
    // Not while a walkthrough SETTLES either (Bugbot, PR #1008): the recorder's
    // flag is already down through Stopping…/Transcribing…, and the marks are
    // the recording's, waiting for words — not a typed round to throw away.
    const r = rec();
    // `settlePhase`, NOT `phase`: the recorder's flag is already down through
    // Stopping…/Transcribing… and the echo arrives a commit later, so reading
    // the copy here left one frame in which this door threw the RECORDING's
    // marks away as a typed round's (Bugbot, PR #1074).
    if ((r && r.recording()) || !on || settlePhase() !== null) return;
    deps.closeComposer();
    deps.store.discardRound();
    set(false);
  }

  /** T:8419 — one dispatcher for the bar's trash, whichever mode is on. */
  function discard(): void {
    const r = rec();
    if (r && r.recording()) {
      r.discard();
      return;
    }
    notesDiscard();
  }

  /** Esc in a typed Comment mode is CANCEL (Akshil, 2026-09-06: "I press escape,
   *  it doesn't discard it") — this round's unsent notes go, not just the mode;
   *  the bar's trash and this key are the same exit. A live walkthrough keeps its
   *  own Esc (stop and KEEP, through `set(false)`), and through
   *  Stopping…/Transcribing… the marks are the recording's to settle, so those
   *  two still only LEAVE the mode — which releases the nav lock. */
  function escape(): void {
    const r = rec();
    if ((r && r.recording()) || settlePhase() !== null) {
      set(false);
      return;
    }
    notesDiscard();
  }

  /** See the interface. The one disarm that never transcribes. */
  function forceOff(): void {
    // A KEEP-ONLY STOP, which is T:8796-8812's own ending: the audio stops and
    // no transcription is asked for, because "this document is going away and
    // there is no panel to show one in". `abandon()` keeps the file.
    // UNCONDITIONAL: `abandon()` reads the recorder's own state and returns for
    // anything that is not live, so a `recording()` test here would only be a
    // second, staler copy of that question — and the settle is a state this
    // door must reach too (`phase` goes below).
    rec()?.abandon();
    on = false;
    phase = null;
    hold = false;
    deps.onLock(locked());
    announce();
  }

  return {
    mode,
    armed: () => on,
    epoch: () => armEpoch,
    locked,
    // T:6874 — `renderAnn` ENDS in `annNavLock()`. The lock is derived, so every
    // repaint re-asserts it rather than trusting whichever transition last set
    // it; that is the second half of the fix above, and it is what makes a
    // stuck lock unreachable rather than merely fixed on one path.
    relock: () => deps.onLock(locked()),
    subscribe(cb) {
      subs.add(cb);
      return () => {
        subs.delete(cb);
      };
    },

    set,
    forceOff,
    bootFromParam() {
      // "2" — the page was reloaded MID-WALKTHROUGH — ENDS the mode rather than
      // becoming Comment (Akshil, 2026-09-07): a walkthrough cannot survive a
      // reload (the handle, the marks and the clock live in this page's memory),
      // and a mic opened from a boot rather than a click is exactly the start
      // that races the hosted pane's arrival. The audio was already stopped and
      // KEPT on `pagehide`, so nothing spoken is lost.
      set(deps.store.modeParam() === "1");
    },
    async done() {
      // NOT THE WALKTHROUGH'S TO FINISH (Bugbot, PR #1074). Done is Comment
      // mode's exit: through the recording, the start window and the settle the
      // marks belong to the RECORDING and are waiting on its words, and a Done
      // landing there auto-submitted them WORDLESS and disarmed the mode
      // mid-transcription. The same refusal `notesDiscard` already makes, for
      // the same reason — stated here and not only in the seats, because the
      // injected bar and a stale render both reach this door directly.
      if (walkthroughOwns(mode())) return;
      // ONE AT A TIME (Bugbot, PR #664): the commit's await could span a second
      // Done click, which would see no open composer, read the just-saved note as
      // merely pending, and send it twice.
      if (doneBusy) return;
      doneBusy = true;
      try {
        // AN EMPTY CARD IS NOT A NOTE (T:7714). `commitDraft` re-checks — it is
        // the one writer and has to — but the condition is stated here as well
        // so this file and T's read the same test.
        const typed = deps.composerText ? deps.composerText().trim() !== "" : true;
        if (deps.composerOpen() && typed) await deps.commitDraft();
        // THE SAME RULE THE SEND PATH AND THE BUTTON READ (`isSendable`): a
        // WORDLESS note stamped by a walkthrough click is a message too, and
        // asking only for `content` here made ✓ Done disarm a round of them
        // and leave them sitting unsent — while the Send affordance beside it
        // said they were sendable (Bugbot, PR #1074).
        const pending = deps.store.list().some((a) => isSendable(a) && !a.sent);
        if (pending && deps.canSend()) deps.autoSubmit();
        set(false);
      } finally {
        doneBusy = false;
      }
    },
    discard,
    notesDiscard,
    escape,
    arriveNarrowChat() {
      // The media rules hide the annotate toggle in the chat-only view because
      // there is no frame to point at, and leaving the mode armed behind a hidden
      // toggle would keep the frame's capture-phase click swallower live over a
      // document the user cannot see, in a state its own view cannot undo.
      if (on || settlePhase() !== null) set(false);
    },
    targetGone() {
      // `set` refuses (`capable()` is false now) AFTER setting `on` false, which
      // is exactly the "ignored, not rewritten" posture a stale `annmode` gets
      // everywhere else. The repaint is the caller's to ask for, since that early
      // return skips it — the chips stay, the pins went with the document.
      set(false);
    },

    setPhase(next) {
      phase = next;
      deps.onLock(locked());
      announce();
    },
    setBusyHold(held) {
      hold = held;
      deps.onLock(locked());
    },
    busyHold: () => hold,
  };
}

/** T:15950 `escapeAction` — WHO CLAIMS ESCAPE, in order, and the key has no
 *  destructive branch left at all (Akshil, 2026-09-03): a press of habit used to
 *  lose a whole turn. Exported so the chat's own Esc handler and the one wired
 *  into the framed document read the same table. */
export function escapeAction(
  viewerOpen: boolean,
  composerOpen: boolean,
  annotating: boolean,
): "close-viewer" | "close-composer" | "exit-annotate" | "" {
  if (viewerOpen) return "close-viewer";
  if (composerOpen) return "close-composer";
  if (annotating) return "exit-annotate";
  return "";
}
