// A LOAD TIMELINE per opened file, for views whose first paint is a chain of
// network steps rather than one document load (SPEC §53.3 NV-12: the canvas
// node view, whose open walks a gate verdict, a lazy chunk, three or four API
// calls and a cross-origin workbench boot before the node is on screen).
//
// Each step marks the timeline when it lands. The marks go to the Performance
// API under `fused-load:<key>:<stage>` (so a DevTools Performance recording
// shows them on its timings track) and into an in-memory record a developer
// can read back:
//
//   window.__fusedLoadTimelines()      // every timeline this page recorded
//
// With the shell's debug flag on (`localStorage.setItem("fused-render.debug",
// "1")`, the same flag the chat's debug views use) a finished timeline also
// prints one line to the console — total, then each stage's time since the
// one before it.
//
// Recording only: nothing here changes what a view does, and it never throws
// (a missing Performance API, or a stage marked twice, is a no-op).
import { debugEnabled } from "@platform/lib/debug-flag";

export interface LoadStage {
  stage: string;
  /** ms since the timeline began. */
  at: number;
}

export interface LoadTimeline {
  key: string;
  label: string;
  /** performance.now() when it began; also its ms since the page started. */
  t0: number;
  stages: LoadStage[];
  /** The stage that ended it ("ready", "signed out", …), or null while open. */
  end: string | null;
  /** ms from begin to end, or null while open. */
  total: number | null;
  /** Durations measured before the timeline began (the stat request that
   *  brought the view here), printed beside the total. */
  before: Record<string, number>;
}

const PREFIX = "fused-load";
const timelines = new Map<string, LoadTimeline>();
/** Finished timelines, oldest first, so a reopen does not erase the last run. */
const history: LoadTimeline[] = [];
const HISTORY_MAX = 20;

const now = (): number => (typeof performance !== "undefined" ? performance.now() : Date.now());

function perfMark(name: string): void {
  try {
    performance.mark(name);
  } catch {
    // No Performance API (a bare test runtime): the in-memory record suffices.
  }
}

function perfMeasure(name: string, start: string, end: string): void {
  try {
    performance.measure(name, start, end);
  } catch {
    // A mark cleared or never made: the record still has the numbers.
  }
}

/** Start (or restart) the timeline for `key`. A reopen of the same file is a
 *  new load, so whatever was recorded for it before is archived. */
export function beginLoad(key: string, label: string, before: Record<string, number> = {}): void {
  const prev = timelines.get(key);
  if (prev && prev.end === null) archive(prev);
  const t: LoadTimeline = { key, label, t0: now(), stages: [], end: null, total: null, before };
  timelines.set(key, t);
  perfMark(`${PREFIX}:${key}:begin`);
}

/** Note that `stage` landed. Only the FIRST mark of a stage counts, and a
 *  finished timeline takes no more marks. A mark that passes `label` is a
 *  view's first one and begins a timeline when none exists (a view mounted
 *  without the surface that usually begins it); any other mark with no
 *  timeline is dropped, so a late step can never start a load that nothing
 *  will finish (a finished record evicted from the map, a callback after
 *  its view is gone). */
export function markLoad(key: string, stage: string, label?: string): void {
  let t = timelines.get(key);
  if (t && t.end !== null) return;
  if (!t) {
    if (label === undefined) return;
    beginLoad(key, label);
    t = timelines.get(key)!;
  }
  if (t.stages.some((s) => s.stage === stage)) return;
  t.stages.push({ stage, at: now() - t.t0 });
  perfMark(`${PREFIX}:${key}:${stage}`);
}

/** End the timeline at `stage` (marking it), measure it, and — with the
 *  debug flag on — print its summary. A timeline already ended is left alone. */
export function finishLoad(key: string, stage: string): void {
  const t = timelines.get(key);
  if (!t || t.end !== null) return;
  markLoad(key, stage);
  t.end = stage;
  t.total = now() - t.t0;
  perfMeasure(`${PREFIX}:${key}`, `${PREFIX}:${key}:begin`, `${PREFIX}:${key}:${stage}`);
  archive(t);
  if (debugEnabled()) console.info(formatTimeline(t));
}

/** The open timeline for `key`, if any (tests, and the stages that only
 *  record while a load is in progress). */
export function openTimeline(key: string): LoadTimeline | null {
  const t = timelines.get(key);
  return t && t.end === null ? t : null;
}

function archive(t: LoadTimeline): void {
  if (history.includes(t)) return;
  history.push(t);
  if (history.length > HISTORY_MAX) history.shift();
  // Finished records stay in the map only so a stray late mark cannot begin
  // a new load; keep at most HISTORY_MAX of them (oldest first — a Map
  // iterates in insertion order).
  let finished = 0;
  for (const r of timelines.values()) if (r.end !== null) finished++;
  for (const [k, r] of timelines) {
    if (finished <= HISTORY_MAX) break;
    if (r.end !== null) {
      timelines.delete(k);
      finished--;
    }
  }
}

/** Every timeline this page recorded: the archived ones, then any still open. */
export function loadTimelines(): LoadTimeline[] {
  const open = [...timelines.values()].filter((t) => t.end === null);
  return [...history, ...open].map((t) => ({ ...t, stages: t.stages.map((s) => ({ ...s })), before: { ...t.before } }));
}

const ms = (n: number): string => `${Math.round(n)} ms`;

/** One line: the label, how it ended and in how long, then each stage with
 *  the time since the one before it — which is where a slow step shows. */
export function formatTimeline(t: LoadTimeline): string {
  let prev = 0;
  const parts = t.stages.map((s) => {
    const part = `${s.stage} +${ms(s.at - prev)}`;
    prev = s.at;
    return part;
  });
  const head =
    t.end === null
      ? `[load] ${t.label}: still loading after ${ms(prev)}`
      : `[load] ${t.label}: ${t.end} in ${ms(t.total ?? prev)}`;
  const before = Object.entries(t.before).map(([k, v]) => `${k} ${ms(v)}, `).join("");
  return `${head} (${before}page +${ms(t.t0)}) — ${parts.join(" · ")}`;
}

/** The duration of the most recent request whose URL contains `match`, from
 *  the Resource Timing buffer, or null when there is none (or no API). */
export function lastRequestMs(match: string): number | null {
  try {
    const entries = performance.getEntriesByType("resource");
    for (let i = entries.length - 1; i >= 0; i--) {
      if (entries[i].name.includes(match)) return entries[i].duration;
    }
  } catch {
    // No Resource Timing: nothing to report.
  }
  return null;
}

if (typeof window !== "undefined") {
  (window as unknown as { __fusedLoadTimelines?: () => LoadTimeline[] }).__fusedLoadTimelines = loadTimelines;
}

/** Tests only: forget every timeline. */
export function resetLoadTimelines(): void {
  timelines.clear();
  history.length = 0;
}
