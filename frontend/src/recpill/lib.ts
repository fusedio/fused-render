// The recording pill's pure helpers (recpill.ts is the DOM half): the level-meter bars, the elapsed clock and
// which artwork the pill wears. No DOM here, so bun can test it.

/** What the native panel pushes into the page (recording_pill.describe). */
export interface PillInfo {
  id: string;
  name: string;
  /** Absolute path of the app folder that started the recording; "" for none. */
  app: string;
  icon: string | null;
  flavor: "render" | "bot";
  canPause: boolean;
  mode: string;
}

/** GET /api/capture/{id}/state. */
export interface PillState {
  state?: string;
  paused?: boolean;
  seconds?: number;
  level?: number;
}

export const BAR_COUNT = 5;
/** Widths at rest as a fraction of the widest (20 px): Notion's uneven taper. */
export const BAR_REST = [9 / 20, 12 / 20, 15 / 20, 11 / 20, 6 / 20];
/** How far each bar grows toward full width at level 1: the middle bars lead. */
const BAR_GAIN = [0.7, 0.9, 1, 0.9, 0.7];

const clamp01 = (n: number) => (Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0);

/** Perceptual curve: the backend maps dB linearly over a -50 dB floor, so speech lands low; sqrt lifts it. */
export function shapeLevel(level: number): number {
  return Math.sqrt(clamp01(level));
}

/** Fast attack, slow release: a louder reading shows at once, a quieter one decays 15% per poll. */
export function smoothLevel(shaped: number, displayed: number): number {
  return Math.max(shaped, displayed * 0.85);
}

/** Turn one 0..1 (already shaped) level into BAR_COUNT bar widths (0..1 of the widest). Each bar grows from its rest
 *  width toward full by its gain; a paused or silent pill rests on the taper. */
export function bars(level: number, paused = false): number[] {
  const lv = paused ? 0 : clamp01(level);
  return BAR_REST.map((rest, i) => rest + (1 - rest) * Math.min(1, lv * BAR_GAIN[i]));
}

/** m:ss, or h:mm:ss from an hour. Garbage and negatives read 0:00. */
export function formatElapsed(seconds: number): string {
  const s = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/** Which artwork the icon slot shows: the app's own icon, else the flavor's home card. */
export function iconFor(info: Pick<PillInfo, "icon" | "flavor">): { kind: "app" | "home"; src: string | null; scale: string } {
  if (info.icon) return { kind: "app", src: info.icon, scale: "100%" };
  return { kind: "home", src: null, scale: info.flavor === "bot" ? "124.3%" : "113.7%" };
}

/** A pause/resume the user just asked for that the server has not confirmed yet. */
export interface PendingPause { want: boolean; until: number }

/** Decide what the pill shows for `paused`: while a click is pending and the server still reports the old value
 *  (and the deadline has not passed) the wanted value wins; once the server agrees or time runs out, the server wins. */
export function resolvePaused(
  serverPaused: boolean | undefined,
  pending: PendingPause | null,
  now: number,
): { paused: boolean; pending: PendingPause | null } {
  const server = !!serverPaused;
  if (pending && server !== pending.want && now < pending.until) return { paused: pending.want, pending };
  return { paused: server, pending: null };
}
