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
//
// It separates TWO RUNS OF ONE TASK as readily as two different tasks, and has
// since chips became per-message: a rule firing every 15 minutes is two lanes of
// the same colour. Hourly and sparser never overlap at all — 60 minutes clears
// CHIP_MIN — so the common dense case stays a single full-width column.

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

// FIVE, not four (Akshil, 2026-08-17): "if we can't show failed tasks, then
// let's have a failed status, and show it everywhere. It should just be
// consistent." Failure used to be a red ring inside Done — a visual with no
// word — which meant the calendar had to keep its own vocabulary to say what
// had happened. It says it here instead, once, for every view.
export const BOARD_COLUMNS = [
  { key: "upcoming", label: "Upcoming" },
  { key: "in_progress", label: "In Progress" },
  { key: "done", label: "Done" },
  { key: "failed", label: "Failed" },
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
  // Settled BADLY has its own word since 2026-08-17, and every view says it.
  if (tone === "error" || tone === "missed") return "failed";
  if (entry.state === "sent") return "done";
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

// The same door, for a task that has no thread to open YET.
//
// tasks-lib.taskHref is null until a task has a session id, and that null is
// what left a run IN FLIGHT unreachable (Akshil, 2026-08-17: a run parked on a
// permission prompt, and no way to get to it). The hole is structural rather
// than a race worth waiting out — a scheduled run is claimed and sent before the
// watcher learns which Claude session it opened, so for the whole of that window
// the task reads `in_progress`, its key is still `pending:<entry>`, and its
// session id is "". Hiding the only way in during exactly the minutes somebody
// needs it is the wrong trade, so the popover's footer falls back to this.
//
// What it opens is the run's own FOLDER with the Claude pane on it: the same
// `_side=claude` hop, carrying an empty `session_id`, which is precisely the hop
// the chat's own composer makes when there is no session yet. It lands on that
// folder's sessions — where the run in question shows up the moment it reports
// one — instead of on nothing.
export function folderHref(task: Task): string | null {
  const target = task.target || task.project;
  return target ? explorerUrl(target, "") : null;
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

/**
 * The same question asked of an ID ALONE, for the callers that hold a pointer to
 * a message rather than the message.
 *
 * tasks-lib's run intents are the case: they carry the `messageId` they act on,
 * and a ghost's "entry id" is the RECURRING RULE's, not a message's — spending
 * it would fire something the calendar only ever DREW. Nothing may turn a
 * projection into a run (§9: catch-up materializes exactly one past slot and
 * drops the rest for good), so the check has to be available where the id is.
 */
export const isProjectedId = (messageId: string) => messageId.startsWith(GHOST_PREFIX);

export const isProjected = (m: TaskMessage) => isProjectedId(m.message_id);

// How a single message PAINTS on the calendar — a CSS class, and since
// 2026-08-17 nothing but a CSS class. Every word the calendar puts on screen now
// comes from the four-column vocabulary below (runStatus), so this one is free
// to keep the finer distinctions the words fold away: `missed` is amber where
// the word says Done, `skipped` is struck through where the word says Archive.
//
// `state` says whether the message went out; `turn` says how the session it
// started then went — two facts that fail independently, so a sent message with
// a dead turn must not read as clean. What this shares with
// tasks-lib.messageTone is the reading of `turn`: that half is turnPhase above,
// imported by both, so the two can no longer disagree about whether a run is
// still going.
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

// ---- Chips ----------------------------------------------------------------------
//
// ONE CHIP PER SCHEDULED MESSAGE, at its own time. A task that runs at 3am, 5am
// and 7am draws three chips on the 3rd, 5th and 7th hour lines.
//
// This reverses the rule the calendar shipped with — one chip per task per day,
// anchored at the day's earliest run, later runs folded into a `+N` — which was
// tried and rejected in review (Akshil, 2026-08-17: "if I have a task at 3 a.m.,
// then at 5 a.m., and then 7 a.m. As of now, we only show the 3 am task... I want
// all these three to show"). A time axis whose whole job is to say WHEN cannot
// leave two of three runs off the axis; a badge that names 5 AM and 7 AM in a
// tooltip is not placement, and the reader has to open a popover to learn that
// anything happens after breakfast.
//
// What keeps the grid from reading as three unrelated things is unchanged and
// carries the whole weight of the trade: chips of one task share ONE COLOUR and
// the ↻ marker, so three chips at 3/5/7 read as one task seen three times. And
// CLICKING ANY OF THEM OPENS THE SAME DAY-SCOPED THREAD — which is why each chip
// still carries its day's full message list (Akshil: "when you click on them you
// just see the thread as is no change").
//
// THE VOLUME IS NOW INTENDED. An hourly rule draws ~24 chips in a day where it
// drew one, and at HOUR_H = 44px against a 21px chip they sit one to an hour line
// with room to spare. Denser than every 30 minutes and lane packing (assignLanes,
// CHIP_MIN) splits them side by side, exactly as it already does for unrelated
// tasks that start together.

export interface CalendarChip {
  key: string;
  day: string; // dayKey of the column it belongs to
  task: Task;
  // The one scheduled message this chip IS, and what it is placed at.
  message: TaskMessage;
  time: Date;
  // Every scheduled message this task has that day, earliest first, INCLUDING
  // this chip's own. Not what the chip draws — what the popover opens: every
  // chip of a day hands over the same list, so all three of a 3/5/7 task open
  // the identical thread.
  messages: TaskMessage[];
  recurring: boolean;
  // The recurring rule this run is an occurrence OF; "" for a one-off. Carried
  // so the chip can NAME its recurrence ("Daily") instead of only flagging it:
  // a ↻ is a glyph, and a glyph is not an accessible name.
  templateId: string;
  colour: number;
  tone: string;
  // This run is not a real message — it is cron arithmetic, drawn rather than
  // written down. True in both directions: a slot ahead of the next materialized
  // run, and a slot BEHIND now that a past-anchored rule went by and will never
  // fill. Either way it is outlined rather than filled, because the calendar has
  // nothing recorded to point at.
  projected: boolean;
}

// An ARCHIVED task draws NO CHIP AT ALL. Three chips at one time, two of them
// struck through, read as noise on the grid (Akshil, 2026-08-17) — and they were
// three genuinely different tasks, so how many chips a task draws was not the
// problem: the filed-away ones were. The calendar is for what is going to
// happen and what did; a task that was cancelled or skipped outright is neither,
// and the Board's Archive lane is where it is read.
//
// This is the TASK's status and not a RUN's, and the difference is the whole
// nuance: a live task with one skipped occurrence still draws its chip, and that
// skip shows as an Archive row inside the popover thread where it belongs.
export const isArchivedTask = (task: Task): boolean => task.status === "archived";

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
    if (isArchivedTask(task)) continue;
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
      for (const m of list) {
        // Each fact is now read off the MESSAGE, which is the point of the
        // change: the rule it belongs to (so a one-off at 5am no longer wears
        // the ↻ of a daily rule that happens to fire at 9 the same day), its own
        // tone (a 9am that ran clean stays clean beside a 2pm that died), and its
        // own projected flag (a materialized run and a ghost on one day are two
        // chips, drawn differently, rather than one chip that has to pick).
        const templateId = m.template_id ?? "";
        out.get(key)!.push({
          // The MESSAGE id, not the day: a day now holds several of these and
          // React needs them distinct. Ghost ids are their own ISO instant, so
          // projections are as stable across a poll as materialized runs.
          key: `${task.key}@${m.message_id}`,
          day: key,
          task,
          message: m,
          time: new Date(m.at * 1000),
          messages: list,
          recurring: !!templateId,
          templateId,
          colour: taskColour(task.key),
          tone: messageTone(m),
          projected: isProjected(m),
        });
      }
    }
  }
  for (const list of out.values())
    list.sort((a, b) => a.time.getTime() - b.time.getTime());
  return out;
}

