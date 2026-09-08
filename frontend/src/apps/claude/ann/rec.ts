// THE VOICE WALKTHROUGH, AS STATE — `annRecBegin` / `annRecMark` /
// `annRecMarkPoint` / `annRecAssign` / `annRecEnd` / `annRecDiscard`
// (T:7858, 7953, 7979, 8097, 8132, 8337) with every DOM write lifted out.
//
// The gesture: press the mic, talk while you click. Every click becomes an
// instant WORDLESS note stamped with its second (`t`); the stop transcribes the
// recording and hands each note the words that were spoken NEAREST to it, with
// everything said before the first click becoming the message's own prompt.
// Record, talk, stop — and it sends itself.
//
// Nothing here touches the document, on purpose: the 205 lines of `annRecEnd`
// are half status-seat repainting and half a state machine whose bugs (Bugbot
// PR #644/#665: a discard racing a stop, a second session started inside a
// settle, a stale ender repainting a live seat) are invisible in a screenshot.
// So the machine lives here with its deps injected, `ann/RecControls.tsx` draws
// it, and the tests drive it with a fake recorder and a fake clock.
import type { AudioOptions, AudioRecording } from "@platform/lib/capture-audio";
import { stampOf } from "./geometry";

import type { Transcript, TranscriptWord } from "./transcribe";

/** The subset of `Annotation` (ann/types.ts) this module reads or writes.
 *  Structural on purpose: the recorder is handed a PORT to the store rather
 *  than importing it, so `Annotation` satisfies this without either module
 *  knowing about the other.
 *
 *  `spoken` is the WORDS — what the template calls `content`, and what the
 *  transcript fills in. NOT `text`: an anchor carries a `text` of its own (the
 *  80-char element digest, `wire-target.ts`), the mark writers below spread the
 *  anchor over this object, and one name for two facts meant the digest
 *  overwrote the empty words and was then read out as the note's content —
 *  every recorded element mark lost its digest and showed the app's own text as
 *  if the user had typed it. */
export interface RecAnnotation {
  id: string;
  kind?: string;
  /** Seconds into the recording, to a tenth (`annRecStamp`, T:7944). Absent on
   *  a typed note, which is why every read of it here is guarded. */
  t?: number;
  spoken: string;
  createdAt?: number;
  sent?: boolean;
}

/** What a click contributes to a mark — the anchor the click handler already
 *  built for the typed flow, handed here instead of to the composer
 *  (T:7950-7952). Opaque to this module: element paths, `iu/iv`, `tag`, `shot`
 *  are the geometry module's business. `Record<string, unknown>` and not `any`
 *  precisely so it stays opaque. */
export type RecAnchor = Record<string, unknown>;

/** The store, as the recorder needs it. Every method is a synchronous write
 *  that ends in the store's own save — `annSave()`, in T. */
export interface RecNotesPort {
  /** `annotations.push(c); annSave()` (T:7956-7959). */
  add(note: RecAnnotation): void;
  get(id: string): RecAnnotation | undefined;
  /** `annRecAssign`'s write: each mark's `text` set from the transcript, once
   *  (T:8121-8124). */
  assign(texts: Array<{ id: string; text: string }>): void;
  /** The marks die with a discarded recording — a note that was only ever going
   *  to carry the recording's words is nothing without them (T:8388-8392). */
  remove(ids: string[]): void;
}

/** The comment mode, as the recorder needs it (ann/mode.ts). */
export interface RecModePort {
  /** `annOn` — a comment layer is armed. */
  isArmed(): boolean;
  /** `annCapable()` — there is a target to annotate at all (T:7861). */
  capable(): boolean;
  /** `annSetMode(true)`: recording needs the same click handler the typed mode
   *  uses, so arm it if the reader went straight for the mic (T:7895-7897). */
  arm(): void;
  /** `annSetMode(false)`: ending a walkthrough puts the whole mode away
   *  (Akshil, 2026-08-19 — T:8190-8198). */
  disarm(): void;
  /** `annArmEpoch` — WHICH arming a disarm belongs to. A reader who re-armed
   *  during a settle owns a NEW arming this stale disarm must not close
   *  (Bugbot PR #644/#665, T:8190-8198). */
  epoch(): number;
  /** `annModeSync()` — the URL now says "2" (T:7903). Optional: a caller
   *  without URL params passes nothing. */
  syncParam?(): void;
}

