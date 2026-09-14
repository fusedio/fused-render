// A settled stretch of tool calls, folded under one chip (design.md §A).
//
// A turn that makes fifteen edits is fifteen chips, and fifteen chips are a
// wall the prose either side of them disappears into. claude.ai folds a turn's
// consecutive steps under one disclosure; this is the same shape, wearing the
// SAME SKIN as `ToolChip` — the `toolchip` rail, `chip-summary`'s row, the
// marker's gutter, the `chip-lead`/`chip-path` split — because a run is a chip
// of chips and a second visual vocabulary for it would read as a second kind of
// thing.
//
// WHICH stretches fold is not decided here: `protocol/segments.ts`
// `groupToolRuns` decides it, and this component only paints what it is handed.
// In particular a run is never live — a running call anywhere in the stretch
// keeps every chip individual — so the header's status glyph is only ever
// `error` or `ok`.
import { memo } from "react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@platform/shadcn/ui/collapsible";
import { cn } from "@platform/lib/utils";

import { toolStatusGlyph } from "../protocol/summaries";
import type { ToolSegment } from "../protocol/types";
import { cardKey, runKey, useCardOpen } from "./cardPolicy";
import { ToolChip } from "./ToolChip";

export interface ToolRunChipProps {
  /** The run's calls, in order. Never fewer than `RUN_MIN` (2). */
  segs: ToolSegment[];
  /** The ORIGINAL index of `segs[0]` in the turn's segment list — the rest are
   *  consecutive from there. What keeps each nested chip's `cardKey` the key it
   *  had before the run existed, so an open chip stays open. */
  start: number;
  /** The container's number (SegmentView's `segSeq`), for the position half of
   *  `cardKey`. */
  seq: number;
}

/** MEMOIZED for `ToolChip`'s reason: a settled run is settled for the rest of
 *  the conversation, and its body is N chip bodies. */
export const ToolRunChip = memo(function ToolRunChip({ segs, start, seq }: ToolRunChipProps) {
  const [open, toggle] = useCardOpen(runKey(seq, segs[0], start));
  // Never `running`: see the note at the top. An error anywhere is the run's
  // status, because a fold that hid a failure behind a tick would be a fold
  // that lied about what happened.
  const status = segs.some((seg) => seg?.status === "error") ? "error" : "ok";
  const name = segs.length + " tool calls";
  return (
    <Collapsible
      open={open}
      onOpenChange={toggle}
      className={cn("toolchip", "toolrun", open && "is-open")}
    >
      <CollapsibleTrigger className="chip-summary">
        <span className="chip-marker" aria-hidden="true">
          ▶
        </span>
        <span className="chip-row">
          <span className={"chip-status is-" + status}>{toolStatusGlyph(status)}</span>
          <span className="chip-name">{name}</span>
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="chip-body">
        {segs.map((seg, j) => {
          // THE ORIGINAL INDEX, not `j`: the key is the chip's identity in the
          // collapse map, and a chip that changed key on being folded into a
          // run would close itself every time the run was opened.
          const key = cardKey(seq, seg, start + j);
          return <ToolChip key={key} seg={seg} cardKey={key} />;
        })}
      </CollapsibleContent>
    </Collapsible>
  );
});

export default ToolRunChip;
