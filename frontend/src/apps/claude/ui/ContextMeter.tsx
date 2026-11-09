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
import { ChevronRight } from "lucide-react";
import { useCallback, useEffect, useId, useState } from "react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@platform/shadcn/ui/popover";
import { runAgent } from "../protocol/agent";
import type { ContextBreakdown, ContextDetail, ContextUsage, ErrorOnly } from "../protocol/types";
import {
  contextBreakdownReport,
  contextHint,
  contextReport,
  contextInput,
  contextTotal,
  groupDigits,
  usedPct,
  type ContextLegendRow,
} from "./context-window";
import { useDismissOnWindow } from "./useDismissOnWindow";

export interface ContextMeterProps {
  /** The latest reply's `usage` (`ChatState.context`). `null` draws nothing. */
  usage: ContextUsage | null;
  /** The model id the window is read off: the transcript's own, or the
   *  composer pill's for a chat whose transcript names none. */
  model: string;
  /** The run whose live host is asked for `/context`'s split on open. Absent
   *  or null: the one-row view only. */
  runId?: string | null;
  /** A chat with no run here (opened from history): the server forks this
   *  session to ask instead. Used only when `runId` is absent. */
  sessionId?: string | null;
  /** Test seam: the `context_usage` call. */
  fetchBreakdown?: FetchBreakdown;
}

export type FetchBreakdown = (runId: string) => Promise<ContextBreakdown | ErrorOnly>;

/** A target with no run: `session:<id>`. */
const SESSION = "session:";

const defaultFetch: FetchBreakdown = (target) =>
  runAgent(
    "context_usage",
    target.startsWith(SESSION)
      ? { session_id: target.slice(SESSION.length) }
      : { run_id: target },
  );

/**
 * `/context`'s split for `runId`, asked once each time the popover OPENS (never
 * polled). The last answer for the same run stays up until the new one lands;
 * an error or a different run is `null`, which is the one-row view.
 */
export function useContextBreakdown(
  open: boolean,
  runId: string | null | undefined,
  fetchBreakdown: FetchBreakdown = defaultFetch,
  /** The latest reply's total. Each new value asks once more, a moment later,
   *  while the host is still up — the server keeps that answer for when the
   *  popover opens after the session has idled out. Omitted: on open only. */
  usageKey?: number,
): ContextBreakdown | null {
  const [got, setGot] = useState<{ runId: string; data: ContextBreakdown } | null>(null);
  const take = useCallback(
    (id: string, res: ContextBreakdown | ErrorOnly, keepOnError: boolean) => {
      const ok = !res.error && Array.isArray((res as ContextBreakdown).categories);
      if (ok) setGot({ runId: id, data: res as ContextBreakdown });
      else if (!keepOnError) setGot(null);
    },
    [],
  );
  useEffect(() => {
    if (!open || !runId) return;
    let live = true;
    fetchBreakdown(runId).then(
      (res) => {
        if (live) take(runId, res, false);
      },
      () => {
        if (live) setGot(null);
      },
    );
    return () => {
      live = false;
    };
  }, [open, runId, fetchBreakdown, take]);
  useEffect(() => {
    // A session target forks a CLI per ask: on open only.
    if (!runId || !usageKey || runId.startsWith(SESSION)) return;
    const key = `${runId}:${usageKey}`;
    let live = true;
    const timer = setTimeout(() => {
      // One ask per reply per page, however many seats show this chat.
      if (prefetched.has(key)) return;
      prefetched.add(key);
      fetchBreakdown(runId).then(
        (res) => {
          if (live) take(runId, res, true);
        },
        () => {},
      );
    }, PREFETCH_DELAY_MS);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [runId, usageKey, fetchBreakdown, take]);
  return got && got.runId === runId ? got.data : null;
}

/** Long enough for a turn's last usage rows to settle. */
const PREFETCH_DELAY_MS = 1500;
const prefetched = new Set<string>();

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
export function ContextMeter({ usage, model, runId, sessionId, fetchBreakdown }: ContextMeterProps) {
  const [open, setOpen] = useState(false);
  const closeMenu = useCallback(() => setOpen(false), []);
  // The dismissal Base UI's outside-press cannot see: a click INSIDE the
  // preview iframe never reaches this document at all (see `PillSelect`).
  useDismissOnWindow(open, closeMenu);
  const target = runId || (sessionId ? `${SESSION}${sessionId}` : null);
  const breakdown = useContextBreakdown(open, target, fetchBreakdown, contextTotal(usage));

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
          <ContextReportView usage={usage} model={model} breakdown={breakdown} />
        </PopoverContent>
      </Popover>
    </span>
  );
}

