// The segment model, as pure data. `T`'s `renderSegments` (T:15650-15727) is a
// DOM reconciler: it keeps a per-container `{views, typed, seq}` and mutates
// elements in place. React needs the same decisions expressed as values — which
// index is the growing tail, what each row's collapse key is, and how much of
// the replayed turn belongs to THIS bubble — so that is what this file is.
//
// The rules it preserves, one per T cite:
//   * poll replays the WHOLE turn every 400 ms, so everything here is
//     idempotent and keyed, never appended (T:15650).
//   * ONLY a `text` segment at the very END of the list is still growing; one
//     anywhere else is finished by definition (T:15664-15667).
//   * a tool row's identity is its `tool_use` id — stable across every
//     re-render of that call; a thinking/text/notice row has no id and is keyed
//     by POSITION inside a numbered container (T:15207-15215, cardKey T:15234).
//   * `segments` is authoritative; `data.text` is the flat legacy field and is
//     NOT rendered as well when segments exist, or the reply prints twice
//     (T:16262-16268).
//   * a payload can hold MORE THAN ONE reply — a follow-up absorbed mid-turn
//     leaves both in the same window (D687) — so the SEAMS come from the poll
//     (`turn_breaks`, agent.py `_absorbed_turn_breaks`) and the caller slices
//     the payload before handing a span to `pollBody`. Nothing here guesses a
//     boundary; the old frozen `segBase`/`textBase` pair did, and got it wrong
//     by however much of the first reply streamed after the send (feedback #9).
import type { Segment, ToolSegment, ToolStatus } from "./types";

/** T:15637 — a segment's text, or "" for one that has none. */
export function segText(seg: Segment | undefined | null): string {
  return seg && typeof (seg as { text?: unknown }).text === "string"
    ? (seg as { text: string }).text
    : "";
}

/** The four kinds `buildSegmentView` dispatches on; anything a newer agent.py
 *  invents renders as text (T:15652-15657). */
export type SegmentKind = "text" | "thinking" | "tool" | "notice";

/** T:15703-15705 — the kind a row is actually built as. */
export function viewKind(seg: Segment | undefined | null): SegmentKind {
  const k = seg && (seg as { kind?: string }).kind;
  return k === "tool" || k === "thinking" || k === "notice" ? k : "text";
}

/** T:15234 `cardKey` — a row's identity for the collapse-override map. `seq`
 *  numbers the container for the life of the page so two turns' second thinking
 *  block cannot share one override. */
export function cardKey(seq: number, seg: Segment | undefined | null, i: number): string {
  const s = seg as { kind?: string; id?: string } | undefined | null;
  return s && s.kind === "tool" && s.id ? "tool:" + s.id : seq + ":" + i;
}

/* ── collapsible runs (design.md §A) ────────────────────────────────────────
 *
 * A turn that makes fifteen edits is fifteen chips, and fifteen chips are a
 * wall the prose either side of them disappears into. v1 folded them under an
 * `N tool calls` header; that traded fifteen rows for one row, and one row is
 * still a row the reader did not ask for. v2 takes the row away entirely: the
 * stretch collapses behind a `more ▸` trigger that sits on the RIGHT EDGE of
 * the sentence before it (ui/RunTrigger), so a settled turn is prose and
 * nothing else until the reader asks.
 *
 * WHAT COLLAPSES: any consecutive stretch of `tool` | `thinking` | `notice`,
 * in ANY MIX, down to a SINGLE member — a lone settled chip gets the trigger
 * too, because the goal is zero machinery rows inline and one chip is one such
 * row. There is no `RUN_MIN` any more.
 *
 * THREE THINGS BREAK A RUN, and each one is a thing the reader is owed:
 *
 *   * a `text` segment — prose between two calls means they did not happen
 *     together (and anything a newer agent.py invents renders as text, so it
 *     breaks a run for the same reason);
 *   * a segment with a filed card in `cardsAfter` — the permission or plan the
 *     reader answered under that chip. The card is glued to its chip
 *     (Transcript's `parkPlan`, #18), so the chip stays individual and the run
 *     ends BEFORE it; the members after it are free to start a new one;
 *   * the end of the list.
 *
 * AND TWO THINGS SUPPRESS ONE (v1's bugbot fix, kept): a stretch holding a tool
 * that is not KNOWN SETTLED — running, or wearing a status this file has no
 * literal for — renders member by member, because while the turn is live the
 * progress is the point; and, while the turn is still streaming, the stretch
 * that runs to the END of the list, because that is the only stretch that can
 * still GROW and a lid that goes on when the second call settles comes straight
 * back off when the third lands. A stretch with any segment AFTER it is closed —
 * the poll only ever appends — so folding that one is final. Suppressed means
 * the members are emitted individually WITH NO TRIGGER, exactly as before.
 *
 * RAW INDICES THROUGHOUT. Every row carries the index it had in the list that
 * was handed in — `cardsAfter`, `cardKey` and the streaming tail are all keyed
 * by position in THAT list — so a hole in the array is a run breaker that is
 * dropped from the output without moving anything around it.
 */

