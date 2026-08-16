// Shared vocabulary for the two schedule views (Scheduled.tsx list cards,
// ScheduleCalendar.tsx week grid): how an entry's pair of facts — `state` (did
// it send) and `turn` (how did the session go) — collapses into one label and
// one tone. Split out of Scheduled.tsx when the calendar arrived, so the two
// views cannot drift into describing the same entry differently.
import type {
  RecurrenceRule,
  ScheduledMessage,
  ScheduledState,
  Task,
  TaskMessage,
} from "@platform/lib/api";

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

// ---- what `turn` means -------------------------------------------------------
// `turn` is the second half of the pair, and it is what both this file and
// tasks-lib keep getting wrong in the same way — so the rule lives in ONE place
// and both read it from here.
//
// The two shapes spell the field differently — a ScheduledMessage's turn is
// "" | "ok" | "failed" | "cancelled" | "unknown", a TaskMessage's is
// "" | "done" | "idle" | "unknown" — but the POSITIONAL rule under both is the
// same, because the field is written exactly once, when the turn ends:
//
//   ""         nothing written yet ⇒ the turn is still live
//   "unknown"  the watch ended without a verdict ⇒ nobody is reporting any more
//   anything   the turn ENDED and said so — "done", "idle", "ok", "failed", and
//   else       whatever word a newer server invents next
//
// The default is the whole point. A value this client has not heard of is a
// turn that ended, never one in flight, because the server does not write the
// field until it ends. Defaulting the other way is what made `idle` — a turn
// that finished and reported — paint as "Running…" on the calendar for as long
// as the row existed.
export type TurnPhase = "running" | "ended" | "unreported";

export function turnPhase(turn: string | undefined | null): TurnPhase {
  if (!turn) return "running";
  if (turn === "unknown") return "unreported";
  return "ended";
}

