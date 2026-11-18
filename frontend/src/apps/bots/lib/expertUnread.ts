// How many new activity lines each expert has (GET /api/experts `unread`), for the dot on its row in the bots list.
// A tiny store of its own: the list polls it while an expert is listed, and the expert's Activity tab writes it too
// (the count its own fetch carried, and 0 once the tab has been opened).
import { useEffect, useSyncExternalStore } from "react";
import { expertApi, type ExpertRow } from "./api";

const POLL_MS = 15_000;
let counts: Record<string, number> = {};
const subs = new Set<() => void>();
const publish = (next: Record<string, number>) => { counts = next; subs.forEach((f) => f()); };

/** The rows of GET /api/experts as id → unread (only the experts that have any). */
export function unreadByExpert(rows: readonly Pick<ExpertRow, "id" | "unread">[] | null | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  for (const r of Array.isArray(rows) ? rows : []) if (r && typeof r.id === "string" && typeof r.unread === "number" && r.unread > 0) out[r.id] = r.unread;
  return out;
}

export const expertUnread = (id: string): number => counts[id] || 0;

export function setExpertUnread(id: string, n: number): void {
  const v = Math.max(0, Math.floor(n || 0));
  if ((counts[id] || 0) === v) return;
  const next = { ...counts };
  if (v) next[id] = v; else delete next[id];
  publish(next);
}

/** Silent: the dot is a nicety, a failed fetch leaves the last counts. */
export async function refreshExpertUnread(): Promise<void> {
  try {
    const next = unreadByExpert(await expertApi.list()), a = Object.keys(next), b = Object.keys(counts);
    if (a.length !== b.length || a.some((k) => next[k] !== counts[k])) publish(next);
  } catch { /* an older server or the phone's door: no dot */ }
}

const subscribe = (f: () => void) => { subs.add(f); return () => { subs.delete(f); }; };
const snapshot = () => counts;
/** id → unread, re-rendering when it changes. */
export const useExpertUnread = (): Record<string, number> => useSyncExternalStore(subscribe, snapshot, snapshot);

/** Keep the counts fresh while `on` (an expert is listed): now, and every 15 s while the page is visible. */
export function useExpertUnreadPoll(on: boolean): void {
  useEffect(() => {
    if (!on) return;
    void refreshExpertUnread();
    const t = window.setInterval(() => { if (!document.hidden) void refreshExpertUnread(); }, POLL_MS);
    return () => window.clearInterval(t);
  }, [on]);
}