/** A folded stretch of collapsible segments. `start` is the ORIGINAL index of
 *  `segs[0]`; the rest are consecutive from there (a hole breaks the stretch),
 *  which is what lets the paint side rebuild each member's `cardKey` without
 *  carrying an index per segment. */
export interface CollapsibleRun {
  kind: "run";
  start: number;
  segs: Segment[];
}

/** One segment, with the index it had in the raw list. A WRAPPER rather than
 *  the bare segment: `kind` is the discriminant for the row union and a segment
 *  has a `kind` of its own, so wrapping is what keeps the two from colliding —
 *  and it carries the raw index instead of leaving the paint side to count it
 *  off the rows before it, which a dropped hole made wrong. */
export interface GroupedSeg {
  kind: "seg";
  index: number;
  seg: Segment;
}

/** One row of the grouped view. */
export type GroupedRow = GroupedSeg | CollapsibleRun;

/** Narrow a grouped row. `kind` is the discriminant either way, so this is the
 *  whole test. */
export function isRun(row: GroupedRow | null | undefined): row is CollapsibleRun {
  return !!row && row.kind === "run";
}

/** The kinds that collapse: everything that is not prose. `viewKind` maps a
 *  hole and anything a newer agent.py invents to "text", so both break a run. */
function isCollapsible(seg: Segment | null | undefined): seg is Segment {
  return !!seg && viewKind(seg) !== "text";
}

/** The `ToolStatus` members (protocol/types.ts) that mean the call is OVER.
 *  Spelled as the LIST OF SETTLED ONES rather than `!== "running"`: a payload
 *  whose `status` is missing, or one carrying a state a newer agent.py invents,
 *  is not something this file knows to be finished, and "not known finished"
 *  must keep its members individual — folding a stretch that might still be
 *  moving is the one mistake this grouping cannot take back on the next poll
 *  without shifting the transcript under the reader. */
const SETTLED: readonly ToolStatus[] = ["ok", "error"];

function isSettled(seg: ToolSegment): boolean {
  const status = (seg as { status?: unknown }).status;
  return SETTLED.some((s) => s === status);
}

/** Is every TOOL in `[from, to)` known to be over? Thinking blocks and notices
 *  have no status and are settled by construction. */
function stretchSettled(list: readonly (Segment | null | undefined)[], from: number, to: number): boolean {
  for (let k = from; k < to; k++) {
    const seg = list[k];
    if (seg && seg.kind === "tool" && !isSettled(seg)) return false;
  }
  return true;
}

/**
 * Group consecutive collapsible segments into runs — see the note above for
 * what breaks one and what suppresses one.
 *
 * `cardsAfter` is read for its KEYS only (a filed card's position), so it is
 * typed as loosely as that use: the paint side's map holds React nodes and this
 * file imports types only.
 */
