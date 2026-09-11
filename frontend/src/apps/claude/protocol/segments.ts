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
import type { Segment } from "./types";

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