/** Where the state machine can be. T's four flags (`annRecStarting`,
 *  `annRecOn`, `annCta.busy` + the label's tense) named as one value, which is
 *  the whole reason the settle bugs were possible: "stopping" and "recording"
 *  were both `annRecOn === false` with a class to tell them apart. */
export type RecState =
  | "off"
  | "starting"
  | "recording"
  | "stopping"
  | "transcribing"
  | "discarding";

export interface RecSnapshot {
  state: RecState;
  /** What `#annreclbl` says: "" at rest, the clock while recording, else the
   *  settle's tense (T:7797-7803, 8181, 8243, 8366). */
  status: string;
  /** How many clicks this recording has stamped (`annRecIds.length`). */
  marks: number;
  /** Seconds elapsed, unrounded — the clock's own input. 0 when not recording. */
  seconds: number;
  /** `annCta.busy`: a settle is in flight. The nav lock and both seats' inert
   *  faces hang off this (T:6889). */
  busy: boolean;
}

export interface RecorderDeps {
  /** `fused.capture.audio` (T:7886) — `platform/lib/capture-audio`'s
   *  `captureAudio`, or a fake. */
  capture(opts: AudioOptions): Promise<AudioRecording>;
  /** `annWarmTranscriber()` (T:7814). Fire-and-forget by contract. */
  warm(): void;
  /** `fused.ai.transcribe({path, words: true})` (T:8262) — ann/transcribe's
   *  `transcribe`, curried down to the one argument this module has. */
  transcribe(path: string): Promise<Transcript>;
  notes: RecNotesPort;
  mode: RecModePort;
  /** `annPrefillComposer(intro)` + the gated `annAutoSubmit()` (T:8296-8299).
   *  The recorder decides WHETHER (words landed, on a note or as intro); the
   *  composer decides whether a live run makes it a follow-up. */
  deliver(intro: string, spoke: boolean): void;
  /** `crypto.randomUUID()` (T:7954). */
  newId?(): string;
  /** `performance.now()` for the clock, `Date.now()` for `createdAt` — two
   *  different clocks in T, kept separate here so a test can freeze the first
   *  without lying about the second. */
  now?(): number;
  wallNow?(): number;
  setInterval?(fn: () => void, ms: number): unknown;
  clearInterval?(id: unknown): void;
  /** `alert("Cannot record — " + …)` (T:7893). */
  alert?(message: string): void;
  warn?(message: string, detail?: unknown): void;
}

/** The clock tick. 250 ms, not 1000: the seconds digit has to turn ON the
 *  second, and a 1 s interval drifts visibly against a start that happened
 *  mid-second (T:7940). */
export const REC_TICK_MS = 250;

/** Below this the recording is treated as EMPTY — a failed stop, a zero-byte
 *  file, or a stop that landed on the start's own beat. SILENCE is not this
 *  case: silence reaches the transcriber and comes back wordless (T:8199-8203). */
export const REC_MIN_SECONDS = 0.4;

/** m:ss, no library (`annRecClock`, T:7790). Not mm:ss — a walkthrough is
 *  minutes at most, and a leading zero on the minutes reads as a duration
 *  field rather than a running clock. */
export function recClock(seconds: number): string {
  return (
    Math.floor(seconds / 60) + ":" + String(Math.floor(seconds % 60)).padStart(2, "0")
  );
}

