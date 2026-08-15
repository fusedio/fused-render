// Week-grid calendar for scheduled messages — the "when" view that the card
// list (the "what happened" view) cannot be. Seven day columns on an hour
// ruler, one box per run: past runs where they ran, the next materialized run
// of each recurring job, and — as outlined "ghost" boxes — the projected
// future runs the server computed (`upcoming` on a recurring template; see
// server/routers/schedule.py). Ghosts are projections, not entries: they have
// no id and nothing to cancel individually, so their popover offers the
// template's own cancel instead.
//
// Clicking a box opens a details popover (position:fixed, pointerdown-outside
// to dismiss — the GlobalSidebar menu pattern). Clicking empty grid hands the
// clicked time to the parent, which opens the New job modal prefilled: the
// calendar itself neither creates nor cancels anything, it only reads the same
// entries the list reads and reports clicks upward — one source of truth, two
// views of it.
import { useEffect, useMemo, useRef, useState } from "react";
import { cancelScheduledMessage, restoreScheduledMessage } from "@platform/lib/api";
import type { ScheduledMessage } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import {
  calendarEvents,
  describeRepeats,
  formatDue,
  stateLabel,
  stateTone,
} from "./schedule-lib";

// One hour of grid, in px. 44 puts a full day at ~1050px — tall enough that
// two runs half an hour apart do not collide, short enough that the 8am–6pm
// band a person actually schedules into fits a laptop viewport.
const HOUR_H = 44;

// Empty-grid clicks snap to the half hour: the grid is a minute-precision
// surface read at hour precision, and "9:30" is almost always what a click at
// 9:26 meant. The New job form keeps minute precision for those who want it.
const SNAP_MIN = 30;

// What the grid draws: a lib CalendarEvent (the rules) plus geometry.
interface CalEvent {
  key: string;
  time: Date;
  entry: ScheduledMessage;
  // A projected future run of a recurring job — drawn, not stored.
  ghost: boolean;
  // Skipped runs only: whether Unskip is honestly on offer (schedule alive,
  // time not passed). Decided in schedule-lib.calendarEvents, where it is
  // testable — offering it wider only ever bought the user a 404.
  unskippable: boolean;
  // Lane index among overlapping neighbours (0 = leftmost), and how many
  // lanes its cluster has — the two numbers the side-by-side layout needs.
  lane: number;
  lanes: number;
}

function startOfWeek(d: Date): Date {
  const out = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  // Monday-first, which is what a work schedule reads like; getDay() is
  // Sunday-first, hence the +6 dance.
  out.setDate(out.getDate() - ((out.getDay() + 6) % 7));
  return out;
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

// Overlapping runs split the column side-by-side into equal lanes — Google
// Calendar's week-view layout, copied deliberately (Akshil, 2026-08-14: the
// earlier AgentCal-style cascade read as clutter). A chip is CHIP_MIN minutes
// tall, so two runs overlap exactly when they are closer than that.
const CHIP_MIN = 30;

function assignLanes(events: Omit<CalEvent, "lane" | "lanes">[]): CalEvent[] {
  const sorted = [...events].sort((a, b) => a.time.getTime() - b.time.getTime());
  const out: CalEvent[] = [];
  let clusterStart = 0;
  for (let i = 0; i < sorted.length; i++) {
    const prev = out[i - 1];
    const lane =
      prev && sorted[i].time.getTime() - prev.time.getTime() < CHIP_MIN * 60000
        ? prev.lane + 1
        : 0;
    if (lane === 0) clusterStart = i;
    out.push({ ...sorted[i], lane, lanes: 1 });
    for (let j = clusterStart; j <= i; j++) out[j].lanes = lane + 1;
  }
  return out;
}

// Small stroke icons, the GlobalSidebar recipe (16px, stroke=currentColor) at
// button scale. Inline rather than a library — these four are the page's whole
// vocabulary.
const icon = (paths: React.ReactNode) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {paths}
  </svg>
);

