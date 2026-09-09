// THE ANNOTATION SUBSYSTEM'S CONTRACT. Behaviour source:
// .claude-design/inventory/02-annotations-narrow.md, citing
// fused_render/templates/claude/template.html as `T`.
//
// `Annotation` is the PERSISTED shape — exactly what `annSave` puts in the
// `annotations` param (T:6612) and what `annLoad` reads back (T:6600), field for
// field, because a bookmark written by the template has to open in the port and
// the other way round. Nothing here is a nicer spelling of one of T's fields:
// `iu`/`iv`, `anchorPath`, `nearPath`, `createdAt` and `sent: 0 | 1` are the
// names on the wire (protocol/wire.ts `AnnotationWire`) and in the param.

/** T:8630 — a note points at an ELEMENT (the default, and the reason `kind` is
 *  absent for one: T never writes `kind: "element"`) or at an exact SPOT. */
export type AnnKind = "point";

/** T:6554 `annTool` — what an armed click pins. Session-local, deliberately not
 *  a param: which tool is in hand is a fact about the hand, not the page. */
export type AnnTool = "element" | "point";

/**
 * T's mode, as the one word the state machine is in (inventory §D). T spells
 * this across three booleans and a class (`annOn`, `annRecOn`,
 * `#anncta.busy`, `annBusyHold`); the port keeps ONE value so the illegal
 * combinations cannot be reached:
 *
 *   off           nothing armed — the reader is USING the app
 *   comment       armed, clicks pin notes, the composer opens per click
 *   recording     a spoken walkthrough is live; a click IS a note, wordless
 *   settling      `handle.stop()` / `handle.cancel()` in flight ("Stopping…")
 *   transcribing  words on their way ("Transcribing…")
 *
 * `settling` and `transcribing` are T's `.busy` seat: the mode is still armed
 * (the transcription belongs to THIS chat), the clicks are over, the bar is
 * hidden, and the nav lock is held by `annBusyHold` until a disarm releases it.
 */
export type AnnMode = "off" | "comment" | "recording" | "settling" | "transcribing";

/** The three shapes a target can take (inventory header, T:6033/6294/6492). */
export type AnnLayout =
  /** This page owns `#leftframe`; the overlay nodes are in THIS document. */
  | "split"
  /** `chat_only=1` — the target is the host's iframe stamped
   *  `[data-fused-annotate-target]`; the layer is INJECTED into its document. */
  | "hosted"
  /** A marked frame whose `contentDocument` throws: the overlay stands in the
   *  PARENT document over the frame's rect, and point notes are all it can make
   *  (D349/D355). */
  | "xo"
  /** No target at all — only screenshot chips render (D239). */
  | "none";

/**
 * What a click captured, before any words. Spread into the note by
 * `annCommit` (T:7416) and `annRecMark` (T:7952), so every key here is a key of
 * the persisted note.
 */
export interface AnnAnchor {
  /** Only ever `"point"`; absent means an element note (T:8630). */
  kind?: AnnKind;
  /** PAGE coordinates for a point note — `clientX + scrollX`, rounded — so it
   *  still means something after the app scrolls (T:8631). Overlay-relative in
   *  the XO layout, where there is no readable scroll (T:6325). */
  x?: number;
  y?: number;
  /** The element's `id`, preferred over a path because it survives a re-render
   *  (T:8677). */
  anchorId?: string;
  /** `tag:nth-of-type(n)>…` from `<body>` (T:6667 `annPathOf`). */
  anchorPath?: string;
  /** A FORCED point that landed over a real element names it — a HINT, never an
   *  anchor, or a note about the space beside a button becomes a note about the
   *  button (T:8693, D146: one spelling of one path scheme). */
  nearPath?: string;
  /** Fractions of the PAINTED content box for a click on an `<img>`/`<video>`/
   *  `<canvas>`, to three decimals (T:8672). `null` is T's own absence. */
  iu?: number | null;
  iv?: number | null;
  /** `el.tagName.toLowerCase()` and the element's leading text, 80 chars, so
   *  Claude can find the source without resolving the path (T:8677). */
  tag?: string;
  text?: string;
  /** The XO composer writes `shot: null` and it stays null forever: there is
   *  nothing we are allowed to rasterise over there (T:6329). */
  shot?: null;
}

/** ONE NOTE, exactly as the `annotations` param carries it (T:6607). */
export interface Annotation extends AnnAnchor {
  /** `crypto.randomUUID()` (T:7419). */
  id: string;
  /** The user's words. Empty until typed, or until a walkthrough's transcript
   *  fills it in (T:7952). */
  content: string;
  /** `Date.now()` at save. The pin gate reads it against `annRoundStart`
   *  (T:6935). */
  createdAt: number;
  /** Seconds into a spoken walkthrough, to a TENTH (T:7943 `annRecStamp`). */
  t?: number;
  /** `1` once a send has taken it, `0` when that send was rolled back
   *  (T:16065/16086). Numbers, not a boolean, because that is what the param
   *  holds and what `annotationsIn` reads back. */
  sent?: 0 | 1;
  /** The badge letter this note got on the overview, stamped at send time
   *  (T:16053). */
  label?: string;
  /** Why this note got NO badge, as the sentence the wire prints verbatim
   *  (T:10166 / 10173, cleared by `annApplyOverview`). */
  offscreen?: string;
  /** The OLD per-note crop fields. A note hydrated from a param written before
   *  the overview existed may still carry them; `annApplyOverview` deletes them
   *  permanently (T:10256). Kept in the type so that deletion is typed. */
  shotNote?: string;
}