export function groupCollapsibles(
  segments: Segment[] | null | undefined,
  cardsAfter?: Map<number, unknown> | null,
  /** `AssistantTurn.streaming` — is this turn still being polled? Only a live
   *  turn can gain segments, so it is the only one whose trailing stretch is
   *  held open. Defaults to false, which is every history replay and every
   *  turn that has ended. */
  streaming = false,
): GroupedRow[] {
  const list: (Segment | null | undefined)[] = Array.isArray(segments) ? segments : [];
  const out: GroupedRow[] = [];
  /** One segment on its own, at its own index. A hole paints nothing. */
  const one = (index: number) => {
    const seg = list[index];
    if (seg) out.push({ kind: "seg", index, seg });
  };
  /** `[from, to)` as one run, or nothing when the span is empty. */
  const run = (from: number, to: number) => {
    const segs: Segment[] = [];
    for (let k = from; k < to; k++) {
      const seg = list[k];
      if (seg) segs.push(seg);
    }
    if (segs.length) out.push({ kind: "run", start: from, segs });
  };
  let i = 0;
  while (i < list.length) {
    if (!isCollapsible(list[i])) {
      one(i);
      i += 1;
      continue;
    }
    let end = i;
    while (end < list.length && isCollapsible(list[end])) end += 1;
    // Judged over the WHOLE consecutive stretch, not the piece a card happens
    // to cut off it, so one running call cannot fold the calls in front of it.
    const growing = streaming && end === list.length;
    if (growing || !stretchSettled(list, i, end)) {
      for (let k = i; k < end; k += 1) one(k);
      i = end;
      continue;
    }
    // The card-bearing member is emitted on its own, between the run that ended
    // before it and whatever starts after it.
    let from = i;
    for (let k = i; k < end; k += 1) {
      if (!cardsAfter || !cardsAfter.has(k)) continue;
      run(from, k);
      one(k);
      from = k + 1;
    }
    run(from, end);
    i = end;
  }
  return out;
}

/* ── where a run's trigger sits (design.md §A, Q1 revised 2026-09-15) ───────
 *
 * The trigger belongs in the bottom-right corner of a PROSE block, never on a
 * line of its own — a bare right-aligned word above the first paragraph is the
 * machinery row §A exists to delete, wearing a smaller hat.
 *
 * So a run seats itself on the prose it is adjacent to:
 *
 *   * the prose BEFORE it, wherever there is one (the original rule);
 *   * failing that — the turn OPENS on tool calls — the prose that FOLLOWS it,
 *     which gets the same corner seat. Only the run before the turn's FIRST
 *     prose looks forward: anywhere else "no prose before me" means a card or a
 *     bare stretch broke the chain, and reaching over that is reaching over
 *     something the reader is owed.
 *
 * A prose block can therefore hold TWO runs, one either side. It does NOT grow
 * two words in one corner: they MERGE onto one trigger, which opens both — and
 * the members of each render at their own chronological position, the leading
 * run's above the prose and the trailing run's below it.
 *
 * Looking BACKWARD, a prose block is not a seat when it is the STREAMING TAIL
 * (the corner of a paragraph still being written) or when it has a card filed
 * after it (the card is between the prose and the run). Those runs keep the
 * bare own-line trigger, as does a turn with no prose in it at all. Looking
 * FORWARD the tail IS a seat — see `seat()` below.
 */
export interface TriggerSeats {
  /** prose ROW index → the run ROW indices whose trigger it carries, earliest
   *  first (a leading run before a trailing one). */
  seats: Map<number, number[]>;
  /** Run ROW indices with no prose to sit on: their own right-aligned line. */
  bare: Set<number>;
}

/**
 * Seat every run in `rows` — see the note above. Pure, and over ROW indices,
 * so the paint side does not have to decide placement while it is also
 * building elements (and so this is testable without a renderer).
 */