export const ICON_CLOCK = icon(<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 3" /></>);
export const ICON_REPEAT = icon(<><path d="M17 2l4 4-4 4" /><path d="M3 11v-1a4 4 0 0 1 4-4h14" /><path d="M7 22l-4-4 4-4" /><path d="M21 13v1a4 4 0 0 1-4 4H3" /></>);
export const ICON_FOLDER = icon(<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />);
export const ICON_SHIELD = icon(<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />);
export const ICON_EDIT = icon(<><path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" /></>);
export const ICON_SKIP = icon(<><polygon points="5 4 15 12 5 20 5 4" /><line x1="19" y1="5" x2="19" y2="19" /></>);
export const ICON_CANCEL = icon(<><circle cx="12" cy="12" r="9" /><path d="M8 8l8 8M16 8l-8 8" /></>);
export const ICON_NOTES = icon(<><line x1="4" y1="7" x2="20" y2="7" /><line x1="4" y1="12" x2="20" y2="12" /><line x1="4" y1="17" x2="14" y2="17" /></>);
export const ICON_RESTORE = icon(<><path d="M3 12a9 9 0 1 0 3-6.7" /><path d="M3 4v5h5" /></>);
export const ICON_INBOX = icon(<><path d="M22 12h-6l-2 3h-4l-2-3H2" /><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" /></>);

function Popover({
  event,
  at,
  onClose,
  onCancelled,
  onEdit,
}: {
  event: CalEvent;
  at: { x: number; y: number };
  onClose: () => void;
  onCancelled: () => void;
  onEdit: (entry: ScheduledMessage) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const entry = event.entry;

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // Stop the shell (or an ancestor modal) also acting on this Esc.
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [onClose]);

  // Keep the popover on screen: flip left of the click when the right edge
  // would clip, and clamp vertically. Width matches the CSS (340px).
  const left = Math.min(at.x + 8, window.innerWidth - 356);
  const top = Math.min(at.y + 8, window.innerHeight - 300);

  // Cancel and restore share one shape: act, refresh, close — and on failure
  // show the server's words and refresh anyway, because the likeliest failure
  // is the honest race (the run fired while the popover was open).
  const act = async (call: (id: string) => Promise<unknown>, id: string) => {
    setBusy(true);
    setError(null);
    try {
      await call(id);
      onCancelled();
      onClose();
    } catch (e) {
      setError((e as Error).message);
      onCancelled();
    } finally {
      setBusy(false);
    }
  };
  const cancel = (id: string) => act(cancelScheduledMessage, id);
  const unskip = (id: string) => act(restoreScheduledMessage, id);

  const repeats = entry.repeats || "";
  const skipped = !event.ghost && entry.state === "cancelled" && !!entry.template_id;

  // What can still be changed: anything that has not acted yet — a waiting
  // run, a projected one, the recurring rule itself, or a SKIPPED run of a
  // live schedule (whose Edit lands on the template: the rule is what
  // repeats, so the change reaches every later run). A dead template's skips
  // never render, so `skipped` here always has a rule behind it.
  const editable =
    event.ghost || skipped ||
    entry.state === "pending" || entry.state === "recurring";

  return (
    <div
      ref={ref}
      className="schedule-cal-popover"
      style={{ left, top }}
      role="dialog"
      aria-label="Scheduled run details"
    >
      <div className="schedule-card-head">
        <span className={`schedule-state schedule-state--${event.ghost ? "recurring" : stateTone(entry)}`}>
          {event.ghost ? "Upcoming" : stateLabel(entry)}
        </span>
      </div>

      {/* The prompt is the headline — it is how the reader recognises the job. */}
      <p className="schedule-pop-title">{entry.message}</p>

      <div className="schedule-pop-rows">
        <span className="schedule-pop-row">
          {ICON_CLOCK}
          <span>{formatDue(event.time.toISOString())}</span>
        </span>
        {repeats && (
          <span className="schedule-pop-row">
            {ICON_REPEAT}
            <span>Repeats {describeRepeats(repeats)}</span>
          </span>
        )}
        <span className="schedule-pop-row">
          {ICON_FOLDER}
          <code title={entry.target}>{entry.target}</code>
        </span>
        {/* Only what departs from the default earns a row. */}
        {(entry.session_id || entry.permission_mode !== "auto") && (
          <span className="schedule-pop-row">
            {ICON_SHIELD}
            <span>
              {entry.session_id ? "Continues an existing chat" : ""}
              {entry.session_id && entry.permission_mode !== "auto" ? " · " : ""}
              {entry.permission_mode !== "auto" ? `${entry.permission_mode} permissions` : ""}
            </span>
          </span>
        )}
      </div>

      {entry.error && <p className="schedule-card-why">{entry.error}</p>}
      {error && <p className="schedule-card-why">{error}</p>}

      <div className="schedule-card-actions">
        {editable && (
          <button type="button" className="btn btn-secondary" disabled={busy}
                  onClick={() => { onEdit(entry); onClose(); }}>
            {ICON_EDIT} Edit
          </button>
        )}
        {!event.ghost && entry.state === "pending" && (
          <button type="button" className="btn btn-secondary" disabled={busy}
                  onClick={() => cancel(entry.id)}>
            {entry.template_id ? ICON_SKIP : ICON_CANCEL}{" "}
            {busy ? "Working…" : entry.template_id ? "Skip this run" : "Cancel"}
          </button>
        )}
        {skipped && event.unskippable && (
          <button type="button" className="btn btn-secondary" disabled={busy}
                  onClick={() => unskip(entry.id)}>
            {ICON_RESTORE} {busy ? "Working…" : "Unskip"}
          </button>
        )}
        {(event.ghost || entry.state === "recurring") && (
          <button type="button" className="btn btn-secondary" disabled={busy}
                  onClick={() => cancel(entry.id)}>
            {ICON_CANCEL} {busy ? "Working…" : "Cancel schedule"}
          </button>
        )}
        {entry.claude_session_id && (
          <button type="button" className="btn btn-secondary"
                  onClick={() => navigateUrl(`/sessions?peek=${encodeURIComponent(entry.claude_session_id!)}`)}>
            {ICON_INBOX} Open in Inbox
          </button>
        )}
      </div>
    </div>
  );
}

export default function ScheduleCalendar({
  entries,
  onCancelled,
  onCreateAt,
  onEdit,
}: {
  entries: ScheduledMessage[];
  onCancelled: () => void;
  onCreateAt: (time: Date) => void;
  onEdit: (entry: ScheduledMessage) => void;
}) {
  const [weekStart, setWeekStart] = useState(() => startOfWeek(new Date()));
  const [open, setOpen] = useState<{ event: CalEvent; x: number; y: number } | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // First paint lands just above 7am, not midnight — the band where schedules
  // live. The 12px of slack keeps the 7am gutter label (centred on its line)
  // from being cut in half at the container's top edge.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 7 * HOUR_H - 12 });
  }, []);

  const days = useMemo(
    () =>
      Array.from({ length: 7 }, (_, i) => {
        const d = new Date(weekStart);
        d.setDate(d.getDate() + i);
        return d;
      }),
    [weekStart],
  );

  const eventsByDay = useMemo(() => {
    // WHAT shows (skipped-run visibility, ghost dedup, Unskip honesty) is
    // schedule-lib.calendarEvents — pure and bun-tested. Here: geometry only.
    const raw = calendarEvents(entries).map((e) => ({
      key: e.key,
      time: new Date(e.iso),
      entry: e.entry,
      ghost: e.ghost,
      unskippable: e.unskippable,
    }));
    const byDay = new Map<number, CalEvent[]>();
    for (const day of days) {
      const todays = raw.filter((e) => sameDay(e.time, day));
      byDay.set(day.getTime(), assignLanes(todays));
    }
    return byDay;
  }, [entries, days]);

  const now = new Date();
  // "August 2026", spanning as "Aug – Sep 2026" when the week straddles a
  // month — the label Google Calendar puts beside the arrows.
  const weekLabel =
    days[0].getMonth() === days[6].getMonth()
      ? days[0].toLocaleDateString(undefined, { month: "long", year: "numeric" })
      : `${days[0].toLocaleDateString(undefined, { month: "short" })} – ${days[6].toLocaleDateString(undefined, { month: "short", year: "numeric" })}`;

  const shiftWeek = (delta: number) => {
    const d = new Date(weekStart);
    d.setDate(d.getDate() + delta * 7);
    setWeekStart(d);
    setOpen(null);
  };

  const clickGrid = (day: Date, e: React.MouseEvent<HTMLDivElement>) => {
    // Only the column itself — a click that landed on an event box is that
    // box's, and it stopPropagation()s before reaching here.
    const rect = e.currentTarget.getBoundingClientRect();
    const minutes = ((e.clientY - rect.top) / HOUR_H) * 60;
    // Clamped to the day's last slot: rounding at the very bottom of a column
    // could reach 24:00, and setMinutes(1440) rolls into TOMORROW — a click
    // in Friday opening a task for Saturday (Bugbot, PR #538).
    const snapped = Math.min(
      Math.round(minutes / SNAP_MIN) * SNAP_MIN,
      24 * 60 - SNAP_MIN,
    );
    const t = new Date(day);
    t.setMinutes(snapped, 0, 0);
    onCreateAt(t);
  };

  return (
    <div className="schedule-cal">
      {/* Controls left — chevrons flanking Today — and where-you-are on the
          right edge (Akshil, 2026-08-14). All three read as real buttons. */}
      <div className="schedule-cal-bar">
        <div className="schedule-cal-nav">
          <button type="button" className="btn btn-secondary" onClick={() => shiftWeek(-1)}
                  aria-label="Previous week">‹</button>
          <button type="button" className="btn btn-secondary"
                  onClick={() => { setWeekStart(startOfWeek(new Date())); setOpen(null); }}>
            Today
          </button>
          <button type="button" className="btn btn-secondary" onClick={() => shiftWeek(1)}
                  aria-label="Next week">›</button>
        </div>
        <span className="schedule-cal-range">{weekLabel}</span>
      </div>

      <div className="schedule-cal-head">
        <div className="schedule-cal-gutter" aria-hidden="true" />
        {days.map((day) => (
          <div key={day.getTime()}
               className={"schedule-cal-day-head" + (sameDay(day, now) ? " is-today" : "")}>
            <span className="schedule-cal-day-name">
              {day.toLocaleDateString(undefined, { weekday: "short" })}
            </span>
            <span className="schedule-cal-day-num">{day.getDate()}</span>
          </div>
        ))}
      </div>

      <div className="schedule-cal-scroll" ref={scrollRef}>
        <div className="schedule-cal-grid" style={{ height: 24 * HOUR_H }}>
          <div className="schedule-cal-gutter">
            {Array.from({ length: 23 }, (_, i) => (
              <span key={i + 1} className="schedule-cal-hour" style={{ top: (i + 1) * HOUR_H }}>
                {new Date(2000, 0, 1, i + 1).toLocaleTimeString(undefined, { hour: "numeric" })}
              </span>
            ))}
          </div>
          {days.map((day) => {
            const todays = eventsByDay.get(day.getTime()) ?? [];
            return (
              <div key={day.getTime()}
                   className={"schedule-cal-col" + (sameDay(day, now) ? " is-today" : "")}
                   onClick={(e) => clickGrid(day, e)}>
                {sameDay(day, now) && (
                  <div className="schedule-cal-now"
                       style={{ top: (now.getHours() + now.getMinutes() / 60) * HOUR_H }} />
                )}
                {todays.map((ev) => (
                  <button
                    key={ev.key}
                    type="button"
                    className={
                      "schedule-cal-event schedule-cal-event--" +
                      (ev.ghost ? "ghost" : stateTone(ev.entry)) +
                      (ev.time < now ? " is-past" : "")
                    }
                    style={{
                      top: (ev.time.getHours() + ev.time.getMinutes() / 60) * HOUR_H,
                      // Side-by-side equal lanes, Google Calendar's split. The
                      // z-index stays a variable, NOT inline: inline would beat
                      // the stylesheet's :hover raise (QA 2026-08-14), and an
                      // inline style outranks any selector.
                      left: `calc(${(ev.lane * 100) / ev.lanes}% + 1px)`,
                      width: `calc(${100 / ev.lanes}% - 3px)`,
                      ["--lane" as string]: ev.lane,
                    } as React.CSSProperties}
                    title={ev.entry.message}
                    onClick={(e) => {
                      e.stopPropagation();
                      setOpen({ event: ev, x: e.clientX, y: e.clientY });
                    }}
                  >
                    {/* Title first, then time — the reading order of a Google
                        Calendar chip: what, then when. */}
                    <span className="schedule-cal-event-text">{ev.entry.message}</span>
                    <span className="schedule-cal-event-time">
                      {ev.time.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}
                    </span>
                  </button>
                ))}
              </div>
            );
          })}
        </div>
      </div>

      {open && (
        <Popover
          event={open.event}
          at={{ x: open.x, y: open.y }}
          onClose={() => setOpen(null)}
          onCancelled={onCancelled}
          onEdit={onEdit}
        />
      )}
    </div>
  );
}