/**
 * `/context`, as much of it as this side of the wire can honestly draw.
 *
 * With the live CLI's `breakdown`, its own categories and colours. Without one
 * (still asking, no live host, another agent) all we have is the total the API
 * charged, so the grid has one used colour and the legend says `(all context
 * in use)` under it, rather than implying that the system prompt and the tools
 * were measured and came out at zero.
 */
export function ContextReportView({
  usage,
  model,
  breakdown,
}: {
  usage: ContextUsage | null;
  model: string;
  breakdown?: ContextBreakdown | null;
}) {
  const report = breakdown
    ? contextBreakdownReport(model, usage, breakdown)
    : contextReport(model, usage);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const toggle = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  return (
    <div className="c-ctxpop-body">
      <div className="c-ctxpop-title">Context Usage</div>
      {report.model ? <div className="c-ctxpop-dim">{report.model}</div> : null}
      {/* The headline is the ring's number in full digits, and the grid and
          legend below are shares of the same pair — Messages is the headline's
          numerator, Messages + Free space is its denominator. */}
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
          <span
            key={i}
            className={`c-ctxpop-sq is-${square.kind}${square.tone ? ` is-tone-${square.tone}` : ""}`}
          >
            {square.glyph}
          </span>
        ))}
      </div>
      <div className="c-ctxpop-legend">
        {report.legend.map((row) => (
          <LegendRow
            key={row.id}
            row={row}
            open={expanded.has(row.id)}
            onToggle={toggle}
          />
        ))}
      </div>
      {report.deferred.length ? (
        <div className="c-ctxpop-legend c-ctxpop-deferred">
          {report.deferred.map((row) => (
            <LegendRow
              key={row.id}
              row={row}
              open={expanded.has(row.id)}
              onToggle={toggle}
            />
          ))}
        </div>
      ) : null}
      {report.suggestion ? (
        <div className="c-ctxpop-note">{report.suggestion}</div>
      ) : null}
    </div>
  );
}

/** One legend row. With a drill-down list the row is a button that opens it
 *  underneath (collapsed by default); without one it is plain text. */
function LegendRow({
  row,
  open,
  onToggle,
}: {
  row: ContextLegendRow;
  open: boolean;
  onToggle: (id: string) => void;
}) {
  const listId = useId();
  const kindCls = `is-${row.kind}${row.tone ? ` is-tone-${row.tone}` : ""}`;
  const body = (
    <>
      <span className={`c-ctxpop-key ${kindCls}`} aria-hidden="true">
        {row.glyph}
      </span>
      <span className="c-ctxpop-label">{`${row.label}:`}</span>
      <span className="c-ctxpop-num">
        {groupDigits(row.tokens)} tokens{row.pct ? ` (${row.pct}%)` : ""}
      </span>
      {row.note ? <span className="c-ctxpop-note">{row.note}</span> : null}
    </>
  );
  const rowCls = `c-ctxpop-row${row.kind === "deferred" ? " is-deferred" : ""}`;
  if (!row.detail) return <div className={rowCls}>{body}</div>;
  return (
    <>
      <button
        type="button"
        className={`${rowCls} c-ctxpop-toggle`}
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => onToggle(row.id)}
      >
        {body}
        <ChevronRight
          className={`c-ctxpop-chev${open ? " is-open" : ""}`}
          size={11}
          aria-hidden="true"
        />
      </button>
      {open ? <DetailList id={listId} detail={row.detail} /> : null}
    </>
  );
}

/** A memory file's path, cut to its last two segments (the whole path is the
 *  tooltip). Anything without a slash is shown as is. */
function shortName(name: string): string {
  const parts = name.split("/").filter(Boolean);
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : name;
}

function DetailList({ id, detail }: { id: string; detail: ContextDetail }) {
  return (
    <ul id={id} className="c-ctxpop-sub">
      {detail.items.map((item, i) => (
        <li key={`${i}:${item.name}`} className="c-ctxpop-subrow">
          <span className="c-ctxpop-subname" data-hint={item.name}>
            {shortName(item.name)}
          </span>
          {item.count ? (
            <span className="c-ctxpop-num">{`(${item.count} ${item.count === 1 ? "tool" : "tools"})`}</span>
          ) : null}
          <span className="c-ctxpop-num">{`· ${groupDigits(item.tokens)} tokens`}</span>
        </li>
      ))}
      {detail.more > 0 ? (
        <li className="c-ctxpop-subrow c-ctxpop-num">
          {`+${detail.more} more · ${groupDigits(detail.more_tokens)} tokens`}
        </li>
      ) : null}
    </ul>
  );
}