/**
 * The recorder seam (`ann/rec*`, owned separately). `mode.ts` never touches a
 * microphone: it asks these three questions and calls these two exits, so the
 * §D rows that end a walkthrough live in one machine rather than two.
 *
 * `end` KEEPS the file (T:8173 `handle.stop()`), `discard` deletes it
 * (T:8380 `handle.cancel()`); both settle asynchronously and both report their
 * own progress through `onMode`.
 */
export interface AnnRecorder {
  /** T:6591 `annRecOn` AND T:7759 `annRecStarting` — the mic prompt's window
   *  counts as recording. Everything the machine decides from this answer has
   *  to be true from the moment the seat was pressed: the bar's recording face,
   *  an inert Comment seat, and above all `set(false)` reaching `end()`, so a
   *  dismissal CANCELS the in-flight capture rather than leaving the mic to
   *  come up behind a reader who has already left (Bugbot, PR #1074). */
  recording(): boolean;
  /** T:8114 — a stop or a discard is in flight (`#anncta.busy`). */
  settling(): boolean;
  /** Stop and keep, then transcribe (T:8109 `annRecEnd`). Through the START
   *  window there is nothing to keep: it cancels the pending capture. */
  end(): void;
  /** Stop, delete, and drop this session's marks (T:8340 `annRecDiscard`). */
  discard(): void;
}

/** T:6724 `ANN_BAR` — the bar's two sentences, verbatim. */
export const ANN_BAR: Record<"comment" | "rec", readonly [string, string]> = {
  comment: ["Comment", "Click on a spot and type what to change."],
  rec: ["Voice annotation", "Click on a spot and say what to change."],
};

/** T:7490 — the armed Comment seat's tooltip, verbatim. */
export const ANN_ARMED_TITLE =
  "Comment mode on — clicks pin what the Element/Point" +
  " tool says (Alt overrides for one click, empty space is always a spot)" +
  " · click this button to send the notes and finish, Esc cancels";

/** T:6491 — the layer host's marker attribute. One `closest()` tells the app's
 *  own handlers that an event landed on our layer rather than on the app. */
export const ANN_LAYER_MARK = "data-fused-annotate";

/** T:4556 — the attribute the shell (and `pane/AppPane`) stamps on the iframe
 *  being previewed. A MARK, not a position, so a layout change cannot fool it. */
export const ANN_TARGET_MARK = "data-fused-annotate-target";

/** T:8447 `ANN_TARGET_POLL_MS`. */
export const ANN_TARGET_POLL_MS = 750;

/**
 * T:6785 `ANN_BAR_TOKENS` — the shell's palette, copied onto a bar standing in
 * another document (Akshil, 2026-09-05: the bar is chrome and wears the SHELL's
 * theme, not literals).
 *
 * PAIRS, `[read, write]`, because the two documents do not spell the palette the
 * same way: this page's tokens are the `--c-` prefixed ones every chat sheet
 * uses, while `ANN_LAYER_CSS` — a stylesheet that has to survive in an arbitrary
 * document — declares its own unprefixed set on `:host`. Reading `--bg` off THIS
 * root (which is what this list used to be) found nothing at all, so nothing was
 * ever copied: the hosted and XO bars fell to their dark literals in a light
 * shell, and their picker inherited the app's own `--accent` (QA round 2, item
 * 4).
 */
/** WHERE THIS PAGE'S PALETTE ACTUALLY LIVES. The chat's tokens are aliased under
 *  `.chat-root` and not on `<html>` (styles/chat.css: the unprefixed names
 *  collide with the shell's tokens.css at different values), so a bar standing
 *  in another document has to read them off that box — reading the root found
 *  nothing at all, which is why a light shell still got a dark bar. */
export const ANN_TOKEN_ROOT = ".chat-root";

export const ANN_BAR_TOKENS: ReadonlyArray<readonly [string, string]> = [
  ["--c-bg", "--bg"],
  ["--c-surface", "--surface"],
  ["--c-border", "--border"],
  ["--c-fg", "--fg"],
  ["--c-dim", "--dim"],
  ["--c-accent", "--accent"],
  ["--c-on-accent", "--on-accent"],
  ["--c-error", "--error"],
  ["--c-shadow", "--shadow"],
];

/** T:6141 `ANN_XO_SCROLL` — the zero-scroll stand-in `annPointXY` needs where
 *  the frame's own scroll is unreadable. */
export const ANN_XO_SCROLL: { scrollX: number; scrollY: number } = { scrollX: 0, scrollY: 0 };

/** T:10166 — why a note got no badge, verbatim, because the wire prints it. */
export const ANN_OFFSCREEN_DETACHED =
  "the annotated element was not in the app's DOM when this " +
  "message was sent (the app may have re-rendered since)";

/** T:10173, verbatim for the same reason. */
export const ANN_OFFSCREEN_SCROLLED =
  "the spot was scrolled out of the visible pane when this " + "message was sent";