export function seatTriggers(
  rows: readonly GroupedRow[],
  opts?: {
    /** The growing segment's index in the RAW list, or -1. */
    tailIndex?: number;
    /** Filed cards by raw index — read for its keys only. */
    cardsAfter?: Map<number, unknown> | null;
  },
): TriggerSeats {
  const tailAt = opts?.tailIndex ?? -1;
  const cards = opts?.cardsAfter ?? null;
  const seats = new Map<number, number[]>();
  const bare = new Set<number>();
  /** Is row `r` a prose block a trigger can be drawn in?
   *
   *  `forward` is the leading run reaching DOWN to the paragraph after it, and
   *  that paragraph is allowed to be the STREAMING TAIL (PR5 review #5). The
   *  tail is rewritten per frame, but only its PROSE slot is: the block around
   *  it is the same keyed element with the trigger in slot 1 beside the caret
   *  (`SegmentView.segBlock`), so seating there costs nothing — while refusing
   *  it cost the reader a word parked on a bare line above the answer for as
   *  long as it streamed, teleporting into the corner when the turn settled.
   *
   *  Looking BACKWARD it still refuses: a run behind the tail means the tail is
   *  not the turn's last row — the shape `tailIndex` never produces — and a
   *  word in the corner of a paragraph that is still growing would ride its
   *  last line down the screen. */
  const seat = (r: number, forward = false): boolean => {
    const row = rows[r];
    if (!row || isRun(row) || viewKind(row.seg) !== "text") return false;
    return forward || row.index !== tailAt;
  };
  const sit = (at: number, run: number) => {
    const held = seats.get(at);
    if (held) held.push(run);
    else seats.set(at, [run]);
  };
  let sawProse = false;
  rows.forEach((row, r) => {
    if (!isRun(row)) {
      if (viewKind(row.seg) === "text") sawProse = true;
      return;
    }
    const before = rows[r - 1];
    if (seat(r - 1) && !cards?.has((before as GroupedSeg).index)) {
      sit(r - 1, r);
      return;
    }
    if (!sawProse && seat(r + 1, true)) {
      sit(r + 1, r);
      return;
    }
    bare.add(r);
  });
  return { seats, bare };
}

/** T:15664-15667 — the index of the growing tail, or -1 when the turn's last
 *  row is not prose (it ended on a tool call, or has no rows at all). */
export function tailIndex(list: Segment[]): number {
  return list.length && viewKind(list[list.length - 1]) === "text" ? list.length - 1 : -1;
}

/* ── the streaming tail seam (T:15664-15683, 15057-15062) ───────────────────
 *
 * `T` hands `makeTyper` an ELEMENT and moves it with `retarget` as the turn
 * grows; React cannot hand a component an element, so the same decision is
 * expressed as a value here and the typer is keyed by it. Three cases, and each
 * is one of T's:
 *
 *   * a turn with NO segments — the legacy flat bubble: the typer streams
 *     `turn.text` into the body itself (`index: -1`, T:13486-13504);
 *   * a turn whose last segment is `text` — the growing tail: the typer streams
 *     THAT segment and everything before it is settled prose (T:15664-15667);
 *   * a turn whose last segment is a tool call or a thinking block — the typer
 *     is PARKED and draws nothing, not even its cursor, because the tail of
 *     this reply is not prose (T:15057-15062). `null`.
 *
 * `key` is what the paint side keys the typer on: it changes when the typer must
 * `retarget` (a new turn, or the tail moving along one), and NOT when the tail's
 * text merely grows — which is exactly the distinction between `retarget` (reset
 * the counters) and `update` (drain further). */
export interface StreamingTail {
  /** The transcript row the typer is attached to. */
  turnKey: string;
  /** The growing text segment's index, or -1 for a turn's flat body. */
  index: number;
  /** `<turnKey>#<index>` — the typer's attachment identity. */
  key: string;
  /** The authoritative text the typer is draining towards. */
  text: string;
}

/** The minimum a tail decision needs off a transcript row. Structural so this
 *  file keeps importing types only. */
export interface TailTurn {
  role: string;
  key: string;
  text: string;
  segments?: Segment[];
  streaming?: boolean;
}

/** `<turnKey>#<index>`. Turn keys are `u:<n>` / `a:<n>` / a history uuid, none of
 *  which carries a `#`, so the last one is always the separator. */
