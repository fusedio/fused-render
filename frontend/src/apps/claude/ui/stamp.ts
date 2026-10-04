// WHEN a user message was sent, as a reader reads a clock (design.md §C).
//
// The stamp is drawn only on hover, in the left icon lane, so it has to be
// short: today's messages are the ones a reader is placing against "just now",
// and those get the bare `HH:MM`. Anything older gets the date in front of it,
// because `21:59` on a message from last week is a lie the reader has to catch
// themselves.
//
// LOCAL TIME, and its own arithmetic rather than `toLocaleString`: the format is
// fixed by the design (`Sep 14, 21:59`), and a locale-formatted string is
// whatever the host decides — `9/14/2026, 9:59:00 PM` on an en-US machine — so
// the one thing a test could pin would be that the function was called.

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

/** Epoch SECONDS (agent.py `_row_ts`, and the controller's own `now() / 1000`)
 *  in milliseconds — tolerating a value that is already milliseconds, because
 *  a raw `Date.now()` is one keystroke away at every site that writes this
 *  field. 1e11 seconds is the year 5138 and 1e11 ms is 1973, so no real stamp
 *  is ambiguous. */
function millis(ts: number): number {
  return ts > 1e11 ? ts : ts * 1000;
}

const pad = (n: number): string => (n < 10 ? "0" + n : String(n));

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

/**
 * Under a day old: one relative unit — `just now`, `5m ago`, `3h ago`
 * (Akshil, 2026-09-16). Older: `14 Sep, 14:34`, day first.
 *
 * `null` for anything that is not a usable instant — a missing field, a zero, a
 * NaN — so the caller's test is "is there a stamp" rather than "is there a `ts`,
 * and is it sane".
 */
export function formatStamp(ts: number | null | undefined, now: number = Date.now()): string | null {
  if (typeof ts !== "number" || !Number.isFinite(ts) || ts <= 0) return null;
  const at = millis(ts);
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return null;
  const age = now - at;
  if (age < DAY) {
    if (age < MIN) return "just now";
    if (age < HOUR) return Math.floor(age / MIN) + "m ago";
    return Math.floor(age / HOUR) + "h ago";
  }
  const clock = pad(d.getHours()) + ":" + pad(d.getMinutes());
  return d.getDate() + " " + MONTHS[d.getMonth()] + ", " + clock;
}

/** The tooltip: the full instant, readable — `14 Sep 2026, 14:34:05`, local
 *  time (Akshil, 2026-09-16: the ISO string was not). `null` on the same
 *  inputs `formatStamp` refuses. */
export function stampTitle(ts: number | null | undefined): string | null {
  if (typeof ts !== "number" || !Number.isFinite(ts) || ts <= 0) return null;
  const d = new Date(millis(ts));
  if (Number.isNaN(d.getTime())) return null;
  return (
    d.getDate() + " " + MONTHS[d.getMonth()] + " " + d.getFullYear() + ", " +
    pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds())
  );
}
