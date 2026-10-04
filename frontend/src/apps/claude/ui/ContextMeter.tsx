// HOW FULL THE MODEL'S HEAD IS, in the composer's control row.
//
// The ring is ONE number — the conversation over the point where the CLI
// auto-compacts it (`usedPct`, 100% = compaction) — and `/context`'s block
// chart is what a press opens. The line above the box says the same number in
// words; they can no longer disagree, because they are one function.
//
// A RING AND NOTHING INSIDE IT, drawn at icon size (16px) in a pill-height
// (24px) button. The digits used to sit in the middle, which made the coin
// 28px against 24px pills and said the number a third time (the tooltip and
// the popover already do). The arc's fill is the reading, and the exact number
// is one hover away; what the ring adds is a glance.
//
// AND IT CHANGES COLOUR AS IT FILLS: dim until three quarters, then yellow
// (`is-warn`, 75%+), then red (`is-high`, 90%+). Three steps rather than a
// gradient because a gradient asks the reader to compare shades; a step asks
// nothing. The thresholds are on the ring's own percentage, so red means
// "auto-compact is 10% away", not "the model's head is 90% full".
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

/** The ring's geometry. The box is 24 units — the pills' height, so the seat
 *  is the row's height and a whole hit target — but the ring itself is 16px
 *  across (r=7 plus the 2-wide stroke), the size of the calendar glyph beside
 *  it: a circle the full height of a text pill reads as a coin, not an icon. */
const R = 7;
/** …and its centre. */
const C = 12;
/** Yellow from here… */
export const WARN_PCT = 75;
/** …and red from here. */
export const HIGH_PCT = 90;

/** The colour step the ring is at: "" (dim), "is-warn" or "is-high". */
export function ringLevel(pct: number): "" | "is-warn" | "is-high" {
  if (pct >= HIGH_PCT) return "is-high";
  if (pct >= WARN_PCT) return "is-warn";
  return "";
}

/** The second line of the tooltip, so the ring reads as a button and not as a
 *  gauge: nothing about a plain ring says it opens anything. */
export const CLICK_HINT = "Click for details";
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
  const level = ringLevel(pct);
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
              className={level ? `c-ctxmeter ${level}` : "c-ctxmeter"}
              data-hint={`${hint}\n${CLICK_HINT}`}
              aria-label={hint}
            >
              <svg
                className="c-ctxmeter-ring"
                viewBox="0 0 24 24"
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
      {/* The headline is the ring's number in full digits; the grid and legend
          below are `/context`'s own picture of the whole model window, buffer
          and all, which is why their per-row percentages are of the window. */}
      <div className="c-ctxpop-dim">
        {groupDigits(report.total)}/{groupDigits(report.compactAt)} tokens before
        auto-compact ({report.pct}%)
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