export function tailKey(turnKey: string, index: number): string {
  return turnKey + "#" + index;
}

/** The inverse, for the paint side: it holds only the key the last frame was
 *  drawn for and has to say WHERE that frame goes. */
export function parseTailKey(key: string): { turnKey: string; index: number } {
  const at = key.lastIndexOf("#");
  if (at < 0) return { turnKey: key, index: -1 };
  return { turnKey: key.slice(0, at), index: Number(key.slice(at + 1)) };
}

/** Where the typer belongs for the transcript's last row, or `null` to park it.
 *  See `StreamingTail` for the three cases. */
export function streamingTailOf(turn: TailTurn | null | undefined): StreamingTail | null {
  if (!turn || turn.role !== "assistant" || !turn.streaming) return null;
  const segs = Array.isArray(turn.segments) ? turn.segments : [];
  if (!segs.length) {
    return { turnKey: turn.key, index: -1, key: tailKey(turn.key, -1), text: turn.text };
  }
  const i = tailIndex(segs);
  if (i < 0) return null;
  return { turnKey: turn.key, index: i, key: tailKey(turn.key, i), text: segText(segs[i]) };
}

/** What the FINISHED turn's tail should drain to — T:16336's
 *  `typer.finish(tailText || "")` for a segment turn, `finish(flatText)` for a
 *  flat one. A turn that ended on a tool call has no prose left to drain, so it
 *  drains to "" and the caret retires at once (T:15118). */
export function finishedTailText(turn: TailTurn | null | undefined): string {
  if (!turn || turn.role !== "assistant") return "";
  const segs = Array.isArray(turn.segments) ? turn.segments : [];
  if (!segs.length) return turn.text;
  const i = tailIndex(segs);
  return i >= 0 ? segText(segs[i]) : "";
}

/** One transcript row, ready to render. */
export interface SegmentRow {
  /** Stable across polls: `tool:<id>` for a tool call, `<seq>:<i>` otherwise. */
  key: string;
  kind: SegmentKind;
  seg: Segment;
  /** True for the row the typer streams — the trailing `text` segment only. */
  tail: boolean;
}

/** The reconciled view of one poll's segment list. */
export interface SegmentView {
  rows: SegmentRow[];
  /** `tailIndex` of `rows`, or -1. */
  tail: number;
  /** The tail row's text — what the caller hands `typer.finish()` when the run
   *  ends (T:15727). `null` when there is no tail. */
  tailText: string | null;
}

/**
 * Merge one poll's segments into rows, deduping exactly as T does.
 *
 * DEDUPE BY ID, not by value: a poll replays the whole turn, and a tool call
 * that changed status arrives as the same `tool_use` id with new `status` /
 * `output` / `images`. Keeping the LAST occurrence of an id is what makes the
 * replay idempotent while still taking the update (T's `view.update(seg)`,
 * T:15709). Rows without an id keep their position — which is their identity.
 *
 * `seq` is the container's number (see `cardKey`); pass a value that is stable
 * for as long as the bubble is.
 *
 * `prev` is the SAME container's last view, and passing it is what lets an
 * unchanged tool row keep its `seg` OBJECT — see `sameChip`.
 */
export function reconcileSegments(
  seq: number,
  segments: Segment[] | null | undefined,
  prev?: SegmentView | null,
): SegmentView {
  const sliced = (Array.isArray(segments) ? segments : []).filter((s): s is Segment => !!s);
  // Collapse repeats of one `tool_use` id onto the FIRST position it held, with
  // the LATEST payload: position is chronology, the payload is the current
  // state of the call.
  const byId = new Map<string, number>();
  const list: Segment[] = [];
  for (const seg of sliced) {
    const s = seg as { kind?: string; id?: string };
    const id = s.kind === "tool" && s.id ? s.id : "";
    if (id) {
      const at = byId.get(id);
      if (at !== undefined) {
        list[at] = seg;
        continue;
      }
      byId.set(id, list.length);
    }
    list.push(seg);
  }
  const tail = tailIndex(list);
  // The previous view's rows by key, so an unchanged chip can be handed back
  // its own object (see `sameChip`).
  const was = new Map<string, SegmentRow>();
  if (prev) for (const row of prev.rows) was.set(row.key, row);
  const rows = list.map((seg, i) => {
    const key = cardKey(seq, seg, i);
    const kind = viewKind(seg);
    const before = was.get(key);
    // IDENTITY, NOT EQUALITY: the carried-over object is the point, because
    // `ToolChip` is `memo`'d and `seg` is its only interesting prop.
    const keep = before && before.kind === kind && sameChip(before.seg, seg);
    return { key, kind, seg: keep ? before!.seg : seg, tail: i === tail };
  });
  return { rows, tail, tailText: tail >= 0 ? segText(list[tail]) : null };
}

