/**
 * Canonical status colour map (Flow design language, rule 4).
 *
 * Every component that renders a status — task rows, job chips, model
 * availability, health strips — imports from here. No call site invents a
 * status colour. Unknown keys fall back to neutral; nothing throws.
 *
 * Buckets: green = done/published/fresh/active; yellow = in progress/draft;
 * blue = todo/upcoming; orange = paused/stale/waiting-on-user; red = failed;
 * neutral = cancelled/archived/unknown.
 */

export type StatusBucket = "green" | "yellow" | "blue" | "orange" | "red" | "neutral";

/** Lifecycle vocabulary → colour bucket. Extend here, never at a call site. */
export const statusBucket: Record<string, StatusBucket> = {
  // done / healthy
  done: "green",
  completed: "green",
  succeeded: "green",
  ok: "green",
  active: "green",
  live: "green",
  installed: "green",
  fresh: "green",
  signed_in: "green",
  // in flight
  in_progress: "yellow",
  running: "yellow",
  progress: "yellow",
  loading: "yellow",
  draft: "yellow",
  pending: "yellow",
  // not started
  todo: "blue",
  upcoming: "blue",
  scheduled: "blue",
  queued: "blue",
  // waiting / stale
  paused: "orange",
  blocked: "orange",
  stale: "orange",
  waiting: "orange",
  warning: "orange",
  // broken
  failed: "red",
  error: "red",
  timed_out: "red",
  broken: "red",
  // neutral
  cancelled: "neutral",
  archived: "neutral",
  unknown: "neutral",
  none: "neutral",
};

export const statusBucketDefault: StatusBucket = "neutral";

export function bucketOf(status: string | null | undefined): StatusBucket {
  return (status && statusBucket[status]) || statusBucketDefault;
}

/** Text colour per bucket (labels, icons). */
export const bucketText: Record<StatusBucket, string> = {
  green: "text-green-600 dark:text-green-400",
  yellow: "text-yellow-600 dark:text-yellow-400",
  blue: "text-blue-600 dark:text-blue-400",
  orange: "text-orange-600 dark:text-orange-400",
  red: "text-red-600 dark:text-red-400",
  neutral: "text-muted-foreground",
};

/** Solid fill per bucket (status dots, meter bars). */
export const bucketFill: Record<StatusBucket, string> = {
  green: "bg-green-600 dark:bg-green-400",
  yellow: "bg-yellow-600 dark:bg-yellow-400",
  blue: "bg-blue-600 dark:bg-blue-400",
  orange: "bg-orange-600 dark:bg-orange-400",
  red: "bg-red-600 dark:bg-red-400",
  neutral: "bg-neutral-500",
};

/** Border colour per bucket (outlined status circles). */
export const bucketBorder: Record<StatusBucket, string> = {
  green: "border-green-600 dark:border-green-400",
  yellow: "border-yellow-600 dark:border-yellow-400",
  blue: "border-blue-600 dark:border-blue-400",
  orange: "border-orange-600 dark:border-orange-400",
  red: "border-red-600 dark:border-red-400",
  neutral: "border-muted-foreground",
};

/** Tinted badge per bucket (bg + text). */
export const bucketBadge: Record<StatusBucket, string> = {
  green: "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300",
  yellow: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-300",
  blue: "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300",
  orange: "bg-orange-100 text-orange-700 dark:bg-orange-900/50 dark:text-orange-300",
  red: "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300",
  neutral: "bg-muted text-muted-foreground",
};

export const statusText = (s: string | null | undefined) => bucketText[bucketOf(s)];
export const statusFill = (s: string | null | undefined) => bucketFill[bucketOf(s)];
export const statusBorder = (s: string | null | undefined) => bucketBorder[bucketOf(s)];
export const statusBadge = (s: string | null | undefined) => bucketBadge[bucketOf(s)];

/** Progress / meter bar: green <60%, yellow 60–85%, red >85%. */
export function meterFill(pct: number): string {
  if (pct > 85) return bucketFill.red;
  if (pct >= 60) return bucketFill.yellow;
  return bucketFill.green;
}

/** Priority → text colour. */
export const priorityText: Record<string, string> = {
  critical: bucketText.red,
  high: bucketText.orange,
  medium: bucketText.yellow,
  low: bucketText.blue,
};
export const priorityTextDefault = bucketText.yellow;
