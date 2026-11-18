// The specialist events of ONE task session (GET /api/specialists/task-events,
// PLAN B.3/B.5): pulls, notes, breakouts opened/closed/discarded, stuck
// nudges. Read when the session is known, again every POLL_MS while mounted,
// and on demand (`refresh`) after this page did something that adds one.
import { useCallback, useEffect, useRef, useState } from "react";
import { getTaskEvents, listSpecialists, type SpecialistSummary, type TaskEvent } from "@platform/lib/specialists-api";

export const TASK_EVENTS_POLL_MS = 15_000;

export function useTaskEvents(
  sessionId: string,
  enabled: boolean,
  fetchEvents: (sid: string) => Promise<{ events: TaskEvent[] }> = getTaskEvents,
): { events: readonly TaskEvent[]; refresh: () => void } {
  const [events, setEvents] = useState<readonly TaskEvent[]>([]);
  const seq = useRef(0);
  const fetchRef = useRef(fetchEvents);
  fetchRef.current = fetchEvents;

  const refresh = useCallback(() => {
    if (!enabled || !sessionId) return;
    const mine = ++seq.current;
    fetchRef.current(sessionId).then(
      (r) => {
        if (mine !== seq.current) return;
        setEvents(Array.isArray(r?.events) ? r.events : []);
      },
      () => {
        // A failed read keeps what is on screen; the next tick tries again.
      },
    );
  }, [enabled, sessionId]);

  useEffect(() => {
    setEvents([]);
    if (!enabled || !sessionId) return;
    refresh();
    const t = setInterval(refresh, TASK_EVENTS_POLL_MS);
    return () => {
      clearInterval(t);
      seq.current += 1;
    };
  }, [enabled, sessionId, refresh]);

  return { events, refresh };
}

// ── the user's specialists, for the menus and pickers inside a task ─────────
// One read per page (shared by every chat mount), refreshed at most every
// LIST_TTL_MS when a new mount asks.
const LIST_TTL_MS = 60_000;
let listCache: { at: number; rows: readonly SpecialistSummary[] } | null = null;
let listReading: Promise<readonly SpecialistSummary[]> | null = null;

export function readSpecialistList(force = false): Promise<readonly SpecialistSummary[]> {
  if (!force && listCache && Date.now() - listCache.at < LIST_TTL_MS) return Promise.resolve(listCache.rows);
  if (listReading) return listReading;
  listReading = listSpecialists()
    .then((rows) => {
      const list = Array.isArray(rows) ? rows : [];
      listCache = { at: Date.now(), rows: list };
      return list;
    })
    .catch(() => listCache?.rows ?? [])
    .finally(() => {
      listReading = null;
    });
  return listReading;
}

/** A specialist's name for what a tool call named it by (its id, or its name
 *  already), from the list this page has read. An id not in the list yet is
 *  answered with nothing (and the list is read for next time). */
export function specialistNameNow(key: string): string {
  const k = (key || "").trim();
  if (!k) return "";
  const rows = listCache?.rows;
  if (!rows) void readSpecialistList();
  const hit = rows?.find((r) => r.id === k) ?? rows?.find((r) => r.name.toLowerCase() === k.toLowerCase());
  if (hit) return hit.name;
  // An id-shaped key (hex, 8+) is not a name to show.
  return /^[0-9a-f-]{8,}$/i.test(k) ? "" : k;
}

/** Test seam. */
export function setSpecialistListForTests(rows: readonly SpecialistSummary[] | null): void {
  listCache = rows ? { at: Date.now(), rows } : null;
  listReading = null;
}

export function useSpecialistList(enabled: boolean): readonly SpecialistSummary[] {
  const [rows, setRows] = useState<readonly SpecialistSummary[]>(() => listCache?.rows ?? []);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    void readSpecialistList().then((r) => {
      if (live) setRows(r);
    });
    return () => {
      live = false;
    };
  }, [enabled]);
  return enabled ? rows : [];
}

/** The specialist a tool input names: by id, then by name (case-insensitive). */
// CONTRACT: the offer card's input carries `specialist` (what the task passed:
// a name or an id) and `name` (PLAN A.8). The page resolves either against the
// list rather than trusting one of them to be the id.
export function resolveSpecialist(
  rows: readonly SpecialistSummary[],
  key: string,
  name = "",
): SpecialistSummary | null {
  const k = key.trim().toLowerCase();
  const n = name.trim().toLowerCase();
  return (
    rows.find((r) => r.id === key) ??
    rows.find((r) => r.name.toLowerCase() === k) ??
    (n ? rows.find((r) => r.name.toLowerCase() === n) : undefined) ??
    null
  );
}