// ---- Where the grid opens ------------------------------------------------------
//
// A 24-hour column is taller than any viewport, so the grid has to pick an hour
// to open on. That used to be a constant — just above 7am, the band a person
// actually schedules into — and it stopped being right the day the calendar
// started drawing a rule's whole past (2026-08-17).
//
// The collision: an hourly rule's first run of the day is at 00:00, seven hours
// above the fold, on a column that then reads as EMPTY until somebody scrolls up.
// That was a rare oddity while only future runs were drawn; past ghosts make it
// the common case. (It was worse under the old one-chip-per-day rule, where 00:00
// was the day's ONLY chip; a chip per run means there is now something at 07:00
// too, and the opening scroll is still the part worth getting right.)
//
// THE RULE, in order:
//
//   1. TODAY WINS, when today is in the range and has any chip at all. The
//      now-line is what a person looks for first, and it sits an hour down from
//      the top so there is context above it rather than a line flush to the edge.
//   2. Otherwise the EARLIEST chip in the range, half an hour above it. The whole
//      point is that the chip is on screen before anybody scrolls.
//   3. Otherwise the old constant, unchanged. An empty range has nothing to aim
//      at and must not invent something.
//
// Then the clamps, and it is worth being exact about which one does the work.
//
// The GRID'S OWN BOUNDS carry it: a target past the bottom of a 24-hour column
// lands at the bottom, which is how a day whose only chip is at 23:00 opens ON
// that chip instead of seven hours above it, and a negative target lands at
// midnight, which is how the 00:00 case above comes out right.
//
// The LAST-CHIP bound is deliberately weak, and saying so is the point. It pulls
// the target down far enough to keep the range's last chip on screen, but never
// past the earliest chip itself — so in practice all it can give up is the lead.
// Read it as: THE LEAD IS A COURTESY, AND IT IS THE FIRST THING SURRENDERED WHEN
// THE VIEWPORT IS TIGHT. It is not, and cannot be, a fix for a range whose chips
// simply do not fit in one screen.
//
// Which is the real trade, and it was a judgement call: a 00:00 chip and a 22:00
// chip cannot both be on a 13-hour viewport, and THE EARLIEST ONE WINS. The
// failure being fixed is a column that lies about being empty. Unused viewport is
// the smaller cost and the reader can undo it with a scroll; the other one they
// cannot, because nothing on screen tells them there is anything to scroll to.
//
// Placement is the CALLER's to trigger, and it belongs to the RANGE, not to the
// data: re-running this on every poll would yank a reader who has scrolled back
// to the top every twenty seconds. See ScheduleCalendar.

/** The old constant, and still the answer for a range with nothing in it. */
export const DEFAULT_SCROLL_HOUR = 7;

// The lead above whatever the grid aims at. An hour over the now-line (context
// above "now" is the point of a time axis); half an hour over a chip, which is
// only meant to lift it off the top edge.
const NOW_LEAD_MIN = 60;
const CHIP_LEAD_MIN = 30;

