// Shared vocabulary for the two schedule views (Scheduled.tsx list cards,
// ScheduleCalendar.tsx week grid): how an entry's pair of facts — `state` (did
// it send) and `turn` (how did the session go) — collapses into one label and
// one tone. Split out of Scheduled.tsx when the calendar arrived, so the two
// views cannot drift into describing the same entry differently.
import type { ScheduledMessage, ScheduledState } from "@platform/lib/api";

export function formatDue(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

// Relative time for a due stamp, in both directions — a pending entry is "in
// 20m", a sent one "12m ago". The list is mostly read to answer "when?", so this
// carries more than the absolute stamp does (which is still shown as a title).
export function relativeDue(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const secs = (t - Date.now()) / 1000;
  const ahead = secs >= 0;
  const s = Math.abs(secs);
  const say = (n: number, unit: string) =>
    ahead ? `in ${n}${unit}` : `${n}${unit} ago`;
  if (s < 60) return ahead ? "any moment" : "just now";
  if (s < 3600) return say(Math.round(s / 60), "m");
  if (s < 86400) return say(Math.round(s / 3600), "h");
  return say(Math.round(s / 86400), "d");
}

// Not finished with: waiting for its time, being sent, sent with a turn still
// running — or a recurring rule, which is never finished with by nature. The
// sent-but-running case is why this is not just a `state` check.
export const isLive = (e: ScheduledMessage) =>
  e.state === "pending" ||
  e.state === "sending" ||
  e.state === "recurring" ||
  (e.state === "sent" && !e.turn);

const STATE_LABELS: Record<ScheduledState, string> = {
  pending: "Scheduled",
  sending: "Sending…",
  sent: "Sent",
  missed: "Missed",
  error: "Failed",
  cancelled: "Cancelled",
  recurring: "Repeats",
};

// `sent` only means the SESSION STARTED. How the turn then went is a second fact,
// and conflating them would report a dead turn as a clean send — so a sent row is
// labelled by its turn once the turn has one.
export function stateLabel(entry: ScheduledMessage): string {
  // An OCCURRENCE that didn't run was SKIPPED, whoever decided it: a cancelled
  // one is the user's skip, and a `missed` one is the loop's own skip-not-
  // catch-up verdict (SCH-13 / D292 — the store's error text already says
  // "skipped"). Painting the loop's as "Missed" filed routine behavior under
  // faults.
  if ((entry.state === "cancelled" || entry.state === "missed") && entry.template_id)
    return "Skipped";
  if (entry.state === "sent") {
    if (entry.turn === "ok") return "Ran";
    if (entry.turn === "failed") return "Turn failed";
    if (entry.turn === "cancelled") return "Stopped";
    // Not "Running…": nothing is watching it any more, and saying otherwise is
    // the frozen-progress-bar lie the job registry's `stalled` state avoids.
    if (entry.turn === "unknown") return "Stopped reporting";
    return "Running…";
  }
  return STATE_LABELS[entry.state] ?? entry.state;
}

// Which CSS state class a row paints with. A failed turn reads as a failure even
// though `state` is the cheerful half of the pair.
export function stateTone(entry: ScheduledMessage): string {
  if ((entry.state === "cancelled" || entry.state === "missed") && entry.template_id)
    return "skipped";
  if (entry.state === "sent" && (entry.turn === "failed" || entry.turn === "unknown"))
    return "error";
  if (entry.state === "sent" && !entry.turn) return "sending";
  return entry.state;
}

// ---- Calendar event building -----------------------------------------------
// Pure, so the rules that decide WHAT the week grid shows are testable without
// a DOM. The component adds only geometry (day split, lanes, pixels).

export interface CalendarEvent {
  key: string;
  // The instant the box sits at, ISO. A handled entry sits where it actually
  // acted; everything still waiting sits at its due time.
  iso: string;
  entry: ScheduledMessage;
  // A projected future run of a recurring job — drawn, not stored.
  ghost: boolean;
  // Skipped runs only: whether Unskip is honestly on offer (the schedule is
  // still alive and the run's time has not passed). Offering it more widely
  // was QA'd as "everything breaks": the server refuses, and the popover
  // showed the 404 where a disabled affordance should have been.
  unskippable: boolean;
}

// Whether a skipped run can honestly be unskipped: it is a skipped occurrence,
// its schedule still exists, and its time has not passed — the exact
// conditions schedule.restore() enforces server-side. One function so the
// calendar popover and the list card can never disagree.
export function canUnskip(
  entry: ScheduledMessage,
  entries: ScheduledMessage[],
  now: Date = new Date(),
): boolean {
  if (entry.state !== "cancelled" || !entry.template_id) return false;
  if (!entries.some((e) => e.id === entry.template_id && e.state === "recurring"))
    return false;
  const due = new Date(entry.due);
  return !Number.isNaN(due.getTime()) && due.getTime() > now.getTime();
}

export function calendarEvents(
  entries: ScheduledMessage[],
  now: Date = new Date(),
): CalendarEvent[] {
  const liveTemplates = new Set(
    entries.filter((e) => e.state === "recurring").map((e) => e.id),
  );
  const out: CalendarEvent[] = [];
  for (const entry of entries) {
    if (entry.state === "recurring") {
      // The materialized next occurrence is a real entry and draws itself;
      // ghosts cover everything past it, deduped to the minute so the first
      // run is never drawn twice.
      const stored = new Set(
        entries
          .filter((e) => e.template_id === entry.id)
          .map((e) => Math.floor(new Date(e.due).getTime() / 60000)),
      );
      for (const iso of entry.upcoming ?? []) {
        const t = new Date(iso);
        if (Number.isNaN(t.getTime())) continue;
        if (stored.has(Math.floor(t.getTime() / 60000))) continue;
        out.push({ key: `${entry.id}@${iso}`, iso, entry, ghost: true, unskippable: false });
      }
      continue;
    }
    if (entry.state === "cancelled") {
      // A skipped run stays visible only while the schedule it is an
      // exception TO still exists — a dead template's skips are just history
      // (the list keeps them), and on the grid they were immortal clutter
      // with an Unskip that could only 404. A plain cancelled one-shot is off
      // the calendar the way a deleted event is off Google's.
      if (!entry.template_id || !liveTemplates.has(entry.template_id)) continue;
      if (Number.isNaN(new Date(entry.due).getTime())) continue;
      out.push({
        key: entry.id,
        iso: entry.due,
        entry,
        ghost: false,
        unskippable: canUnskip(entry, entries, now),
      });
      continue;
    }
    const terminal = entry.state === "sent" || entry.state === "error";
    const iso = (terminal && entry.fired) || entry.due;
    if (Number.isNaN(new Date(iso).getTime())) continue;
    out.push({ key: entry.id, iso, entry, ghost: false, unskippable: false });
  }
  return out;
}

// ---- Board (kanban) columns ---------------------------------------------------
// The Schedule page's third view, mirroring the Inbox board's shape. Columns
// derive strictly from fields that exist — `state` and `turn` — through the
// same collapse stateTone performs, so a card sits in the column its pill
// already claims. No drag: unlike the Inbox's triage status, these states are
// the scheduler's own facts, not labels a person may move.

export const BOARD_COLUMNS = [
  { key: "upcoming", label: "Upcoming" },
  { key: "ran", label: "Ran" },
  { key: "attention", label: "Needs attention" },
  { key: "cancelled", label: "Cancelled" },
] as const;

export type BoardColumn = (typeof BOARD_COLUMNS)[number]["key"];

export function boardColumn(entry: ScheduledMessage): BoardColumn {
  const tone = stateTone(entry);
  if (tone === "error" || tone === "missed") return "attention";
  if (tone === "cancelled" || tone === "skipped") return "cancelled";
  // `sending` is a sent message whose turn is still working — news, not a plan.
  if (entry.state === "sent" || tone === "sending") return "ran";
  return "upcoming";
}

// A human reading of a 5-field cron line, for the handful of shapes the New job
// form itself writes; anything else is shown verbatim. Deliberately not a cron
// parser — the server owns the real one, and a wrong-but-confident English
// reading of an expression this function does not understand would be worse
// than showing the expression.
const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

export function describeRepeats(repeats: string): string {
  const m = repeats.trim().split(/\s+/);
  if (m.length !== 5) return repeats;
  const [min, hour, dom, mon, dow] = m;
  const pad = (v: string) => v.padStart(2, "0");
  if (dom === "*" && mon === "*") {
    if (hour === "*" && dow === "*" && /^\d+$/.test(min))
      return `hourly at :${pad(min)}`;
    if (/^\d+$/.test(min) && /^\d+$/.test(hour)) {
      const at = `${pad(hour)}:${pad(min)}`;
      if (dow === "*") return `daily at ${at}`;
      if (/^[0-7]$/.test(dow)) return `${DAY_NAMES[Number(dow) % 7]}s at ${at}`;
    }
  }
  return repeats;
}
