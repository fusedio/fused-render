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
export const BAR_MIN = 0.12;

/** Turn one 0..1 level into BAR_COUNT bar heights (0..1). The middle bar leads, the outer ones trail it, so the
 *  meter breathes like a voice rather than moving as one block. A paused or silent pill rests at BAR_MIN. */
export function bars(level: number, paused = false, count = BAR_COUNT): number[] {
  const lv = paused || !Number.isFinite(level) ? 0 : Math.min(1, Math.max(0, level));
  const mid = (count - 1) / 2;
  return Array.from({ length: count }, (_, i) => {
    const falloff = 1 - Math.abs(i - mid) / (mid + 1.5);
    return BAR_MIN + (1 - BAR_MIN) * Math.min(1, lv * falloff * 1.25);
  });
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
