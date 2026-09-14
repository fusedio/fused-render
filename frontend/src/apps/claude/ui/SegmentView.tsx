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

import { groupToolRuns, isToolRun } from "../protocol/segments";
import { segText } from "../protocol/summaries";
import type { Segment } from "../protocol/types";
import { Caret } from "./Caret";
import { cardKey, runKey } from "./cardPolicy";
import { MarkdownView } from "./MarkdownView";
import { NoticeView } from "./NoticeView";
import { ThinkingView } from "./ThinkingView";
import { ToolChip } from "./ToolChip";
import { ToolRunChip } from "./ToolRunChip";

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
   *  segments may yet land on the end of it. `groupToolRuns` holds the trailing
   *  run open while it is true, or the fold flickers once per tool for the
   *  length of a live multi-tool turn. */
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
  // A settled stretch of tool calls folds into one row (design.md §A,
  // `groupToolRuns`). The ORIGINAL index rides along with every row: a run
  // carries its own `start`, and a plain segment's is counted off the rows
  // before it — because `cardKey` and `cardsAfter` are both keyed by where a
  // segment sits in the TURN, not by where it sits in the grouped list.
  const rows = useMemo(() => {
    let at = 0;
    return groupToolRuns(segments, cardsAfter, live).map((item) => {
      const index = at;
      at += isToolRun(item) ? item.segs.length : 1;
      return { item, index };
    });
  }, [segments, cardsAfter, live]);
  return (
    <>
      {rows.map(({ item, index: i }) => {
        if (isToolRun(item)) {
          // A run never holds a filed card (one ENDS the run before its chip)
          // and never holds the streaming tail (a tail is prose), so neither
          // the `withFiled` wrap nor the `growing` branch can apply here.
          return (
            <ToolRunChip
              key={runKey(seq, item.segs[0], item.start)}
              segs={item.segs}
              start={item.start}
              seq={seq}
            />
          );
        }
        const seg = item;
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
        if (seg.kind === "tool")
          return withFiled(<ToolChip key={key} seg={seg} cardKey={key} />);
        if (seg.kind === "thinking")
          return withFiled(<ThinkingView key={key} cardKey={key} text={seg.text} />);
        if (seg.kind === "notice") return withFiled(<NoticeView key={key} text={seg.text} />);
        // "text", and anything a newer agent.py invents (T:15630-15635).
        const growing = !!tail && tail.index === i;
        if (growing) {
          // The typer's slice, and the caret AFTER the prose element rather than
          // inside it (T:15066 `bodyEl.after(cur)`). `enhance` is off: hljs and
          // the copy button must never run per frame (T:14998-15055) — the pass
          // lands once the tail moves on, or at the end of the run (T:16336),
          // both of which flip this branch off and re-render with `enhance`.
          return (
            <Fragment key={key}>
              <MarkdownView className="seg-text" text={tail.text} enhance={false} />
              {tail.cursor ? <Caret /> : null}
            </Fragment>
          );
        }
        return withFiled(
          <MarkdownView key={key} className="seg-text" text={segText(seg)} enhance />,
        );
      })}
      {children}
    </>
  );
});

export default SegmentView;
