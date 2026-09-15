// A turn's segments, in order (T:15650-15712 `renderSegments`).
//
// One element per segment rather than markdown appended to the turn, because
// the streaming tail is one element rewritten as text arrives and the segments
// around it must not be inside what gets rewritten. Only a TRAILING `text`
// segment is growing; everything before it is settled and gets the code-block
// pass, which must never run on the per-frame path (T:15668-15676).
//
// WHICH segment is growing is not decided here: it is decided once, beside the
// typer, and arrives as `tail` (protocol/segments.ts `streamingTailOf`). This
// component only paints it — the typer's slice plus the caret — because the
// alternative is two renderers with their own opinion of the same string.
import { Fragment, memo, useMemo, useRef } from "react";

import { cn } from "@platform/lib/utils";

import { groupCollapsibles, isRun, leadSplit, seatTriggers } from "../protocol/segments";
import { segText } from "../protocol/summaries";
import type { Segment } from "../protocol/types";
import { Caret } from "./Caret";
import { cardKey, runKey, useCardOpens } from "./cardPolicy";
import { MarkdownView } from "./MarkdownView";
import { NoticeView } from "./NoticeView";
import { RunTrigger } from "./RunTrigger";
import { ThinkingView } from "./ThinkingView";
import { ToolChip } from "./ToolChip";

/** Numbers segment containers, for the position keys a thinking block is kept
 *  folded by (T:15196 `cardSeq`). */
let segSeq = 0;

/** The paint side of the typer, for the one segment it is attached to. */
export interface SegmentTail {
  /** The growing segment's index. A tail belonging to the turn's flat body
   *  (`-1`) never reaches here — that turn has no segments. */
  index: number;
  /** `target.slice(0, shown)` — this frame's text (T:15098). */
  text: string;
  /** Whether the caret belongs on screen (T:15063). */
  cursor: boolean;
}

export interface SegmentViewProps {
  segments: Segment[];
  /** The typer's attachment inside THIS turn, or absent when nothing here is
   *  streaming: a settled turn, a history replay, or a live turn whose last
   *  segment is a tool call (the typer is parked — T:15057-15062). */
  tail?: SegmentTail | null;
  /** Parked (answered) cards and anything else that belongs at the turn's tail,
   *  in chronological position (T:14728 `parkResolvedCard`). */
  children?: React.ReactNode;
  /** segment index → whatever is drawn immediately AFTER that segment. This is
   *  how a resolved card gets to sit under the tool chip it answered rather than
   *  at the end of the turn (#18, Transcript's `parkPlan`). */
  cardsAfter?: Map<number, React.ReactNode> | null;
  /** `AssistantTurn.streaming` — this turn is still being polled, so more
   *  segments may yet land on the end of it. `groupCollapsibles` holds the
   *  trailing run OPEN while it is true — its members render individually with
   *  no trigger — or the fold flickers once per tool for the length of a live
   *  multi-tool turn. */
  live?: boolean;
}

/** MEMOIZED for the same reason `Turn` is, and this is where it pays: a settled
 *  turn's segment array is the same object across every poll, so its chips and
 *  its prose are not re-rendered — and `MarkdownView` is not re-parsed — while a
 *  live turn streams above them (T:15545-15554's whole concern). */