/** The seat's name and tooltip for each state — the settle names the seat for
 *  its STATUS and `recIdleName` gives the resting name back (2026-09-06,
 *  T:8422-8425). Verbatim from T:7929-7930, 8165-8166, 8236-8237, 8362-8363. */
export interface SeatName {
  label: string;
  title: string;
}

export function recIdleName(): SeatName {
  return {
    label: "Annotate with a spoken walkthrough",
    title:
      "Annotate with a spoken walkthrough — talk while you click, and each click becomes a note",
  };
}

export function recSeatName(state: RecState): SeatName {
  switch (state) {
    case "recording":
      return {
        label: "Stop the recording",
        title: "Recording — click to stop · Esc also stops it",
      };
    case "stopping":
      return { label: "Stopping the recording", title: "Stopping the recording…" };
    case "transcribing":
      return {
        label: "Transcribing the walkthrough",
        title:
          "Transcribing the walkthrough — the notes send themselves when the words land",
      };
    case "discarding":
      return { label: "Discarding the recording", title: "Discarding the recording…" };
    default:
      return recIdleName();
  }
}

/** The Comment seat is INERT while a walkthrough runs or settles — one mode at
 *  a time, and the stop is the bar's ■ and the mic, never a neighbouring seat
 *  (Bugbot PR #1022, T:7936-7937, 8171-8172). */
export const COMMENT_SEAT_WHILE_RECORDING: SeatName = {
  label: "Comment — unavailable while recording",
  title: "Unavailable while a walkthrough records",
};

export const COMMENT_SEAT_WHILE_SETTLING: SeatName = {
  label: "Comment — unavailable while the recording settles",
  title: "Unavailable while the recording settles",
};

function errText(err: unknown): string {
  const e = err as { message?: string; name?: string } | null;
  return (e && (e.message || e.name)) || String(err);
}

const collapse = (s: string) => s.replace(/\s+/g, " ").trim();

/**
 * EACH PIECE OF SPEECH GOES TO EXACTLY ONE CLICK (`annRecAssign`, T:8097).
 *
 * Nearest-by-start-time, not a fixed lead window. OpenPointer's "each click
 * owns LEAD seconds before it until the next click" looked right but
 * double-counts by construction: click i's window reaches forward to click
 * i+1's timestamp and click i+1's reaches the same LEAD seconds backward from
 * there, so anything said in that shared stretch landed in BOTH notes the
 * moment two clicks were less than LEAD apart — which a quick "click, say one
 * word, click again" walkthrough hits on nearly every click. Nearest has no
 * such overlap: a unit can only be closest to one click. It also self-adjusts
 * to the pace of the recording instead of needing a tuned constant.
 *
 * It returns the INTRO: everything said before the FIRST click is the user
 * framing the task, not a comment on any one spot, so those pieces become the
 * message's own prompt text rather than being dragged onto click 1.
 *
 * The units are already per-word wherever the engine timed the words
 * (`flattenWords`, ann/transcribe) — a segment is a sentence or several, so a
 * segment-grained match hands every word of it to whichever click its FIRST
 * word started nearest, and a click made mid-sentence (the ordinary case:
 * talk, click, keep talking) drags the words after it onto the previous note.
 *
 * Exported bare so the matcher can be tested against fixtures without a
 * recorder, a transcript or a clock.
 */
export function assignWords(
  marks: RecAnnotation[],
  units: TranscriptWord[],
): { texts: Array<{ id: string; text: string }>; intro: string } {
  const timed = marks
    .filter((a): a is RecAnnotation & { t: number } => !!a && typeof a.t === "number")
    .sort((a, b) => a.t - b.t);
  if (!timed.length || !units.length) return { texts: [], intro: "" };
  const intro: string[] = [];
  const words = new Map<string, string[]>(timed.map((c) => [c.id, []]));
  for (const u of units) {
    if (u.start < timed[0].t) {
      intro.push(u.text);
      continue;
    }
    let best = timed[0];
    let bestDist = Math.abs(u.start - timed[0].t);
    for (const c of timed) {
      const dist = Math.abs(u.start - c.t);
      if (dist < bestDist) {
        best = c;
        bestDist = dist;
      }
    }
    (words.get(best.id) as string[]).push(u.text);
  }
  return {
    texts: timed.map((c) => ({ id: c.id, text: collapse((words.get(c.id) as string[]).join(" ")) })),
    intro: collapse(intro.join(" ")),
  };
}

