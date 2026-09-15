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

import { groupCollapsibles, isRun, viewKind } from "../protocol/segments";
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
  // THE TRIGGER IS NOT A ROW. It is drawn inside the prose block immediately
  // before the run, so the run and that block read as ONE element.
  //
  // AND THE BLOCK IS NOT THE TRIGGER'S (PR4 review #4). It used to be: the
  // prose was emitted bare, and a run that followed it POPPED the node back off
  // the list and re-parented it into its own component — so the very frame a
  // live run settled and un-suppressed, every `MarkdownView` before a run
  // changed parent, remounted, re-parsed its markdown, re-ran hljs and threw
  // away the reader's selection and the copy button's "copied" state.
  //
  // So EVERY prose segment gets its own `.seg-block`, streaming or settled,
  // trigger or no trigger. The block's key is the segment's own `cardKey` and
  // its first child is the prose, in both states; the trigger is a SIBLING
  // inside it, rewritten into the slot the caret (or nothing) held. React
  // matches the div by key and the prose by position, so the element survives
  // the transition untouched.
  const nodes: React.ReactNode[] = [];
  /** The prose block the NEXT row may put its trigger in: where it sits in
   *  `nodes`, the key it was built under, and the prose element itself. Null
   *  after anything that is not a plain settled prose block — which is exactly
   *  the set `carries` refuses below. */
  let slot: { at: number; key: string; prose: React.ReactNode } | null = null;
  rows.forEach((row, r) => {
    if (isRun(row)) {
      const key = runKey(seq, row.segs[0], row.start);
      const shown = isRunOpen(key);
      const trigger = <RunTrigger open={shown} onToggle={() => toggleRun(key)} />;
      const before = rows[r - 1];
      // Only settled prose carries a trigger: the growing tail is being
      // rewritten per frame (the trigger would be inside what the typer owns)
      // and a block with a card filed after it has the card between it and the
      // run. Either way the trigger takes its own right-aligned line.
      const carries =
        !!slot &&
        !!before &&
        !isRun(before) &&
        viewKind(before.seg) === "text" &&
        !(tail && tail.index === before.index) &&
        !cardsAfter?.has(before.index);
      // …and it is REWRITTEN IN PLACE, same index, same key, same prose element.
      if (carries && slot) nodes[slot.at] = segBlock(slot.key, slot.prose, trigger, true);
      else
        nodes.push(
          // NO PROSE BEFORE THE RUN (design.md §A, Q1) — a turn that opens on a
          // tool call, or a run that follows a card.
          <div key={"bare:" + key} className="seg-block is-bare has-trigger">
            {trigger}
          </div>,
        );
      slot = null;
      if (shown)
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
    slot = null;
    const { seg, index: i } = row;
    const key = cardKey(seq, seg, i);
    // Whatever is filed at this position, wrapped WITH the segment rather
    // than emitted beside it: the map's node and the segment have to stay
    // one keyed child or React re-keys the whole list when a card resolves.
    const filed = cardsAfter?.get(i) ?? null;
    const withFiled = (node: React.ReactNode) =>
      filed ? (
        <Fragment key={key}>
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
    if (tail && tail.index === i) {
      // The typer's slice, and the caret AFTER the prose element rather than
      // inside it (T:15066 `bodyEl.after(cur)`) — in the trigger's own slot of
      // the block, which is what keeps the prose element the same element when
      // the tail moves on and a trigger takes that slot. `enhance` is off: hljs
      // and the copy button must never run per frame (T:14998-15055) — the pass
      // lands once the tail moves on, or at the end of the run (T:16336), both
      // of which flip this branch off and re-render with `enhance`.
      nodes.push(
        segBlock(
          key,
          <MarkdownView className="seg-text" text={tail.text} enhance={false} />,
          tail.cursor ? <Caret /> : null,
        ),
      );
      return;
    }
    const prose = <MarkdownView className="seg-text" text={segText(seg)} enhance />;
    if (filed) {
      nodes.push(withFiled(segBlock(key, prose, null)));
      return;
    }
    slot = { at: nodes.length, key, prose };
    nodes.push(segBlock(key, prose, null));
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
): React.ReactElement {
  return (
    <div key={key} className={cn("seg-block", trigger && "has-trigger")}>
      {prose}
      {after}
    </div>
  );
}

export default SegmentView;
