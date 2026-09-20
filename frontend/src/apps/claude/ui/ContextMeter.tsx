// HOW FULL THE MODEL'S HEAD IS, in the composer's control row.
//
// Claude Code prints this in two places and this pill is both of them at once:
// the statusline's percentage (input-only, over the full model window) is the
// RING and the digits inside it, and `/context`'s block chart is what a press
// opens. The one number a reader needs is "can I keep going in this chat" —
// unanswered, the conversation compacts mid-thought.
//
// THE NUMBER LIVES INSIDE THE RING. A ring beside a percentage is two readings
// of one fact competing for the same 40px; a ring AROUND the percentage is one
// object, and it survives the composer's tightest rungs without dropping the
// digits — there is nothing left to drop.
//
// AND IT STAYS DIM AT EVERY LEVEL, which is parity and not an oversight: the
// CLI has no colour ramp while auto-compact is on (spec §4). Red there means
// "this conversation is about to hit a wall", and auto-compact means it is
// about to hit a SUMMARY instead. A pill that turns red on every long chat is
// a pill people stop reading; the dim line above the box is what speaks up.
import { useCallback, useState } from "react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@platform/shadcn/ui/popover";
import type { ContextUsage } from "../protocol/types";
import {
  contextHint,
  contextReport,
  contextInput,
  groupDigits,
  usedPct,
} from "./context-window";
import { useDismissOnWindow } from "./useDismissOnWindow";

export interface ContextMeterProps {
  /** The latest reply's `usage` (`ChatState.context`). `null` draws nothing. */
  usage: ContextUsage | null;
  /** The model id the window is read off: the transcript's own, or the
   *  composer pill's for a chat whose transcript names none. */
  model: string;
}

/** The ring's geometry in its own 28-unit box — the height of the pills
 *  beside it. r=12 keeps the 2-wide stroke inside the viewBox at every angle. */
const R = 12;
/** …and its centre. */
const C = 14;
const CIRCUMFERENCE = 2 * Math.PI * R;

/**
 * Nothing at all until there is something to say. A chat with no reply yet —
 * and the moment right after a compaction, where the CLI's own statusline
 * reports `null` — draws NO pill rather than "0%": an empty meter in an empty
 * conversation is chrome that says nothing, and the reader learns to ignore
 * the seat before it ever has news.
 */
export function ContextMeter({ usage, model }: ContextMeterProps) {
  const [open, setOpen] = useState(false);
  const closeMenu = useCallback(() => setOpen(false), []);
  // The dismissal Base UI's outside-press cannot see: a click INSIDE the
  // preview iframe never reaches this document at all (see `PillSelect`).
  useDismissOnWindow(open, closeMenu);

  if (!usage || contextInput(usage) <= 0) return null;

  const pct = usedPct(model, usage);
  const hint = contextHint(model, usage);
  // Drawn from 12 o'clock clockwise: the arc is a dashed stroke whose first
  // dash is the filled part — one element, rather than an arc path and its
  // large-arc-flag arithmetic, and it degrades to an empty ring at 0.
  const dash = `${((CIRCUMFERENCE * pct) / 100).toFixed(2)} ${CIRCUMFERENCE.toFixed(2)}`;

  // `c-pillwrap` is worn for its positioning alone, as the three select pills
  // wear it. Unlike them, the ring IS the trigger (SchedButton's shape): Base
  // UI then owns the toggle, so a second press on the ring closes the report
  // instead of an outside-press closing it and this button's own click
  // flipping it straight back open (Bugbot, PR #1253).
  return (
    <span className="c-pillwrap c-ctxwrap">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger
          render={
            <button
              type="button"
              className="c-ctxmeter"
              data-hint={hint}
              aria-label={hint}
            >
              <svg
                className="c-ctxmeter-ring"
                viewBox="0 0 28 28"
                aria-hidden="true"
                focusable="false"
              >
                <circle className="c-ctxmeter-track" cx={C} cy={C} r={R} />
                {/* The arc shares the track's centre, and is rotated about
                    that same point — a quarter turn back, so the first dash
                    starts at twelve o'clock and sits concentric with the
                    track (Bugbot, PR #1253). */}
                <circle
                  className="c-ctxmeter-arc"
                  cx={C}
                  cy={C}
                  r={R}
                  strokeDasharray={dash}
                  transform={`rotate(-90 ${C} ${C})`}
                />
              </svg>
              {/* The sentence above already says the number; saying it twice is
                  what an unhidden label here would do. */}
              <span className="c-ctxmeter-pct" aria-hidden="true">
                {pct}
              </span>
            </button>
          }
        />
        <PopoverContent
          side="top"
          align="start"
          sideOffset={6}
          aria-label="Context usage"
          className="c-overlay c-ctxpop w-auto min-w-0 flex-col gap-0 rounded-[10px] bg-[var(--c-panel)] p-3 text-[var(--c-fg)] shadow-none ring-0"
        >
          <ContextReportView usage={usage} model={model} />
        </PopoverContent>
      </Popover>
    </span>
  );
}

/**
 * `/context`, as much of it as this side of the wire can honestly draw.
 *
 * The CLI splits the prompt eleven ways because it BUILT each part; all we are
 * given is the total the API charged. So the grid has one used colour and the
 * legend says `(all context in use)` under it, rather than implying that the
 * system prompt and the tools were measured and came out at zero.
 */
export function ContextReportView({ usage, model }: ContextMeterProps) {
  const report = contextReport(model, usage);
  return (
    <div className="c-ctxpop-body">
      <div className="c-ctxpop-title">Context Usage</div>
      {report.model ? <div className="c-ctxpop-dim">{report.model}</div> : null}
      <div className="c-ctxpop-dim">
        {groupDigits(report.used)}/{groupDigits(report.window)} tokens (
        {report.pct}%)
      </div>
      <div
        className="c-ctxpop-grid"
        style={{ "--c-ctx-cols": report.columns } as React.CSSProperties}
        aria-hidden="true"
      >
        {report.squares.map((square, i) => (
          <span key={i} className={`c-ctxpop-sq is-${square.kind}`}>
            {square.glyph}
          </span>
        ))}
      </div>
      <div className="c-ctxpop-legend">
        {report.legend.map((row) => (
          <div key={row.kind} className="c-ctxpop-row">
            <span className={`c-ctxpop-key is-${row.kind}`} aria-hidden="true">
              {row.glyph}
            </span>
            <span className="c-ctxpop-label">{`${row.label}:`}</span>
            <span className="c-ctxpop-num">
              {groupDigits(row.tokens)} tokens ({row.pct}%)
            </span>
            {row.note ? <span className="c-ctxpop-note">{row.note}</span> : null}
          </div>
        ))}
      </div>
      {report.suggestion ? (
        <div className="c-ctxpop-note">{report.suggestion}</div>
      ) : null}
    </div>
  );
}