// Not finished with: waiting for its time, being sent, sent with a turn still
// running — or a recurring rule, which is never finished with by nature. The
// sent-but-running case is why this is not just a `state` check.
export const isLive = (e: ScheduledMessage) =>
  e.state === "pending" ||
  e.state === "sending" ||
  e.state === "recurring" ||
  (e.state === "sent" && turnPhase(e.turn) === "running");

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
  // catch-up verdict (SCH-13 / D296 — the store's error text already says
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
    // Only an EMPTY turn is still going. A word this build has never heard of
    // is a turn that ENDED without us knowing what to call it (turnPhase), and
    // it says "Sent" — the honest half of the pair — rather than claiming work
    // is in flight forever.
    return turnPhase(entry.turn) === "running" ? "Running…" : STATE_LABELS.sent;
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
  // Same rule as stateLabel's: only an empty turn is in flight. An unrecognised
  // word falls through to `sent`, the neutral tone — never to `sending`.
  if (entry.state === "sent" && turnPhase(entry.turn) === "running") return "sending";
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
    if (entry.state === "cancelled" ||
        (entry.state === "missed" && entry.template_id)) {
      // A skipped run stays visible only while the schedule it is an
      // exception TO still exists — a dead template's skips are just history
      // (the list keeps them), and on the grid they were immortal clutter
      // with an Unskip that could only 404. The rule covers BOTH kinds of
      // skip — the user's (cancelled occurrence) and the loop's (missed
      // occurrence, SCH-13) — because the label calls them the same thing,
      // so the grid must retire them the same way. A plain cancelled
      // one-shot is off the calendar the way a deleted event is off
      // Google's; a missed ONE-SHOT stays, being a fault worth seeing.
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

// ---- Calendar lane packing ----------------------------------------------------
// Overlapping chips split a day column side-by-side (Google's week view). A
// chip occupies CHIP_MIN minutes of column; two chips overlap exactly when
// they are closer than that. Lanes are REUSED the way an interval coloring
// does — the first lane whose previous chip has ended takes the newcomer —
// so a third event that only overlaps the second slides back to lane 0
// instead of widening the whole cluster (Bugbot, PR #538). `lanes` is the
// cluster's width: every chip in one overlapping run shares it, and a
// cluster closes when a gap of CHIP_MIN goes by with nothing on screen.

export const CHIP_MIN = 30;

export function assignLanes<T extends { time: Date }>(
  events: T[],
): (T & { lane: number; lanes: number })[] {
  const sorted = [...events].sort((a, b) => a.time.getTime() - b.time.getTime());
  const out: (T & { lane: number; lanes: number })[] = [];
  const laneEnds: number[] = [];
  let clusterStart = 0;
  let clusterEnd = -Infinity;
  let clusterLanes = 0;
  const closeCluster = (upto: number) => {
    for (let j = clusterStart; j < upto; j++) out[j].lanes = clusterLanes;
  };
  for (let i = 0; i < sorted.length; i++) {
    const t = sorted[i].time.getTime();
    if (t >= clusterEnd && i > clusterStart) {
      closeCluster(i);
      clusterStart = i;
      clusterLanes = 0;
      laneEnds.length = 0;
    }
    let lane = laneEnds.findIndex((end) => t >= end);
    if (lane === -1) lane = laneEnds.length;
    laneEnds[lane] = t + CHIP_MIN * 60000;
    clusterLanes = Math.max(clusterLanes, lane + 1);
    clusterEnd = Math.max(clusterEnd, laneEnds[lane]);
    out.push({ ...sorted[i], lane, lanes: 1 });
  }
  closeCluster(out.length);
  return out;
}

// ---- Board (kanban) columns ---------------------------------------------------
// The Schedule page's third view — the Inbox board's EXACT columns (In
// Progress / Done / Archive) plus Upcoming, because this board now shows every
// Claude session alongside the scheduler's tasks (Akshil, 2026-08-16). A task
// column derives strictly from fields that exist — `state` and `turn` —
// through the same collapse stateTone performs, so a card sits in the column
// its pill already claims. No drag: unlike the Inbox's triage status, these
// states are the scheduler's own facts, not labels a person may move.

export const BOARD_COLUMNS = [
  { key: "upcoming", label: "Upcoming" },
  { key: "in_progress", label: "In Progress" },
  { key: "done", label: "Done" },
  { key: "archived", label: "Archive" },
] as const;

export type BoardColumn = (typeof BOARD_COLUMNS)[number]["key"];

export function boardColumn(entry: ScheduledMessage): BoardColumn {
  const tone = stateTone(entry);
  // The user's skip and the loop's skip are both filed away, like the Inbox's
  // Archive — not outcomes to keep reading.
  if (tone === "cancelled" || tone === "skipped") return "archived";
  // `sending` is a sent message whose turn is still working — work happening
  // NOW, which is exactly what In Progress means for a session too.
  if (tone === "sending") return "in_progress";
  // Every settled outcome lands in Done; the pill still says HOW it settled
  // (Ran / Turn failed / Missed), so folding them loses no fact.
  if (entry.state === "sent" || tone === "error" || tone === "missed") return "done";
  return "upcoming";
}

// A Claude session's own status, in the board's vocabulary. The server already
// speaks it (api.ClaudeSessionSummary.status), so this is a narrowing, not a
// derivation — its job is to keep an unrecognised value (an older server, a
// status added later) off the board's floor rather than silently dropping the
// session. "Done" is where an unknown lands, not "Archive": a session the
// client cannot read is still a session that HAPPENED, and filing it away
// hides it behind a collapsed lane.
export function sessionColumn(status: string): BoardColumn {
  if (status === "in_progress" || status === "done" || status === "archived")
    return status;
  return "done";
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

// ---- Structured recurrence wording (Google Calendar's, copied deliberately) --
// The cron sentences above ("daily at 09:00") read as machine output; Google's
// read as answers to "when does this happen?" (Akshil, 2026-08-15). These are
// pure so the select's labels, the cards and the popover all say the same words.

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
const NTH_NAMES = ["first", "second", "third", "fourth", "fifth"];

// Which Wednesday of its month a date is — the 12th is the SECOND Wednesday.
export function nthOfMonth(d: Date): number {
  return Math.floor((d.getDate() - 1) / 7) + 1;
}

// "Nov 11, 2026" — how an end date reads in a repeat sentence.
function shortDate(ymd: string): string {
  const [y, m, day] = ymd.split("-").map(Number);
  if (!y || !m || !day) return ymd;
  return `${MONTH_NAMES[m - 1].slice(0, 3)} ${day}, ${y}`;
}

const WEEKDAYS_1_TO_5 = [1, 2, 3, 4, 5];

export function describeRule(rule: RecurrenceRule, anchor: Date): string {
  const n = rule.interval ?? 1;
  const every = (unit: string) => (n === 1 ? "" : `Every ${n} ${unit}s`);
  let base: string;
  switch (rule.freq) {
    case "hour":
      base = n === 1 ? "Hourly" : every("hour");
      break;
    case "day":
      base = n === 1 ? "Daily" : every("day");
      break;
    case "week": {
      const days = (rule.byday?.length ? rule.byday : [anchor.getDay()])
        .slice()
        .sort((a, b) => a - b);
      const names = days.map((d) => DAY_NAMES[d]).join(", ");
      if (n === 1 && days.length === 5 && days.every((d, i) => d === WEEKDAYS_1_TO_5[i])) {
        base = "Every weekday (Monday to Friday)";
      } else if (n === 1) {
        base = `Weekly on ${names}`;
      } else {
        base = `${every("week")} on ${names}`;
      }
      break;
    }
    case "month": {
      const what =
        rule.monthly === "nth-weekday"
          ? `the ${NTH_NAMES[nthOfMonth(anchor) - 1]} ${DAY_NAMES[anchor.getDay()]}`
          : `day ${anchor.getDate()}`;
      base = n === 1 ? `Monthly on ${what}` : `${every("month")} on ${what}`;
      break;
    }
    case "year": {
      const what = `${MONTH_NAMES[anchor.getMonth()]} ${anchor.getDate()}`;
      base = n === 1 ? `Annually on ${what}` : `${every("year")} on ${what}`;
      break;
    }
  }
  if (rule.until) return `${base}, until ${shortDate(rule.until)}`;
  if (rule.count) return `${base}, ${rule.count} ${rule.count === 1 ? "time" : "times"}`;
  return base;
}

// One sentence for whatever kind of repeat an entry carries — the rule's
// Google wording when it has one, the legacy cron reading otherwise.
export function entryRepeatText(entry: ScheduledMessage): string {
  if (entry.rule) return describeRule(entry.rule, new Date(entry.due));
  return describeRepeats(entry.repeats || "");
}

// The repeat select's derived choices, Google's list verbatim: every label is
// read off the picked date, so recurrence needs no fields of its own until
// Custom. Returned as data so the modal renders and the tests read the same
// list.
export interface RepeatChoice {
  key: string;
  label: string;
  rule: RecurrenceRule | null; // null = does not repeat
}

export function repeatChoicesFor(picked: Date): RepeatChoice[] {
  const dow = picked.getDay();
  return [
    { key: "none", label: "Does not repeat", rule: null },
    // Shortest first, the order recur.FREQUENCIES is written in: "Hourly" sits
    // between not repeating and repeating daily because that is where a reader
    // scanning by how-often expects to find it.
    { key: "hourly", label: "Hourly", rule: { freq: "hour" } },
    { key: "daily", label: "Daily", rule: { freq: "day" } },
    {
      key: "weekly",
      label: `Weekly on ${DAY_NAMES[dow]}`,
      rule: { freq: "week", byday: [dow] },
    },
    {
      key: "monthly",
      label: `Monthly on the ${NTH_NAMES[nthOfMonth(picked) - 1]} ${DAY_NAMES[dow]}`,
      rule: { freq: "month", monthly: "nth-weekday" },
    },
    {
      key: "annually",
      label: `Annually on ${MONTH_NAMES[picked.getMonth()]} ${picked.getDate()}`,
      rule: { freq: "year" },
    },
    {
      key: "weekday",
      label: "Every weekday (Monday to Friday)",
      rule: { freq: "week", byday: WEEKDAYS_1_TO_5 },
    },
    { key: "custom", label: "Custom…", rule: null },
  ];
}

// "Open in Explorer" — the folder the job ran against, with the Claude pane
// already holding that run's conversation (Akshil, 2026-08-16 — replaced
// "Open in Inbox": the inbox showed the chat but not the files it was
// about). Same /view codec + `_side=claude` handoff the Inbox's own
// open-dir button uses (core_apps/sessions/inbox.html).
export function explorerUrl(target: string, sessionId: string): string {
  const norm = /^[A-Za-z]:[\\/]/.test(target) ? target.replace(/\\/g, "/") : target;
  const encoded = norm.replace(/^\/+/, "").split("/")
    .filter(Boolean).map(encodeURIComponent).join("/");
  return `/explorer/view/${encoded}?_side=claude&session_id=${encodeURIComponent(sessionId)}`;
}

// ---- Calendar: the task chip grid ---------------------------------------------
// The calendar shows the same unit the List and the Board show — a TASK — and
// what the time axis adds is placement:
//
//   ONE CHIP PER TASK PER DAY, anchored at that task's EARLIEST message that
//   day; later messages the same day nest inside it and the anchor carries the
//   count.
//
// Only `scheduled` messages place a chip: a chat message has no schedule, so it
// has nothing to sit on. Everything below is pure — the component adds pixels
// and nothing else — because these are the rules that are worth testing and the
// ones a DOM makes untestable (DST days, midnight straddles, an hourly rule).
//
// All day arithmetic is done through LOCAL calendar fields (getFullYear /
// getMonth / getDate), never by adding 86_400_000ms: on the two DST days of the
// year a day is 23 or 25 hours long, and millisecond arithmetic slides a chip
// onto the wrong column.

export type CalendarRange = "week" | "4day";

// Google Calendar's two ranges. "4 days" earns its place on width: four columns
// are wide enough to read a task title, which a seven-column week is not.
export const RANGE_DAYS: Record<CalendarRange, number> = { week: 7, "4day": 4 };

export function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

// Monday-first, which is what a work schedule reads like; getDay() is
// Sunday-first, hence the +6 dance.
export function startOfWeek(d: Date): Date {
  const out = startOfDay(d);
  out.setDate(out.getDate() - ((out.getDay() + 6) % 7));
  return startOfDay(out);
}

// n days on from `d`, at local midnight. Field arithmetic, so a 23-hour or
// 25-hour day still advances by exactly one column.
export function addDays(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
}

// Where a range anchored on `anchor` begins. The week snaps to its Monday; the
// 4-day range does NOT snap to anything — its whole point is that the day you
// are on is the leftmost column.
export function rangeStart(anchor: Date, range: CalendarRange): Date {
  return range === "week" ? startOfWeek(anchor) : startOfDay(anchor);
}

export function rangeDays(start: Date, range: CalendarRange): Date[] {
  return Array.from({ length: RANGE_DAYS[range] }, (_, i) => addDays(start, i));
}

// The arrows step by a whole range: seven days in the week view, FOUR in the
// 4-day view (Google's behaviour — the arrows move the window, not a day).
export function stepRange(start: Date, range: CalendarRange, delta: number): Date {
  return addDays(start, delta * RANGE_DAYS[range]);
}

// Local `YYYY-MM-DD`. The grid's day identity, and deliberately not an ISO
// instant: 23:50 and 00:10 are two days apart to a reader looking at a wall
// clock, whichever side of UTC midnight they fall.
export function dayKey(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Where in its column a chip sits, in minutes past local midnight. Read off the
// LOCAL clock, so on a spring-forward day 09:00 is still the 9am line even
// though only 23 hours have elapsed since midnight.
export function minutesOfDay(d: Date): number {
  return d.getHours() * 60 + d.getMinutes();
}

export function sameDay(a: Date, b: Date): boolean {
  return dayKey(a) === dayKey(b);
}

// "August 2026", spanning as "Aug – Sep 2026" when the window straddles a
// month — the label Google Calendar puts beside its arrows.
export function rangeLabel(days: Date[]): string {
  if (!days.length) return "";
  const first = days[0];
  const last = days[days.length - 1];
  if (first.getMonth() === last.getMonth() && first.getFullYear() === last.getFullYear())
    return first.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  return (
    `${first.toLocaleDateString(undefined, { month: "short" })} – ` +
    last.toLocaleDateString(undefined, { month: "short", year: "numeric" })
  );
}

// ---- Task colour ---------------------------------------------------------------
// Same task, same colour, everywhere on the grid — that is what makes five days
// of a daily task read as ONE thing rather than five unrelated boxes. The colour
// is derived from the task key so it needs no storage and cannot drift between
// renders, and it indexes a small HAND-PICKED palette (styles/tokens.css,
// --task-c0…--task-c7) rather than generating an hsl(): an arbitrary hue clashes
// with the theme in one mode or the other, and the eight tuned ones do not.

export const TASK_COLOURS = 8;

export function taskColour(key: string): number {
  // FNV-1a, 32-bit. Cheap, well spread over short ASCII keys, and — unlike
  // summing char codes — it does not collide on anagrams, which task keys
  // (session uuids, `pending:<id>`) are full of.
  let h = 2166136261;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) % TASK_COLOURS;
}

// ---- Message and day tone -------------------------------------------------------

// A projected occurrence the server computed but has not materialized into a
// message yet (see projectedMessages). Marked by its id rather than a new field
// because TaskMessage is the server's shape and the client does not get to
// widen it.
export const GHOST_PREFIX = "GHOST-";

export const isProjected = (m: TaskMessage) => m.message_id.startsWith(GHOST_PREFIX);

// How a single message paints ON THE CALENDAR. `state` says whether the message
// went out; `turn` says how the session it started then went — two facts that
// fail independently, so a sent message with a dead turn must not read as clean.
//
// NOT the same function as tasks-lib.messageTone, and deliberately so — see the
// note there. What the two DO share is the reading of `turn`, and that half is
// turnPhase above, imported by both, so the calendar and the list can no longer
// disagree about whether a run is still going.
export function messageTone(m: TaskMessage): string {
  switch (m.state) {
    case "pending":
      return "upcoming";
    case "sending":
      return "sending";
    case "sent":
      switch (turnPhase(m.turn)) {
        // Nothing is watching it any more; "Running" would be the frozen
        // progress bar.
        case "unreported":
          return "error";
        case "running":
          return "sending";
        // Ended and said so — "done", "idle", or a word a newer server added.
        default:
          return "ran";
      }
    case "error":
      return "error";
    case "missed":
      return "missed";
    default:
      return "skipped";
  }
}

// One chip carries a whole day of a task, so its tone has to answer for all of
// them. Failures win, then work in flight, then work still coming: a day whose
// 9am ran fine and whose 2pm died must not read green.
const TONE_RANK = ["error", "missed", "sending", "upcoming", "skipped", "ran"];

export function dayTone(messages: TaskMessage[]): string {
  let best = "ran";
  for (const m of messages) {
    const tone = messageTone(m);
    if (TONE_RANK.indexOf(tone) < TONE_RANK.indexOf(best)) best = tone;
  }
  return best;
}

// ---- Chips ----------------------------------------------------------------------

export interface CalendarChip {
  key: string;
  day: string; // dayKey of the column it belongs to
  task: Task;
  // The earliest scheduled message that day — what the chip is placed at.
  anchor: TaskMessage;
  time: Date;
  // Every scheduled message that day, earliest first (the anchor included).
  messages: TaskMessage[];
  // How many messages nest INSIDE the anchor — the chip's `+N`. An hourly rule
  // is 23 here, not 23 extra chips.
  extra: number;
  recurring: boolean;
  // The recurring rule these runs are occurrences OF; "" for a one-off. Carried
  // so the chip can NAME its recurrence ("Daily") instead of only flagging it:
  // a ↻ is a glyph, and a glyph is not an accessible name.
  templateId: string;
  colour: number;
  tone: string;
  // Nothing on this day has run yet and nothing is left to run — a fully
  // projected day, drawn as a forecast rather than a commitment.
  projected: boolean;
}

// The whole rule, in one pass. `threads` is an optional override for tasks whose
// FULL message list the caller has fetched (GET /api/tasks ships only the last
// few per task); without it the chips are built from what each task carries.
export function taskChips(
  tasks: Task[],
  days: Date[],
  threads: Record<string, TaskMessage[]> = {},
): Map<string, CalendarChip[]> {
  const out = new Map<string, CalendarChip[]>();
  for (const day of days) out.set(dayKey(day), []);
  for (const task of tasks) {
    const messages = threads[task.key] ?? task.messages ?? [];
    const byDay = new Map<string, TaskMessage[]>();
    const seen = new Set<string>();
    for (const m of messages) {
      if (m.kind !== "scheduled") continue;
      if (seen.has(m.message_id)) continue;
      const t = new Date(m.at * 1000);
      if (Number.isNaN(t.getTime())) continue;
      const key = dayKey(t);
      if (!out.has(key)) continue;
      seen.add(m.message_id);
      const list = byDay.get(key);
      if (list) list.push(m);
      else byDay.set(key, [m]);
    }
    for (const [key, list] of byDay) {
      list.sort((a, b) => a.at - b.at);
      const anchor = list[0];
      // The anchor is not necessarily the recurring one — a one-off at 5am can
      // hold the chip for a daily rule that runs at 9 — so the rule is looked
      // for across the day, not read off the anchor.
      const templateId = list.find((m) => m.template_id)?.template_id ?? "";
      out.get(key)!.push({
        key: `${task.key}@${key}`,
        day: key,
        task,
        anchor,
        time: new Date(anchor.at * 1000),
        messages: list,
        extra: list.length - 1,
        recurring: !!templateId,
        templateId,
        colour: taskColour(task.key),
        tone: dayTone(list),
        projected: list.every(isProjected),
      });
    }
  }
  for (const list of out.values())
    list.sort((a, b) => a.time.getTime() - b.time.getTime());
  return out;
}

// The recurrence a chip is an occurrence of, IN WORDS — "Daily", "Every 2
// weeks on Monday, Wednesday". Reads the rule off the template entry and hands
// it to entryRepeatText, which is the app's single source for this wording
// (describeRule for a structured rule, the legacy cron reading otherwise): the
// calendar must never grow a second dialect for the same fact.
export function repeatTextFor(
  templateId: string,
  entries: ScheduledMessage[],
): string {
  if (!templateId) return "";
  const template = entries.find((e) => e.id === templateId);
  if (!template || (!template.rule && !template.repeats)) return "";
  return entryRepeatText(template);
}

// A chip's ACCESSIBLE NAME, composed rather than left to fall out of whatever
// text happens to be visible.
//
// Three things go wrong when a chip has no name of its own (audit 2026-08-17):
// the ↻ glyph needs an aria-label to mean anything, that label is a bare verb
// ("Repeats") that says nothing about HOW it repeats, and — because a label is
// a global string — seventeen chips end up answering to the same name as the
// New task form's recurrence dropdown, so anything addressing that control by
// label hits a chip instead. Naming the chip here fixes all three: the glyph
// goes decorative, the recurrence is spoken in words, and the name is stable
// even when CSS hides the time in a narrow lane.
export function chipAccessibleName(
  title: string,
  repeat: string,
  time: string,
  later: string[] = [],
): string {
  // Recurrence before clock time, matching how the popover reads: what kind of
  // thing this is, then when it happens.
  const when = [repeat, time].filter(Boolean).join(", ");
  const also = later.length ? `, also ${later.join(", ")}` : "";
  if (!title) return `${when}${also}`;
  return when ? `${title} — ${when}${also}` : `${title}${also}`;
}

// Future occurrences of a recurring rule exist only as server-side projections
// (`upcoming` on the recurring template — see server/routers/schedule.py); they
// are not messages yet, so a task's thread cannot carry them and the grid past
// the next materialized run would otherwise be empty. This turns them into
// synthetic messages on the task that owns the rule, deduped to the minute
// against what the thread already holds so the next run is never drawn twice.
// `base` is the thread each task is known to have in the visible window (the
// windowed endpoint's answer). It matters for the DEDUPE, not just the seed: a
// materialized occurrence the listing's three-message tail never mentioned is
// still a run the grid must not draw twice, and only the windowed thread knows
// about it.
export function projectedMessages(
  tasks: Task[],
  entries: ScheduledMessage[],
  base: Record<string, TaskMessage[]> = {},
): Record<string, TaskMessage[]> {
  const out: Record<string, TaskMessage[]> = {};
  for (const entry of entries) {
    if (entry.state !== "recurring" || !entry.upcoming?.length) continue;
    // WHICH task a projection hangs on is not obvious, and picking the first
    // match is wrong: under "new task each run" (§6) every past occurrence of
    // this rule is its OWN task, so a plain find() lands on whichever the
    // listing happened to sort first. Future runs belong to the task that has
    // not run yet — the `pending:<entry>` shell §5 keeps for exactly this — so
    // that is preferred explicitly, and a task with a session is the fallback
    // (the chained case, where every occurrence shares one task anyway).
    const claims = tasks.filter((t) =>
      (base[t.key] ?? t.messages ?? []).some(
        (m) => m.template_id === entry.id || m.entry_id === entry.id,
      ),
    );
    const owner =
      claims.find((t) => !t.session_id) ??
      claims[0] ??
      // A rule that has never run at all has no message to match on; the
      // server keys its task off the entry until the first turn mints a
      // session (§5).
      tasks.find((t) => t.key === `pending:${entry.id}`);
    if (!owner) continue;
    const list =
      out[owner.key] ??
      (out[owner.key] = [...(base[owner.key] ?? owner.messages ?? [])]);
    const taken = new Set(
      list.filter((m) => m.kind === "scheduled").map((m) => Math.floor(m.at / 60)),
    );
    for (const iso of entry.upcoming) {
      const t = new Date(iso);
      if (Number.isNaN(t.getTime())) continue;
      const at = Math.floor(t.getTime() / 1000);
      const minute = Math.floor(at / 60);
      if (taken.has(minute)) continue;
      taken.add(minute);
      list.push({
        message_id: `${GHOST_PREFIX}${iso}`,
        kind: "scheduled",
        body: entry.message,
        at,
        state: "pending",
        unread: false,
        entry_id: entry.id,
        template_id: entry.id,
        turn: "",
        anchor: "",
      });
    }
  }
  return out;
}

// The `threads` argument taskChips takes, assembled from the two feeds that
// know anything about a window:
//
//   * `windowed` — GET /api/tasks/scheduled, every scheduled message in the
//     visible days. Authoritative and complete, and the reason the grid stops
//     under-drawing: the listing ships only each task's three most recent.
//   * `entries`  — the recurring templates, whose `upcoming` projections are
//     not messages yet and so appear in neither feed.
//
// Projections are layered ON TOP of the windowed thread (they were seeded from
// it), so spreading them over it is a merge, not a clobber. A task in neither
// gets no entry at all, which is what makes taskChips fall back to its own
// `task.messages` — the degraded-but-useful grid when the window fetch fails.
export function calendarThreads(
  tasks: Task[],
  entries: ScheduledMessage[],
  windowed: Record<string, TaskMessage[]> | null,
): Record<string, TaskMessage[]> {
  const base = windowed ?? {};
  return { ...base, ...projectedMessages(tasks, entries, base) };
}

// The windowed endpoint answers flat — one row per (task, message) — because
// that is the cheap shape to produce. The grid wants it per task.
export function groupScheduled(
  items: { task_key: string; message: TaskMessage }[],
): Record<string, TaskMessage[]> {
  const out: Record<string, TaskMessage[]> = {};
  for (const item of items) {
    const list = out[item.task_key] ?? (out[item.task_key] = []);
    list.push(item.message);
  }
  return out;
}

// The window to ask the server for, in epoch seconds: local midnight of the
// first visible day, to local midnight AFTER the last one. Exclusive `to` is
// the boundary that matters — a 23:59 run on the last column is inside the
// window, and a `to` of that day's own midnight would drop it.
export function windowBounds(days: Date[]): { from: number; to: number } {
  if (!days.length) return { from: 0, to: 0 };
  const first = startOfDay(days[0]);
  const past = addDays(days[days.length - 1], 1);
  return {
    from: Math.floor(first.getTime() / 1000),
    to: Math.floor(past.getTime() / 1000),
  };
}

// The popover's list: THAT DAY's messages first, earliest first and with their
// real times — this is how the 7pm run stays reachable when the 5am run is the
// one holding the chip — then the rest of the thread, newest first (the app's
// standing order everywhere else).
export function threadForDay(
  messages: TaskMessage[],
  day: string,
): { today: TaskMessage[]; rest: TaskMessage[] } {
  const today: TaskMessage[] = [];
  const rest: TaskMessage[] = [];
  for (const m of messages) {
    const t = new Date(m.at * 1000);
    if (!Number.isNaN(t.getTime()) && dayKey(t) === day) today.push(m);
    else rest.push(m);
  }
  today.sort((a, b) => a.at - b.at);
  rest.sort((a, b) => b.at - a.at);
  return { today, rest };
}

// ---- The queued strip -----------------------------------------------------------
// Nothing fires while the app is closed and one-off catch-up is now unbounded,
// so opening after a week away can find real work waiting. `queued` is past due
// and unclaimed, `running` is mid-flight; the strip draws both and is the cancel
// surface for them.

// One line for a prompt: the calendar reads a message by its first line, and a
// pasted three-paragraph prompt must not become a three-line chip.
export function firstLine(text: string): string {
  const line = (text ?? "").split("\n").find((l) => l.trim().length);
  return (line ?? "").trim();
}

export function queueSummary(
  queued: ScheduledMessage[],
  running: ScheduledMessage[],
): string {
  const lead = running[0] ?? queued[0];
  if (!lead) return "";
  const head = firstLine(lead.message) || "Scheduled message";
  // What is running is named; what is behind it is counted. Two numbers in one
  // line ("1 running, 3 waiting") reads as a dashboard; this reads as a fact.
  if (running.length && queued.length)
    return `${head} · ${queued.length} waiting`;
  if (running.length > 1) return `${head} · ${running.length - 1} more running`;
  if (queued.length > 1) return `${head} · ${queued.length - 1} more waiting`;
  return head;
}

// Cancelling races the claim, and the server resolves it honestly: an entry it
// has already handed to the sender comes back REFUSED, not cancelled. Saying so
// is the whole point — a silent drop teaches the user the button lies.
export function cancelOutcome(cancelled: string[], refused: string[]): string {
  if (!refused.length) return "";
  const n = refused.length;
  if (!cancelled.length)
    return n === 1
      ? "Already running — too late to cancel."
      : `${n} were already running — too late to cancel.`;
  return n === 1
    ? `Cancelled ${cancelled.length}; 1 was already running.`
    : `Cancelled ${cancelled.length}; ${n} were already running.`;
}