// One chip's height in px. Mirrors `.schedule-cal-chip { height: 21px }` in
// schedule.css and is only used to ask whether a chip's BOTTOM is still on
// screen; the wide range's extra 2px is not worth a second constant.
const CHIP_H = 21;

/**
 * Which pixel of the 24-hour column the scroller should open on.
 *
 * `chips` is every chip in the visible range, in any order — only each one's
 * `day` and its time of day matter, because all the columns share one axis.
 * `viewportH` is the scroller's own height; 0 means "not measured yet", which
 * makes the clamps degrade to the grid's bounds rather than misfire.
 */
export function scrollTarget(
  chips: { day: string; time: Date }[],
  days: Date[],
  now: Date,
  viewportH: number,
  hourH: number,
): number {
  const seen = Math.max(0, viewportH);
  const floor = Math.max(0, 24 * hourH - seen);
  const clamp = (n: number) => Math.max(0, Math.min(n, floor));
  const px = (minutes: number) => (minutes / 60) * hourH;

  if (!chips.length) return clamp(DEFAULT_SCROLL_HOUR * hourH - 12);

  const todayKey = dayKey(now);
  if (
    days.some((d) => dayKey(d) === todayKey) &&
    chips.some((c) => c.day === todayKey)
  )
    return clamp(px(minutesOfDay(now)) - px(NOW_LEAD_MIN));

  let first = minutesOfDay(chips[0].time);
  let last = first;
  for (const c of chips) {
    const m = minutesOfDay(c.time);
    if (m < first) first = m;
    if (m > last) last = m;
  }
  const aim = px(first) - px(CHIP_LEAD_MIN);
  // Far enough down that the last chip's bottom edge is still on screen.
  const keepLast = px(last) + CHIP_H - seen;
  // …but never past the earliest chip itself, which is the one being rescued.
  return clamp(Math.min(Math.max(aim, keepLast), px(first)));
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
// A fourth part used to hang off the end — `, also 5 AM, 7 AM`, naming the runs
// that had no chip of their own. Every run has its own chip now, so the list
// would name chips that are already on screen and already say their own time.
export function chipAccessibleName(
  title: string,
  repeat: string,
  time: string,
): string {
  // Recurrence before clock time, matching how the popover reads: what kind of
  // thing this is, then when it happens.
  const when = [repeat, time].filter(Boolean).join(", ");
  if (!title) return when;
  return when ? `${title} — ${when}` : title;
}

// ---- A rule's own occurrences, walked here ---------------------------------------
//
// The server projects a rule FORWARD only (`upcoming`, schedule.py) because
// nothing behind us will ever be MATERIALIZED: catch-up creates exactly one
// occurrence, at the most recent slot at or before now, and the slots before it
// are dropped for good. That is a fact about what RUNS, and the calendar was
// reading it as a fact about what to DRAW — so a repeat anchored last Saturday
// appeared out of nowhere on the one run that went, and its shape was invisible.
//
// Akshil overruled that twice (2026-08-17): "when doing a repeating [task] in the
// past, it should show me all the chips but it should only run the last one, the
// most last one, and then in the future as is". So the backward walk is the
// CLIENT's, and it is a mirror of fused_render/recur.py's `_walk_*`.
//
// The two must not drift, so the semantics below are recur.py's and are named
// rather than re-invented: a weekly rule counts in SUNDAY BLOCKS from the
// anchor's own week (so "every 2 weeks on Mon+Wed" means the same fortnight
// whichever of the two was picked as the start); a monthly rule reads its
// nth-weekday off the anchor and SKIPS a month with no 31st or no fifth Friday
// rather than clamping; `until` is an inclusive local DATE, so an hourly rule
// ending "on Nov 11" keeps all of the 11th's runs and none of the 12th's. A
// disagreement with recur.py is a bug here, never a second opinion.
//
// `count` is deliberately NOT applied. The server bills the budget only on
// occurrences it actually creates — `_catch_up_base` walks the past with
// `spend=False` precisely because those slots never existed and cost nothing —
// so numbering the theoretical past series against `count` would be the client
// inventing an accounting the store does not use.
//
// All arithmetic goes through LOCAL CALENDAR FIELDS, never a millisecond step:
// recur.py works in naive local time, and on the two DST days of the year adding
// 86_400_000ms lands an hour off the slot a person is looking at.

/** Hard cap on one walk — the same 500 the server's own projection uses. A rule
 * dense enough to reach it is drawn truthfully as far as the cap and no further;
 * spinning the render thread is the one outcome that is not acceptable. */
export const OCCURRENCE_LIMIT = 500;

// A month with no 31st, or no fifth Friday, is SKIPPED rather than clamped, so a
// walk can legitimately step over barren months. Give up after this many.
const MAX_EMPTY_STEPS = 500;

// Belt and braces around every loop below. None of them can run away — each
// advances a monotonic cursor — but a walk driven by server data on the render
// path gets a hard stop anyway.
const MAX_WALK_STEPS = 20000;

const daysInMonth = (y: number, m: number) => new Date(y, m + 1, 0).getDate();

const isLeapYear = (y: number) => (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;

// The day-of-month of the `nth` `want`-weekday of that month, or 0 when the
// month has none. recur.py `_nth_weekday_day`.
function nthWeekdayDay(y: number, m: number, want: number, nth: number): number {
  const first = new Date(y, m, 1).getDay();
  const day = 1 + ((want - first + 7) % 7) + (nth - 1) * 7;
  return day <= daysInMonth(y, m) ? day : 0;
}

const RULE_FREQS = ["hour", "day", "week", "month", "year"];

/**
 * Every occurrence of `rule` strictly after `after` and at or before `through`,
 * in order. `anchor` is the series' first run and the series includes it, so an
 * anchor inside the window comes back as its own occurrence.
 *
 * A rule this build cannot read — a `freq` a newer server invented — walks to
 * NOTHING rather than to a guess. An unreadable schedule that draws no ghost is
 * a thin calendar; one that draws the wrong ghost is a lie.
 */
export function ruleOccurrences(
  rule: RecurrenceRule,
  anchor: Date,
  after: Date,
  through: Date,
  limit: number = OCCURRENCE_LIMIT,
): Date[] {
  const out: Date[] = [];
  const born = anchor.getTime();
  const from = after.getTime();
  const stop = through.getTime();
  if (Number.isNaN(born) || Number.isNaN(from) || Number.isNaN(stop)) return out;
  if (stop <= from || limit <= 0) return out;
  if (!RULE_FREQS.includes(rule.freq)) return out;

  const interval = Math.min(99, Math.max(1, Math.floor(rule.interval || 1)));
  const until = rule.until || "";
  const hh = anchor.getHours();
  const mm = anchor.getMinutes();
  const ss = anchor.getSeconds();
  const y = anchor.getFullYear();
  const mo = anchor.getMonth();
  const dd = anchor.getDate();
  // Past the window, or past the rule's own end: either way the walk is over.
  // `until` is compared as a DATE string so the time of day cannot decide it.
  const done = (when: Date) =>
    when.getTime() > stop || (!!until && dayKey(when) > until);
  // The instant a jump-estimate should land just behind. Never before the
  // anchor: the series does not exist before its own first run.
  const edge = new Date(Math.max(from, born));
  let guard = 0;

  if (rule.freq === "hour" || rule.freq === "day") {
    const stepMs = (rule.freq === "hour" ? 3_600_000 : 86_400_000) * interval;
    // Jump to just behind `after` rather than walking from the anchor: a rule
    // anchored last year would otherwise cost thousands of steps to draw one
    // week. Two steps of slack absorb the DST slop a millisecond estimate
    // carries — the field arithmetic below is what actually decides.
    let n = Math.max(0, Math.floor((from - born) / stepMs) - 2);
    while (out.length < limit && guard++ < MAX_WALK_STEPS) {
      const when =
        rule.freq === "hour"
          ? new Date(y, mo, dd, hh + n * interval, mm, ss, 0)
          : new Date(y, mo, dd + n * interval, hh, mm, ss, 0);
      n += 1;
      if (when.getTime() <= from) continue; // still catching up to `after`
      if (done(when)) break;
      out.push(when);
    }
    return out;
  }

  if (rule.freq === "week") {
    // No `byday` means the anchor's OWN weekday — resolved against the anchor,
    // which the rule's validator never sees, exactly as recur.py does it.
    const days = rule.byday?.length
      ? [...new Set(rule.byday)].sort((a, b) => a - b)
      : [anchor.getDay()];
    // The Sunday on or before the anchor: the block a weekly rule counts in.
    const origin = new Date(y, mo, dd - anchor.getDay());
    let block = 0;
    if (startOfDay(edge).getTime() >= origin.getTime()) {
      // Round, not floor: a DST day is 23 or 25 hours and would otherwise
      // divide a whole week short.
      const days7 = Math.round(
        (startOfDay(edge).getTime() - origin.getTime()) / 86_400_000,
      );
      block = Math.floor(Math.floor(days7 / 7) / interval) * interval;
    }
    while (out.length < limit && guard++ < MAX_WALK_STEPS) {
      const start = addDays(origin, block * 7);
      block += interval;
      // Every day of a block that begins after the window is after it too.
      if (start.getTime() > stop) break;
      let ended = false;
      for (const day of days) {
        const when = new Date(
          start.getFullYear(), start.getMonth(), start.getDate() + day,
          hh, mm, ss, 0,
        );
        // `>= anchor` is what makes the anchor's own week a PARTIAL one: a
        // Wednesday anchor with byday Mon+Wed does not run on the Monday that
        // has already gone by, but does run every Monday after.
        if (when.getTime() < born || when.getTime() <= from) continue;
        if (done(when)) {
          ended = true;
          break;
        }
        out.push(when);
        if (out.length >= limit) break;
      }
      if (ended) break;
    }
    return out;
  }

  if (rule.freq === "month") {
    const byWeekday = rule.monthly === "nth-weekday";
    // "The second Wednesday" is read OFF the anchor rather than stated in the
    // rule: the user picked a date, and which Wednesday of the month it is
    // cannot then disagree with itself.
    const nth = Math.floor((dd - 1) / 7) + 1;
    const want = anchor.getDay();
    const anchorMonths = y * 12 + mo;
    const edgeMonths = edge.getFullYear() * 12 + edge.getMonth();
    let offset =
      edgeMonths >= anchorMonths
        ? Math.floor((edgeMonths - anchorMonths) / interval) * interval
        : 0;
    let empty = 0;
    while (out.length < limit && empty < MAX_EMPTY_STEPS && guard++ < MAX_WALK_STEPS) {
      const total = anchorMonths + offset;
      const my = Math.floor(total / 12);
      const mmo = total % 12;
      offset += interval;
      const day = byWeekday
        ? nthWeekdayDay(my, mmo, want, nth)
        : dd <= daysInMonth(my, mmo) ? dd : 0;
      if (!day) {
        empty += 1; // no 31st / no fifth Friday: skipped, never clamped
        continue;
      }
      const when = new Date(my, mmo, day, hh, mm, ss, 0);
      // The jump landed a step short; not an empty month, so `empty` stands.
      if (when.getTime() < born || when.getTime() <= from) continue;
      if (done(when)) break;
      empty = 0;
      out.push(when);
    }
    return out;
  }

  let offset =
    edge.getFullYear() >= y
      ? Math.floor((edge.getFullYear() - y) / interval) * interval
      : 0;
  let empty = 0;
  while (out.length < limit && empty < MAX_EMPTY_STEPS && guard++ < MAX_WALK_STEPS) {
    const yy = y + offset;
    offset += interval;
    if (mo === 1 && dd === 29 && !isLeapYear(yy)) {
      empty += 1; // a Feb 29 rule genuinely has nothing to say in 2027
      continue;
    }
    const when = new Date(yy, mo, dd, hh, mm, ss, 0);
    if (when.getTime() < born || when.getTime() <= from) continue;
    if (done(when)) break;
    empty = 0;
    out.push(when);
  }
  return out;
}

// ---- Ghosts, both directions -------------------------------------------------------
//
// A recurring rule's occurrences that are NOT messages: the future ones the
// server projected (`upcoming` on the recurring template — see
// server/routers/schedule.py), and the past ones nothing ever materialized.
// Neither is in any feed, so a task's thread cannot carry them and the grid
// would show a rule only where it happens to have written something down. This
// turns both into synthetic messages on the task that owns the rule.
//
// DEDUPE RUNS IN BOTH DIRECTIONS, and that is what keeps the one real run of a
// past-anchored rule honest: catch-up materializes the most recent past slot at
// that slot's OWN time, so a ghost would land on the very same minute. Every
// projection — behind now or ahead of it — is checked against the minutes the
// thread already holds, and drops when one is taken.
//
// `base` is the thread each task is known to have in the visible window (the
// windowed endpoint's answer). It matters for the DEDUPE, not just the seed: a
// materialized occurrence the listing's three-message tail never mentioned is
// still a run the grid must not draw twice, and only the windowed thread knows
// about it.
//
// THE PAST GHOSTS ARE DRAW-ONLY. Nothing here asks for them to be created, and
// the backend must not start: catch-up runs exactly one of them and drops the
// rest for good (§9). They are a picture of the rule's shape, which is what the
// user asked the calendar to show.

export function projectedMessages(
  tasks: Task[],
  entries: ScheduledMessage[],
  base: Record<string, TaskMessage[]> = {},
  now: Date = new Date(),
  /**
   * The oldest instant worth drawing — the first visible day. HOW FAR BACK is
   * the window's question and this function does not guess at it: omitted, the
   * past is not walked at all. A default horizon would make the answer depend on
   * a clock nobody passed in, and would draw ghosts for a caller that never
   * asked how the past looked.
   */
  since?: Date,
): Record<string, TaskMessage[]> {
  const nowSec = Math.floor(now.getTime() / 1000);
  // A hair before, because the walk is strictly-after: a rule that fires at
  // local midnight must still draw on the window's own first day.
  const back = since ? new Date(since.getTime() - 1) : null;
  const out: Record<string, TaskMessage[]> = {};
  for (const entry of entries) {
    if (entry.state !== "recurring") continue;
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
    // An ARCHIVED task draws no chip (taskChips), so hanging a live rule's
    // whole forecast on one would delete the forecast. Claimants that are still
    // drawn are preferred; an archived one is a last resort rather than a
    // silent hole in the grid.
    const pick = (list: Task[]) => list.find((t) => !t.session_id) ?? list[0];
    const owner =
      pick(claims.filter((t) => !isArchivedTask(t))) ??
      pick(claims) ??
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
    const ghost = (iso: string, at: number, state: TaskMessage["state"]) => {
      const minute = Math.floor(at / 60);
      if (taken.has(minute)) return;
      taken.add(minute);
      list.push({
        message_id: `${GHOST_PREFIX}${iso}`,
        kind: "scheduled",
        body: entry.message,
        at,
        // A projection has not run — a future one not yet, a past one not ever.
        ran_at: 0,
        state,
        unread: false,
        entry_id: entry.id,
        template_id: entry.id,
        turn: "",
        anchor: "",
      });
    };

    for (const iso of entry.upcoming ?? []) {
      const t = new Date(iso);
      if (Number.isNaN(t.getTime())) continue;
      const at = Math.floor(t.getTime() / 1000);
      // The server's projection is now-forward by construction; a stale poll can
      // still hand over a slot that has since gone, and the past walk below owns
      // those.
      if (at <= nowSec) continue;
      ghost(iso, at, "pending");
    }

    // The slots behind us that nothing ever ran. Only a RULE template has them:
    // a cron template is computed from `now` at creation and has no anchor, so
    // there is no past series to walk (`_catch_up_base` refuses one for exactly
    // this reason) and the client has no cron parser to walk it with.
    const rule = entry.rule;
    if (!rule || !back) continue;
    // `anchor` is on the wire — schedule.py writes it on every rule template —
    // but not yet in the ScheduledMessage type, which belongs to another lane.
    // Read defensively and fall back to `due`, exactly as the server does.
    const anchorIso = (entry as { anchor?: string }).anchor || entry.due;
    const anchor = new Date(anchorIso);
    if (Number.isNaN(anchor.getTime())) continue;
    for (const when of ruleOccurrences(rule, anchor, back, now)) {
      // `missed` WITH a template_id is the app's existing reading of "that slot
      // went by and nothing ran": tasks-lib.messageTone files it under ARCHIVE,
      // which is §1's word for these, and schedule-lib.messageTone greys the
      // chip rather than striking it through — a strike says somebody called
      // the run off, and nobody called these off.
      ghost(when.toISOString(), Math.floor(when.getTime() / 1000), "missed");
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
//   * `entries`  — the recurring templates, whose occurrences either side of now
//     are not messages yet (ahead) or never will be (behind), and so appear in
//     neither feed.
//
// Projections are layered ON TOP of the windowed thread (they were seeded from
// it), so spreading them over it is a merge, not a clobber. A task in neither
// gets no entry at all, which is what makes taskChips fall back to its own
// `task.messages` — the degraded-but-useful grid when the window fetch fails.
//
// `days` is the visible window, and it is what bounds the BACKWARD walk: a
// person paging three weeks back still wants to see the shape of the rule that
// was running then, and a fixed horizon off `now` would draw nothing there.
// Omitted, the past is not walked at all — see projectedMessages on why that is
// a refusal to guess rather than a missing default.
export function calendarThreads(
  tasks: Task[],
  entries: ScheduledMessage[],
  windowed: Record<string, TaskMessage[]> | null,
  now: Date = new Date(),
  days: Date[] = [],
): Record<string, TaskMessage[]> {
  const base = windowed ?? {};
  return { ...base, ...projectedMessages(tasks, entries, base, now, days[0]) };
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

// ---- The queue, inside the thread -------------------------------------------------
// Nothing fires while the app is closed and one-off catch-up is now unbounded,
// so opening after a week away can find real work waiting. `queued` is past due
// and unclaimed, `running` is mid-flight.
//
// Both used to be drawn as an all-day strip across the top of the grid. They are
// not: a queued message is a MESSAGE, its siblings are already listed in the
// task's thread, and a band across the week said otherwise (Akshil, 2026-08-17).
// The two facts now ride the thread row they belong to, and so does the cancel.

// One line for a prompt: the calendar reads a message by its first line, and a
// pasted three-paragraph prompt must not become a three-line chip.
export function firstLine(text: string): string {
  const line = (text ?? "").split("\n").find((l) => l.trim().length);
  return (line ?? "").trim();
}

/** "" for a message the queue has nothing to say about. */
export type QueueRole = "" | "queued" | "running";

/** The server's own answer, by schedule-entry id. `running` is applied second
 * so an entry claimed between the two lists reads as running, which is the
 * half that changes what Cancel can promise. */
export function queueRoles(
  queued: ScheduledMessage[],
  running: ScheduledMessage[],
): Map<string, QueueRole> {
  const map = new Map<string, QueueRole>();
  for (const e of queued) if (e.id) map.set(e.id, "queued");
  for (const e of running) if (e.id) map.set(e.id, "running");
  return map;
}

/**
 * Whether a thread row is queued or running.
 *
 * The queue endpoint is authoritative when it knows the entry, and the message
 * itself is the fallback — the queue is a SECOND feed and a page whose second
 * feed failed still has to say something true. Past due and still `pending` IS
 * the definition of queued, so the fallback is not a guess.
 *
 * A projected occurrence is excluded on purpose: it is cron arithmetic, nothing
 * has been written down, and there is nothing for the queue to be holding.
 */
export function queueRole(
  m: TaskMessage,
  roles: Map<string, QueueRole>,
  nowSec: number,
): QueueRole {
  if (m.kind !== "scheduled" || isProjected(m)) return "";
  const known = m.entry_id ? roles.get(m.entry_id) : undefined;
  if (known) return known;
  if (m.state === "sending") return "running";
  if (m.state === "pending" && m.at > 0 && m.at <= nowSec) return "queued";
  return "";
}

/**
 * Which cancel a thread row gets, and the ONE place that decides it — the same
 * job `rowCancelKind` does for the queue dock (queue-dock-lib.ts), kept here so
 * the popover's markup and its click handler cannot disagree about whether a row
 * is withdrawable.
 *
 * The rule is the SERVER'S, not the popover's idea of what looks cancellable.
 * `schedule.cancel_queued` accepts exactly `pending` → `cancelled`, so:
 *
 * * **scheduled** — pending and still in the future — is a plain
 *   `cancelScheduledMessage`. Nothing has claimed it, so nothing can race.
 * * **queued** — pending, and held by the queue — goes through `cancelQueued`,
 *   the only endpoint that can answer honestly when the claim wins the race: the
 *   entry comes back `refused` and `cancelOutcome` puts the server's sentence on
 *   screen. That refusal is a real answer to a real attempt, and it is exactly
 *   why this row keeps its button.
 * * **held** — `sending`: claimed, the helper already away — gets NO control,
 *   and the row says why in its place. The server refuses this state EVERY time
 *   ("cancelled" would be a claim it cannot make good on), so the button's only
 *   possible outcome is a refusal, and a button that can only fail is worse than
 *   no button. The row that was queued a second ago must not simply go quiet
 *   either — a control that vanishes without a word reads as a bug — hence a
 *   fourth value rather than folding this into `none`.
 * * **none** — everything else. A projected ghost has no entry to cancel; a
 *   finished, missed or cancelled run has nothing left to stop.
 *
 * A LIVE row — `sent` with a turn still running — is deliberately `none` and not
 * the dock's `"job"`. The dock earns its ✕ by polling the job registry and
 * joining the queue's `live` list onto it; the popover has neither feed, and a
 * stop button here would rest on `turnPhase("")`, which DEFAULTS to "running"
 * for a field the server has not written yet. A process-killing control hung off
 * a defaulted field is precisely the promise this module must not make. "Open in
 * Explorer" is the popover's way TO a running turn; the dock is where it stops.
 */
export type MsgCancelKind = "scheduled" | "queued" | "held" | "none";

export function msgCancelKind(m: TaskMessage, role: QueueRole): MsgCancelKind {
  if (!m.entry_id || isProjected(m)) return "none";
  // The claimed state, on EITHER witness: the queue endpoint calling it running,
  // or the message's own `sending`. Tested before `pending` so an entry the
  // server has already claimed cannot fall through to a button on the strength
  // of a stale `state` the second feed has since corrected.
  if (role === "running" || m.state === "sending") return "held";
  if (m.state !== "pending") return "none";
  return role === "queued" ? "queued" : "scheduled";
}

/**
 * The words that stand in for a claimed row's cancel.
 *
 * The dock's own sentence, minus the half this row already has: `roleText` says
 * "Starting… · too late to cancel" because a dock row carries no status word,
 * while a thread row says "In Progress" right beside this. Only the part the
 * reader cannot get anywhere else is repeated.
 */
export const HELD_TEXT = "too late to cancel";

/**
 * The row's second line of fact, when it has one — decided here rather than in
 * the markup so it stays tied to the control the row was given.
 *
 * A catch-up says how far behind it ran (`at` vs `ran_at`, never the chip's
 * position); a queued one says it is waiting to be claimed, which "Upcoming"
 * alone does not.
 */
export function msgNote(m: TaskMessage, kind: MsgCancelKind): string {
  // Why the control is missing outranks a retrospective fact. `ran_at` is
  // written at the CLAIM, so a caught-up entry in `sending` genuinely does have
  // a "ran 2 days late" to report — and it answers a question nobody is asking
  // while a button they were about to press has just gone. It comes back
  // unchanged the moment the state moves on.
  if (kind === "held") return HELD_TEXT;
  return lateText(m) || (kind === "queued" ? "queued" : "");
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

// ---- Late runs ---------------------------------------------------------------------
// `at` is ALWAYS the time a message was scheduled for, and `ran_at` is when it
// actually went (0 if it never did). They used to be one field, so a task
// scheduled on Monday and caught up on Wednesday jumped to Wednesday's column;
// the chip is placed by `at` and the gap between the two is said in words
// instead. Nothing here invents a notion of "late" — it is exactly `ran_at`
// minus `at`.

// Below this the gap is the scheduler's own granularity (it sweeps on a tick,
// and a run that starts 40 seconds after its minute is not a fact about the
// task). Five minutes is the floor at which "late" is worth a reader's
// attention.
export const LATE_MIN_S = 300;

/** `ran_at` read defensively: the field is the server's and older builds — or a
 * projected occurrence — simply do not carry it. 0 means "never ran". */
export function ranAt(m: TaskMessage): number {
  const v = (m as TaskMessage & { ran_at?: number }).ran_at;
  return typeof v === "number" && Number.isFinite(v) && v > 0 ? v : 0;
}

/** Seconds between when a message was due and when it ran; 0 when it has not
 * run, or ran close enough to its time that saying so would be noise. */
export function lateBy(m: TaskMessage): number {
  const ran = ranAt(m);
  if (!ran || !m.at) return 0;
  const late = ran - m.at;
  return late >= LATE_MIN_S ? late : 0;
}

/** "ran 2 days late" — the row's own words for a catch-up. "" when the run was
 * on time, or has not happened. Each unit hands over before it can print a
 * value its next unit owns, so 59m59s is never "60 minutes". */
export function lateText(m: TaskMessage): string {
  const s = lateBy(m);
  if (!s) return "";
  const say = (n: number, unit: string) =>
    `ran ${n} ${unit}${n === 1 ? "" : "s"} late`;
  const mins = Math.round(s / 60);
  if (mins < 60) return say(mins, "minute");
  const hours = Math.round(s / 3600);
  if (hours < 24) return say(hours, "hour");
  return say(Math.round(s / 86400), "day");
}

// ---- One status vocabulary ---------------------------------------------------------
// The Board and the List say Upcoming / In Progress / Done / Failed / Archive
// about a task. The calendar used to say Scheduled / Ran / Failed / Projected
// about a run, and the split read as two products (Akshil, 2026-08-17). There is
// now ONE set of words and the calendar speaks it.
//
// FAILURE IS A WORD, not a red ring inside Done — that was the first cut of this
// change and the user rejected it: "if we can't show failed tasks, then let's
// have a failed status, and show it everywhere". The red stays as
// reinforcement; the word is the signal.
//
// Two edge cases are settled, and neither gets a sixth word:
//
//   skipped    → "Archive". Filed away, never attempted — a run that tried and
//                broke is a different thing.
//   projected  → "Upcoming", because a projection genuinely IS upcoming.
//                Nothing is lost by the word, and the dashed ring is what says
//                nothing has been written down yet.
//
// WHY THE COLUMN IS AN ARGUMENT. tasks-lib.messageTone is the app's one answer
// to "which column is this message in", and it is not re-derived here: this file
// is the LOWER module (tasks-lib imports from it), so importing it back would be
// a cycle. `RunTone` is that answer's shape, structurally — the caller hands
// messageTone's result straight in, and a second mapping never gets written.
//
// schedule-lib.messageTone survives alongside this, and is now PIXELS ONLY: it
// names a CSS class per chip (missed amber, skipped struck through) and no longer
// puts any word on the screen. Its day-level sibling, dayTone, is gone — a chip
// is one message again, so there is no day for a tone to answer for.

export interface RunTone {
  column: BoardColumn;
  failed: boolean;
  label: string;
}

export interface RunStatus {
  column: BoardColumn;
  /** Settled, but not well. The word already says "Failed"; this is what paints
   * it red as well, and what a caller filters on. */
  failed: boolean;
  /** Cron arithmetic, not a message yet: dashed ring, dashed pill. */
  projected: boolean;
  /** The one word that goes on screen — the Board's own. */
  label: string;
  /** The finer reading, for a tooltip only ("Missed", "Stopped reporting",
   * "Ran"): never the row's word, and never a status of its own. */
  detail: string;
}

/** The Board's word for one of the four columns. BOARD_COLUMNS is the single
 * source; a hand-written second map is how the two views drift apart. */
export function columnLabel(column: BoardColumn): string {
  return BOARD_COLUMNS.find((c) => c.key === column)?.label ?? column;
}

/**
 * One message, in the one vocabulary. `tone` is tasks-lib.messageTone(m).
 *
 * The one thing done here rather than read: a tone that is settled-but-failed
 * lands in the `failed` COLUMN. tasks-lib filed those under `done` back when
 * failure was only a red ring, and this promotes the flag it already sets into
 * the word the user asked for. It is a bridge, not a second opinion — the day
 * tasks-lib returns `failed` itself, this line becomes a no-op and can go.
 */
export function runStatus(m: TaskMessage, tone: RunTone): RunStatus {
  const column = tone.failed ? "failed" : tone.column;
  return {
    column,
    failed: tone.failed,
    projected: isProjected(m),
    label: columnLabel(column),
    detail: tone.label,
  };
}

/**
 * THE POPOVER HEADER'S PILL: one TASK's status, in the same five words.
 *
 * This replaced a `dayStatus` that ranked the runs on the chip's DAY (failed >
 * in_progress > upcoming > archived > done) and put the worst one's word in the
 * header. That is a different question from the one the header asks, and the two
 * answers were visibly disagreeing: a recurring rule whose TASK is `upcoming`
 * but whose day holds one failed run wore a pill reading "Failed" beside a
 * footer button reading "Run now" — two facts about one task, in one panel.
 *
 * The pill sits beside `TASK-023` in that header, so it labels the TASK, not the
 * column of runs under it — the same noun the List's row and the Board's card
 * are, and therefore the same word. Nothing is lost by the change: each run's
 * own outcome is already the word at the end of its row in the thread below
 * (runStatus above), which is where a day-level fact belongs.
 *
 * WHY THE COLUMN IS AN ARGUMENT, again: `taskColumn` lives in tasks-lib, which
 * imports this file, so asking it here would be a cycle. The caller reads
 * `taskColumn(task)` and `task.failed` — the exact two values it hands
 * StatusIcon on the List and the Board — and this turns them into the word.
 *
 * `failed` collapses into the COLUMN rather than staying a flag beside it, which
 * is the same bridge runStatus makes and matches StatusIcon's own text exactly:
 * a task triaged to `done` whose newest run broke says Failed, and so does a
 * task the server already filed under `failed`.
 *
 * `projected` is the one DAY-scoped thing the pill still carries, and
 * deliberately not as a word: the dashes say "nothing on this day is written
 * down yet", a glance-level cue the chip itself also wears. Dropping a visual
 * distinction is not part of dropping a duplicated word.
 */
export function taskStatus(
  column: BoardColumn,
  failed: boolean,
  projected = false,
): RunStatus {
  const c = failed ? "failed" : column;
  return {
    column: c,
    failed: c === "failed",
    projected,
    label: columnLabel(c),
    // A task has no finer reading to keep: `detail` exists for the run-level
    // words runStatus folds away ("Missed", "Stopped reporting"), and a task's
    // status IS the column.
    detail: "",
  };
}

// ---- Keeping a popover on screen ---------------------------------------------------
// The panel is `position: fixed` (an absolutely-positioned one is clipped by the
// first scrolling ancestor, and this grid scrolls). Fixed means the viewport is
// the only frame it has to fit, so: sit below-right of the click, FLIP to the
// other side when that overflows, and clamp as the last resort — a panel taller
// than the viewport pins to the top margin and scrolls internally.

export const POPOVER_MARGIN = 8;

export function popoverPos(
  x: number,
  y: number,
  w: number,
  h: number,
  vw: number,
  vh: number,
): { left: number; top: number } {
  const m = POPOVER_MARGIN;
  let left = x + m;
  if (left + w > vw - m) left = x - w - m;
  left = Math.max(m, Math.min(left, Math.max(m, vw - w - m)));
  let top = y + m;
  if (top + h > vh - m) top = y - h - m;
  top = Math.max(m, Math.min(top, Math.max(m, vh - h - m)));
  return { left, top };
}