export interface Recorder {
  snapshot(): RecSnapshot;
  subscribe(fn: () => void): () => void;
  /** `annRecBegin` (T:7858). Never throws: a machine that cannot record says so
   *  through `deps.alert` with the MACHINE'S OWN SENTENCE. */
  begin(): Promise<void>;
  /** One stamped, wordless note per click (`annRecMark`, T:7953). Returns the
   *  id, or null when nothing is recording. */
  mark(anchor: RecAnchor): string | null;
  /** A note with no element under it: PAGE coordinates, so it still means
   *  something after the app scrolls (`annRecMarkPoint`, T:7979). */
  markPoint(
    clientX: number,
    clientY: number,
    win?: { scrollX?: number; scrollY?: number } | null,
    nearPath?: string | null,
  ): string | null;
  /** Stop, transcribe, fill the notes in. NEVER THROWS: a failed stop or a
   *  failed transcription leaves the clicks exactly as `mark` left them —
   *  stamped and empty, editable by hand like any other pending note — rather
   *  than costing the reader the walkthrough they just did (T:8128-8131). */
  end(): Promise<void>;
  /** The walkthrough thrown away instead of sent (`annRecDiscard`, T:8337). */
  discard(): Promise<void>;
  /** Whichever of the two the bar's trash means right now (`annDiscard`,
   *  T:8419) is the mode module's dispatch; this is the recording half. */
  stamp(): number;
  seatName(): SeatName;
  idleName(): SeatName;
}