export const SegmentView = memo(function SegmentView({
  segments,
  tail,
  children,
  cardsAfter,
  live = false,
}: SegmentViewProps) {
  const seqRef = useRef<number | null>(null);
  if (seqRef.current === null) seqRef.current = ++segSeq;
  const seq = seqRef.current;
  // A settled stretch of tools / thinking / notices folds behind one `more`
  // trigger (design.md §A, `groupCollapsibles`). Every row carries the RAW
  // index it had in `segments`, because `cardKey`, `cardsAfter` and the
  // streaming tail are all keyed by where a segment sits in the TURN.
  const rows = useMemo(
    () => groupCollapsibles(segments, cardsAfter, live),
    [segments, cardsAfter, live],
  );
  // ONE READER AND ONE TOGGLE for every run in this turn: the trigger and the
  // members it opens are two positions in the list below, and a hook per run
  // cannot be called from the loop that builds it (`useCardOpens`).
  const [isRunOpen, toggleRun] = useCardOpens();
  // WHERE EVERY TRIGGER SITS, decided BEFORE anything is built (design.md §A,
  // Q1 revised 2026-09-15): in the bottom-right corner of the prose before the
  // run, or — for the run a turn OPENS on — the corner of the prose that
  // FOLLOWS it, or, failing both, on a bare line of its own.
  //
  // THE TRIGGER IS NOT A ROW. It is drawn inside a prose block, so the run and
  // that block read as ONE element.
  //
  // AND THE BLOCK IS NOT THE TRIGGER'S (PR4 review #4). It used to be: the
  // prose was emitted bare, and a run that followed it POPPED the node back off
  // the list and re-parented it into its own component — so the very frame a
  // live run settled and un-suppressed, every `MarkdownView` before a run
  // changed parent, remounted, re-parsed its markdown, re-ran hljs and threw
  // away the reader's selection and the copy button's "copied" state.
  //
  // So EVERY prose segment gets its own `.seg-block`, streaming or settled,
  // trigger or no trigger — the same keyed div with the prose in slot 0 and the
  // trigger, the caret, or nothing in slot 1. Deciding the seats up front is
  // what lets the block be built ONCE, in its final shape, instead of being
  // written and then rewritten.
  const { seats, bare } = useMemo(
    () => seatTriggers(rows, { tailIndex: tail ? tail.index : -1, cardsAfter }),
    [rows, tail, cardsAfter],
  );
  // THE KEY A TRIGGER TOGGLES — THE SEAT'S, not a member's (PR5 review #6).
  // Two runs seated on one prose block (the turn opened on tool calls and made
  // more after the paragraph) share ONE word and therefore one state, and that
  // state has to survive the polls in which the pair is still assembling. Keyed
  // off the LEADING RUN's first chip it did not: a live turn suppresses the
  // trailing run, a filed card splits one in two, and either way the run that
  // is "first" changes under the reader — the word they opened shut itself.
  // The SEAT cannot change: it is the paragraph the word is drawn in.
  const runKeys = useMemo(() => {
    const keys = new Map<number, string>();
    for (const [at, held] of seats) {
      const row = rows[at];
      if (!row || isRun(row)) continue;
      const key = "run:seat:" + cardKey(seq, row.seg, row.index);
      for (const r of held) keys.set(r, key);
    }
    return keys;
  }, [rows, seats, seq]);
  const nodes: React.ReactNode[] = [];
  rows.forEach((row, r) => {
    if (isRun(row)) {
      const key = runKeys.get(r) ?? runKey(seq, row.segs[0], row.start);
      // NO PROSE EITHER SIDE (design.md §A) — a turn that is nothing but tool
      // calls, or a run that follows a card. Only then does the word take a
      // line; everywhere else it is already drawn in a prose block and this row
      // contributes its MEMBERS alone, at the run's own chronological position:
      // above the paragraph for a leading run, below it for a trailing one.
      if (bare.has(r))
        nodes.push(
          <div key={"bare:" + key} className="seg-block is-bare has-trigger">
            <RunTrigger open={isRunOpen(key)} onToggle={() => toggleRun(key)} />
          </div>,
        );
      if (!isRunOpen(key)) return;
      for (let j = 0; j < row.segs.length; j++) {
        const seg = row.segs[j]!;
        // THE ORIGINAL INDEX, not `j`: the key is the member's identity in
        // the collapse map, and a chip that changed key on being folded into
        // a run would close itself every time the run was opened.
        const mk = cardKey(seq, seg, row.start + j);
        if (seg.kind === "tool") nodes.push(<ToolChip key={mk} seg={seg} cardKey={mk} />);
        else if (seg.kind === "thinking")
          nodes.push(<ThinkingView key={mk} cardKey={mk} text={seg.text} />);
        else nodes.push(<NoticeView key={mk} text={segText(seg)} />);
      }
      return;
    }
    const { seg, index: i } = row;
    const key = cardKey(seq, seg, i);
    // Whatever is filed at this position, wrapped WITH the segment rather
    // than emitted beside it: the map's node and the segment have to stay
    // one keyed child or React re-keys the whole list when a card resolves.
    const filed = cardsAfter?.get(i) ?? null;
    const withFiled = (node: React.ReactNode, k: string = key) =>
      filed ? (
        <Fragment key={k}>
          {node}
          {filed}
        </Fragment>
      ) : (
        node
      );
    if (seg.kind === "tool") {
      nodes.push(withFiled(<ToolChip key={key} seg={seg} cardKey={key} />));
      return;
    }
    if (seg.kind === "thinking") {
      nodes.push(withFiled(<ThinkingView key={key} cardKey={key} text={seg.text} />));
      return;
    }
    if (seg.kind === "notice") {
      nodes.push(withFiled(<NoticeView key={key} text={seg.text} />));
      return;
    }
    // "text", and anything a newer agent.py invents (T:15630-15635).
    const held = seats.get(r);
    const seated = held ? (runKeys.get(held[0]!) ?? null) : null;
    const trigger = seated ? (
      <RunTrigger open={isRunOpen(seated)} onToggle={() => toggleRun(seated)} />
    ) : null;
    // A LEADING run — one seated here from ABOVE (its row is before this one)
    // — sits on the FIRST SENTENCE of this prose, not its last line (Akshil,
    // 2026-09-15; `leadSplit`). The prose is drawn as two blocks: the lead
    // sentence with the trigger in its corner, then the rest. A trailing run
    // alone keeps the corner of the whole paragraph, as before.
    const leading = !!held && held.some((run) => run < r);
    const isTail = !!tail && tail.index === i;
    const source = isTail ? tail.text : segText(seg);
    const split = leading ? leadSplit(source) : null;
    if (isTail) {
      // The typer's slice, and the caret AFTER the prose element rather than
      // inside it (T:15066 `bodyEl.after(cur)`) — in the trigger's own slot of
      // the block, which is what keeps the prose element the same element when
      // the tail moves on and a trigger takes that slot. `enhance` is off: hljs
      // and the copy button must never run per frame (T:14998-15055) — the pass
      // lands once the tail moves on, or at the end of the run (T:16336), both
      // of which flip this branch off and re-render with `enhance`.
      //
      // BOTH, when a leading run is seated here (review #5): the word belongs
      // in this paragraph's corner from the first frame of it, and the caret
      // belongs after the last glyph — the slot holds the pair rather than
      // choosing, so streaming loses neither. Once the first sentence has
      // closed (`split`), the word stays on it and the caret moves on to the
      // rest, which is its own block from that frame.
      if (split) {
        nodes.push(
          segBlock(
            key,
            <MarkdownView className="seg-text" text={split.lead} enhance={false} />,
            trigger,
            true,
            "is-lead",
          ),
          segBlock(
            key + ":rest",
            <MarkdownView className="seg-text" text={split.rest} enhance={false} />,
            tail.cursor ? <Caret /> : null,
            false,
            "is-rest",
          ),
        );
        return;
      }
      nodes.push(
        segBlock(
          key,
          <MarkdownView className="seg-text" text={tail.text} enhance={false} />,
          trigger || tail.cursor ? (
            <>
              {trigger}
              {tail.cursor ? <Caret /> : null}
            </>
          ) : null,
          !!seated,
        ),
      );
      return;
    }
    if (split) {
      const rest = segBlock(
        key + ":rest",
        <MarkdownView className="seg-text" text={split.rest} enhance />,
        null,
        false,
        "is-rest",
      );
      nodes.push(
        segBlock(
          key,
          <MarkdownView className="seg-text" text={split.lead} enhance />,
          trigger,
          true,
          "is-lead",
        ),
        filed ? withFiled(rest, key + ":rest") : rest,
      );
      return;
    }
    const block = segBlock(
      key,
      <MarkdownView className="seg-text" text={segText(seg)} enhance />,
      trigger,
      !!seated,
    );
    nodes.push(filed ? withFiled(block) : block);
  });
  return (
    <>
      {nodes}
      {children}
    </>
  );
});

/** ONE SHAPE FOR BOTH STATES (review #4): the same keyed `div`, the prose in
 *  slot 0, and slot 1 holding the trigger, the caret, or nothing. Written as
 *  one function so the two call sites cannot drift apart — a difference between
 *  them IS the remount this exists to prevent. */
function segBlock(
  key: string,
  prose: React.ReactNode,
  after: React.ReactNode,
  /** `after` is the TRIGGER, not the caret: only then does the last line owe it
   *  room (`styles/transcript.css`, `.has-trigger`). */
  trigger = false,
  /** `is-lead` / `is-rest` — the two halves a leading run splits a prose
   *  segment into (`leadSplit`); the sheet spaces the pair. */
  extra?: string,
): React.ReactElement {
  return (
    <div key={key} className={cn("seg-block", trigger && "has-trigger", extra)}>
      {prose}
      {after}
    </div>
  );
}

export default SegmentView;