/**
 * Is this the same live chip as last poll, for rendering purposes? T:15549-15554
 * answers it with a key of `[status, output, images.length]` and spends five
 * lines on the omission:
 *
 *   "`input` is deliberately NOT in the key. It cannot change under a live chip
 *   — agent.py reads tool calls only from FINALIZED assistant rows, whose input
 *   is complete and deduped by tool id — and a Write's `content` is uncapped, so
 *   keying on it would re-stringify the whole file being written on every 400 ms
 *   poll for the rest of the turn. `output` is capped (4000 chars) and images
 *   contribute only their count, so what is left is cheap."
 *
 * Native had no equivalent: every poll built a fresh `seg` object, so
 * `ToolChip`'s `memo` never hit and a `Write` chip re-serialised its whole
 * `<pre>` body 2.5×/s for the length of the turn — with the `JSON.stringify` of
 * the uncapped `content` alongside it. Not a wrong-output bug; precisely the
 * cost T's rule exists to avoid.
 *
 * TOOL ROWS ONLY. For a text or thinking segment the body IS the content, so
 * "unchanged" would have to be a deep comparison of the very string that is
 * growing — the opposite of cheap, and those rows are the ones that genuinely
 * change on every poll.
 */
function sameChip(a: Segment, b: Segment): boolean {
  const x = a as { kind?: string; id?: string; status?: unknown; output?: unknown; images?: unknown[] };
  const y = b as { kind?: string; id?: string; status?: unknown; output?: unknown; images?: unknown[] };
  if (x.kind !== "tool" || y.kind !== "tool") return false;
  // The id is the chip's identity in the first place, so a key collision across
  // two different calls cannot be smuggled past the rest of the test.
  if (!x.id || x.id !== y.id) return false;
  if (x.status !== y.status) return false;
  if (x.output !== y.output) return false;
  return (Array.isArray(x.images) ? x.images.length : 0) ===
    (Array.isArray(y.images) ? y.images.length : 0);
}

/** Which body a poll's payload is: segments when it has any, the flat legacy
 *  text otherwise. NEVER both (T:16255-16261). */
export type Body =
  | { mode: "segments"; view: SegmentView; text: "" }
  | { mode: "text"; view: null; text: string }
  | { mode: "empty"; view: null; text: "" };

/**
 * T:16284-16311 — pick the body for one poll, after D687 slicing.
 *
 * The FLIP matters: a first poll with text but no segments yet (no assistant
 * row on disk) starts the turn on the legacy text path, and the moment segments
 * arrive they take it back — the flat text must not stay behind them
 * (T:16289-16296). Callers detect the flip as `mode` changing "text" →
 * "segments" and clear whatever the text path drew.
 */
export function pollBody(
  segments: Segment[] | null | undefined,
  text: string | null | undefined,
  seq: number,
  /** The same container's previous view, so unchanged chips keep their objects
   *  and `ToolChip`'s `memo` hits (T:15549-15554). */
  prev?: SegmentView | null,
): Body {
  const flat = text || "";
  const view = reconcileSegments(seq, segments, prev);
  if (view.rows.length) return { mode: "segments", view, text: "" };
  if (flat) return { mode: "text", view: null, text: flat };
  return { mode: "empty", view: null, text: "" };
}
