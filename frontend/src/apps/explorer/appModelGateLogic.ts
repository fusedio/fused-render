// Pure helpers behind AppModelGate: what each row says, what the footer says,
// and when the downloads count as finished. No React, no fetch, so the wording
// and the completion rule are testable without a dialog.
import type { AppModelRow } from "@platform/lib/api";
import type { Job } from "@platform/lib/jobs";

export function formatGb(gb: number): string {
  return gb >= 10 ? `${Math.round(gb)} GB` : `${Math.round(gb * 10) / 10} GB`;
}

/** The right-aligned value of a row in the review view. */
export function rowValue(row: AppModelRow): string {
  if (row.provider === "apple") return "Built in";
  if (row.provider === "cloud") return "Cloud";
  if (row.downloaded) return "On this Mac";
  return row.sizeGb != null ? formatGb(row.sizeGb) : "To download";
}

/** The row detail: the publisher's label, plus the off-machine note on cloud. */
export function rowDetail(row: AppModelRow): string {
  return row.provider === "cloud" ? `${row.use} · sends data off this machine` : row.use;
}

/** The model a row will run: the user's pick when it differs from the declared one. */
export function chosenRow(row: AppModelRow, choice: string) {
  const picked = row.candidates.find((c) => c.id === choice);
  return picked ?? { id: row.model, label: row.label, sizeGb: row.sizeGb };
}

/** What still has to be fetched once the user's picks are applied. */
export function pendingRows(rows: AppModelRow[], choices: Record<string, string>): AppModelRow[] {
  return rows.filter((r) => !r.downloaded && !(choices[r.capability] && choices[r.capability] !== r.model));
}

export function needGb(rows: AppModelRow[], choices: Record<string, string>): number {
  return pendingRows(rows, choices).reduce((sum, r) => sum + (r.sizeGb ?? 0), 0);
}

export function footerHint(rows: AppModelRow[], choices: Record<string, string>, etaMin: number | null | undefined): string {
  const need = needGb(rows, choices);
  if (pendingRows(rows, choices).length === 0) return "Nothing to download";
  const size = need > 0 ? formatGb(need) : "";
  const eta = etaMin ? `about ${etaMin} min` : "";
  return [size, eta].filter(Boolean).join(" · ") || "Download needed";
}

/** The substitutions to send on accept: only rows whose pick differs. */
export function substitutionsOf(rows: AppModelRow[], choices: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of rows) {
    const c = choices[r.capability];
    if (r.swappable && c && c !== r.model) out[r.capability] = c;
  }
  return out;
}

export type RowProgress =
  | { state: "ready" }
  | { state: "running"; pct: number | null; done: number | null; total: number | null }
  | { state: "error"; message: string }
  | { state: "waiting" };

/** One row's download state from the live jobs list. A row with no job and
 *  not downloaded is `waiting` until the caller decides it failed. */
export function rowProgress(row: AppModelRow, jobId: string | null, jobs: Job[]): RowProgress {
  if (row.downloaded) return { state: "ready" };
  const job = jobId ? jobs.find((j) => j.id === jobId) : undefined;
  if (!job) return { state: "waiting" };
  if (job.state === "error" || job.state === "cancelled") {
    return { state: "error", message: job.message || (job.state === "cancelled" ? "Cancelled" : "Download failed") };
  }
  if (job.state === "done") return { state: "ready" };
  const pct = job.total && job.done != null ? Math.min(100, Math.round((job.done / job.total) * 100)) : null;
  return { state: "running", pct, done: job.done, total: job.total };
}

/** True when no tracked download is still running, so the overview should be
 *  re-read to confirm the weights are on disk. */
export function noneRunning(jobIds: string[], jobs: Job[]): boolean {
  return !jobIds.some((id) => jobs.some((j) => j.id === id && (j.state === "running" || j.state === "waiting")));
}