export function createRecorder(deps: RecorderDeps): Recorder {
  const now = deps.now || (() => (typeof performance !== "undefined" ? performance.now() : Date.now()));
  const wallNow = deps.wallNow || (() => Date.now());
  const newId =
    deps.newId ||
    (() => (typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : String(wallNow()) + Math.random()));
  const every = deps.setInterval || ((fn: () => void, ms: number) => setInterval(fn, ms));
  const clear = deps.clearInterval || ((id: unknown) => clearInterval(id as ReturnType<typeof setInterval>));
  const warn = deps.warn || ((m: string, d?: unknown) => console.warn(m, d));
  const shout = deps.alert || ((m: string) => {
    if (typeof alert === "function") alert(m);
  });

  let state: RecState = "off";
  let handle: AudioRecording | null = null;
  let startedAt = 0;
  let ids: string[] = [];
  let timer: unknown = null;
  /** OUR session counter, the twin of `annArmEpoch` for the RECORDING rather
   *  than the mode. T asks `!annRecOn` after each await to mean "no new session
   *  owns the seat"; one number says it without conflating "a new recording
   *  started" with "this one is still settling" (T:8244, 8302, 8380). */
  let session = 0;
  let snap: RecSnapshot = build();
  const watchers = new Set<() => void>();

  function busy(): boolean {
    return state === "stopping" || state === "transcribing" || state === "discarding";
  }

  function status(): string {
    if (state === "recording") {
      // The label IS the status: the running clock and a running count of the
      // clicks so far. One writer, so the tick, a fresh mark and the ender can
      // never show three different tenses of the same state (T:7793-7803).
      return recClock(seconds()) + (ids.length ? " · " + ids.length : "");
    }
    if (state === "stopping") return "Stopping…";
    if (state === "transcribing") return "Transcribing…";
    if (state === "discarding") return "Discarding…";
    return "";
  }

  function seconds(): number {
    // "recording" ONLY. `startedAt` is written by `begin()` when the request
    // COMES BACK, so through "starting" it is still 0 — and a snapshot field
    // that reads 1.8e9 seconds for the width of the start request is a public
    // nonsense the one caller's `state === "recording"` guard happened to hide.
    return state === "recording" ? Math.max(0, (now() - startedAt) / 1000) : 0;
  }

  function build(): RecSnapshot {
    return { state, status: status(), marks: ids.length, seconds: seconds(), busy: busy() };
  }

  /** `annRecPaint` (T:7797) minus the two `textContent` writes: recompute and
   *  tell the watchers. The snapshot object is replaced, never mutated, so a
   *  `useSyncExternalStore` reader sees a new identity exactly when something
   *  changed. */
  function paint(): void {
    snap = build();
    for (const fn of watchers) fn();
  }

  /** Seconds into the live recording, to a TENTH (`annRecStamp`, T:7944). One
   *  writer for both mark functions so the two kinds of note cannot be stamped
   *  at different precisions, and the rounding is `geometry.stampOf`'s — one
   *  copy of the arithmetic, because nothing downstream wants the rest: the
   *  matcher works in whole words seconds apart and the reader is a m:ss clock.
   *  The raw division put seventeen digits per note on the wire
   *  (2.4150000000372529). */
  function stamp(): number {
    return stampOf(now() - startedAt);
  }

  function stopClock(): void {
    if (timer !== null) {
      clear(timer);
      timer = null;
    }
  }

  async function begin(): Promise<void> {
    // `.busy` too (Bugbot, PR #1022): through Stopping…/Transcribing… the mic
    // seat is drawn inert, and a keyboard press must not start a second
    // walkthrough on the epoch the in-flight `end()` is about to disarm. The
    // "starting" state is `annRecStarting` (T:7759): one start request in
    // flight at most, for programmatic callers too — the boot and a re-arm can
    // both ask while the first request is still out, and `disabled` on a button
    // is not a guard a function call sees.
    if (state !== "off") return;
    if (!deps.mode.capable()) return;
    state = "starting";
    paint();
    deps.warm();
    let rec: AudioRecording;
    try {
      // NO `path` of our own — the container is the backend's to name (CP-5:
      // .m4a natively, mp4 or WebM where the browser encodes) and a path whose
      // extension contradicts it is refused. So the audio lands under
      // `<home>/recordings` as a download-manager row (CP-3): the row's ✕ is
      // how a walkthrough is deleted now, and the row is what lets one outlive
      // this session. Cap is the default 30 min, which STOPS and keeps (CP-4)
      // — a mic nobody turned off still turns off (T:7873-7886).
      rec = await deps.capture({ title: "Spoken walkthrough" });
    } catch (err) {
      state = "off";
      paint();
      // Nothing appended: the sentence carries the MACHINE's own answer (System
      // Settings, or a browser that can), and the fixed "allow microphone
      // access" line this used to add was wrong for half of them (T:7888-7893).
      shout("Cannot record — " + errText(err));
      return;
    }
    // Recording needs the same click handler the typed mode uses, just pointed
    // the other way — arm it if the reader went straight for the mic without
    // sliding Comment on first (T:7895-7897).
    if (!deps.mode.isArmed()) deps.mode.arm();
    ids = [];
    handle = rec;
    // The handle lands when the recording is ALREADY RUNNING (CP-1), so this
    // clock and the transcript's own timestamps differ only by the start
    // reply's trip home — inside what the nearest-word match cares about
    // (T:7898-7901).
    startedAt = now();
    state = "recording";
    session += 1;
    if (deps.mode.syncParam) deps.mode.syncParam(); // the URL now says "2"
    paint();
    timer = every(paint, REC_TICK_MS);
  }

  function mark(anchor: RecAnchor): string | null {
    if (state !== "recording") return null;
    const id = newId();
    // `spoken` is filled in by the assignment once the recording is
    // transcribed; until then the chip shows an empty note, same as a manual one
    // not yet typed (T:7950-7952). The anchor is spread LAST and its own `text`
    // (the element digest) rides along untouched.
    deps.notes.add({ id, spoken: "", createdAt: wallNow(), t: stamp(), ...anchor });
    ids.push(id);
    paint();
    return id;
  }

  function markPoint(
    clientX: number,
    clientY: number,
    win?: { scrollX?: number; scrollY?: number } | null,
    nearPath?: string | null,
  ): string | null {
    if (state !== "recording") return null;
    const id = newId();
    const note: RecAnnotation & Record<string, unknown> = {
      id,
      kind: "point",
      spoken: "",
      createdAt: wallNow(),
      t: stamp(),
      // PAGE coordinates (client + scroll), so the note still means something
      // after the app scrolls (T:7962-7964).
      x: Math.round(clientX + ((win && win.scrollX) || 0)),
      y: Math.round(clientY + ((win && win.scrollY) || 0)),
    };
    // The element the click landed OVER when there was one (a forced point —
    // the Point tool or Alt): a HINT and never an anchor (T:7971-7975).
    if (nearPath) note.nearPath = nearPath;
    deps.notes.add(note);
    ids.push(id);
    paint();
    return id;
  }

  async function end(): Promise<void> {
    // The SETTLING GUARD, and the reason it is the first line: a second click
    // during the stop is a NO-OP. Flags down BEFORE the stop's await (Bugbot,
    // PR #665) — the Discard seat stays on screen exactly as long as the mic's
    // `.on` does, and a discard click landing inside this await used to pass
    // its own guard, call `stop()` on an already-inactive recorder
    // (InvalidStateError) and delete nothing while this function went on to
    // send the walkthrough it tried to throw away (T:8133-8148).
    if (state !== "recording" || !handle) return;
    // The WHOLE SESSION is snapshotted synchronously here: the flags drop
    // before the await and Esc re-enables the seat, so a NEW recording can
    // begin while this one settles — every module read after the await would
    // be the new session's (T:8140-8148).
    const rec = handle;
    const mine = session;
    const marks = ids;
    const armed = deps.mode.epoch();
    stopClock();
    ids = [];
    handle = null;
    state = "stopping";
    paint();

    // The ending that KEEPS the file (CP-4) — `cancel()` is the discard. Its
    // reply is the finished file, so nothing here uploads anything. Swallowed
    // like the transcription below: a failed stop leaves the clicks stamped and
    // empty, editable by hand (T:8182-8188).
    const out = await rec.stop().catch((err: unknown) => {
      warn("walkthrough stop failed:", errText(err));
      return null;
    });

    /** A NEW session that began during the settle owns the seat now — its
     *  labels, its clock — and this stale path must not repaint it (T:8244). */
    const stillOurs = () => session === mine;

    // Nothing to transcribe: a failed stop, an empty file, or a stop that
    // landed on the start's own beat (T:8199-8203).
    if (!out || !out.path || !out.bytes || (out.seconds || 0) < REC_MIN_SECONDS) {
      if (stillOurs()) {
        state = "off";
        paint();
      }
      // Ending a walkthrough puts the whole mode away (Akshil, 2026-08-19).
      // Same-epoch only: a reader who re-armed meanwhile owns a NEW arming this
      // stale disarm must not close (T:8190-8198).
      if (deps.mode.isArmed() && deps.mode.epoch() === armed) deps.mode.disarm();
      return;
    }

    // One status seat, two tenses: from "Stopping…" to "Transcribing…". GATED
    // like every other post-settle write — when the seat is someone else's the
    // transcription just runs quietly in the background (T:8221-8243).
    if (stillOurs()) {
      state = "transcribing";
      paint();
    }
    try {
      // Already on disk, written by the app — no upload leg, and no temp dir of
      // ours to prune (T:8245-8248).
      const transcript = await deps.transcribe(out.path);
      // Everything said BEFORE the first click is the main prompt, not a note:
      // it seeds the composer as the message's own words, so the send reads as
      // "here is the task, and here are the spots" rather than a first comment
      // that swallowed the framing. A walkthrough with NO clicks is the
      // degenerate case of the same rule — everything came before the first
      // click, so the whole transcript is the prompt (T:8263-8271).
      let intro: string;
      if (marks.length) {
        const found = marks
          .map((id) => deps.notes.get(id))
          .filter((a): a is RecAnnotation => !!a);
        const assigned = assignWords(found, transcript.words);
        deps.notes.assign(assigned.texts);
        intro = assigned.intro;
      } else {
        intro = collapse(transcript.segments.map((s) => s.text).join(" "));
      }
      // Auto-send, the same way a typed note sends itself: the walkthrough is
      // finished the moment its words land, and "record, talk, stop" should
      // reach Claude without a fourth step. Only when words actually landed —
      // on a note OR as intro; a transcription that assigned nothing leaves the
      // clicks pending and editable rather than sending empty notes
      // (T:8272-8300).
      const spoke = marks.some((id) => {
        const c = deps.notes.get(id);
        return !!(c && c.spoken && !c.sent);
      });
      if (spoke || intro) deps.deliver(intro, spoke);
    } catch (err) {
      warn("spoken annotation transcription failed:", errText(err));
    } finally {
      // A NEW session that began mid-transcription owns the seat — and this
      // stale finally must not repaint, re-name or re-enable any of it (Bugbot,
      // PR #665). Symmetric with the gated writes above: if this ender never
      // painted the status, there is nothing of its to undo (T:8302-8318).
      if (stillOurs()) {
        state = "off";
        paint();
      }
    }
    // The other half of the stop-disarms rule: transcript fetched (or failed —
    // the notes stay stamped either way), walkthrough done, mode off. AFTER the
    // auto-send, deliberately: the composer prefill needs the armed composer it
    // seeds (T:8320-8325).
    if (deps.mode.isArmed() && deps.mode.epoch() === armed) deps.mode.disarm();
  }

  async function discard(): Promise<void> {
    if (state !== "recording" || !handle) return;
    // Snapshotted synchronously, like `end()`'s: a new recording can begin
    // while this one's stop settles, and every read after the await would be
    // the new session's — its marks deleted, its timer killed (Bugbot, PR
    // #665, T:8341-8351).
    const rec = handle;
    const mine = session;
    const doomed = new Set(ids);
    const armed = deps.mode.epoch();
    stopClock();
    ids = [];
    handle = null;
    state = "discarding";
    paint();
    // `cancel()` STOPS AND DELETES (CP-4) — the ✕'s own meaning — where a stop
    // would leave the file behind. It cannot lose to the stop seat either: the
    // handle memoizes ONE ending, so a stop click landing inside this await
    // gets this cancel's promise back instead of opening a second request
    // (T:8368-8371).
    await rec.cancel().catch((err: unknown) => {
      warn("walkthrough discard failed:", errText(err));
      return null;
    });
    if (session === mine) {
      state = "off";
      paint();
    }
    // The marks die with the recording whatever the mode is doing now — they
    // are THIS session's, captured above — and the repaint must not wait on a
    // disarm that an Esc during the settle already spent (T:8385-8392).
    if (doomed.size) deps.notes.remove([...doomed]);
    if (deps.mode.isArmed() && deps.mode.epoch() === armed) deps.mode.disarm();
  }

  return {
    snapshot: () => snap,
    subscribe(fn: () => void) {
      watchers.add(fn);
      return () => {
        watchers.delete(fn);
      };
    },
    begin,
    mark,
    markPoint,
    end,
    discard,
    stamp,
    seatName: () => recSeatName(state),
    idleName: recIdleName,
  };
}
