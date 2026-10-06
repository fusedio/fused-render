// Pure helpers behind the live view and the dialogs (OpenBot live.js + dialogs.js), kept free of React and the DOM
// so bun can test them: the page-pixel mapping, the CDP key table, the URL-bar rule, routine labels and usage weights.
import type { Routine } from "./api";
import { fmtWhen } from "./format";

// ------------------------------------------------------------------ live view ----
/** The screencast request: JPEG q60 up to 1920x1200 (OpenBot CAST). */
export const CAST = { quality: 60, maxWidth: 1920, maxHeight: 1200 } as const;

export interface ModsLike { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }
/** CDP modifier bitmask: Alt 1, Ctrl 2, Meta 4, Shift 8. */
export const CDP_MODS = (e: ModsLike): number => (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
export const BTN = ["left", "middle", "right", "back", "forward"] as const;

/** A screencast frame's metadata (Page.screencastFrame params.metadata); deviceWidth/Height are CSS pixels. */
export interface FrameMeta { deviceWidth?: number; deviceHeight?: number; [k: string]: unknown }
export interface RectLike { left: number; top: number; width: number; height: number }

/** How many CSS pixels the frame covers: the frame's own metadata, else the bot's viewport, else the image's natural size. */
export function frameDims(meta: FrameMeta | null | undefined, viewport: [number, number] | null | undefined, natural: [number, number]): [number, number] {
  if (meta?.deviceWidth) return [meta.deviceWidth, meta.deviceHeight || 0];
  return viewport || natural;
}

/**
 * Map a pointer position on the shown frame to CSS viewport pixels (what CDP expects); null when the frame has no
 * size yet or the point falls outside the page. The window is 1280x800 but the viewport is shorter (Chrome's UI takes
 * the rest), so assuming 800 sent clicks 12% too low: the frame metadata is the source of truth.
 */
export function toPageXY(clientX: number, clientY: number, rect: RectLike, natural: [number, number], meta: FrameMeta | null | undefined, viewport: [number, number] | null | undefined): { x: number; y: number } | null {
  if (!natural[0] || !rect.width) return null;
  const [vw, vh] = frameDims(meta, viewport, natural);
  const x = (clientX - rect.left) * vw / rect.width;
  const y = (clientY - rect.top) * vh / rect.height;
  if (x < 0 || y < 0 || x > vw || y > vh) return null;
  return { x: Math.round(x), y: Math.round(y) };
}

export interface LastDown { t: number; x: number; y: number; n: number }
/** mousedown click counting: another press within 400 ms and 6 px of the last one is a double (triple, …) click. */
export function nextDown(last: LastDown, t: number, p: { x: number; y: number }): LastDown {
  const n = t - last.t < 400 && Math.hypot(p.x - last.x, p.y - last.y) < 6 ? last.n + 1 : 1;
  return { t, x: p.x, y: p.y, n };
}

export const VK: Record<string, number> = { Enter: 13, Backspace: 8, Tab: 9, Escape: 27, " ": 32, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Delete: 46, Home: 36, End: 35, PageUp: 33, PageDown: 34, Shift: 16, Control: 17, Alt: 18, Meta: 91, CapsLock: 20 };
export const EDIT_CMDS: Record<string, string> = { a: "SelectAll", c: "Copy", x: "Cut", z: "Undo" };
export const EDIT_LINE_CMDS: Record<string, string> = { ArrowLeft: "MoveToBeginningOfLine", ArrowRight: "MoveToEndOfLine", Backspace: "DeleteToBeginningOfLine" };

export interface KeyLike extends ModsLike { type: string; key: string; code: string; repeat?: boolean }
export interface KeyParams {
  type: "keyDown" | "keyUp"; key: string; code: string; modifiers: number; autoRepeat: boolean;
  windowsVirtualKeyCode?: number; text?: string; unmodifiedText?: string; commands?: string[];
}
/** A DOM keyboard event as Input.dispatchKeyEvent params, with the editing commands macOS Chrome needs for ⌘A/⌘C/⌘X/⌘Z/⇧⌘Z and ⌘←/⌘→/⌘⌫. */
export function keyParams(e: KeyLike): KeyParams {
  const mods = CDP_MODS(e), key = e.key, down = e.type === "keydown";
  const p: KeyParams = { type: down ? "keyDown" : "keyUp", key, code: e.code, modifiers: mods, autoRepeat: !!e.repeat };
  const vk = VK[key] || (key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0);
  if (vk) p.windowsVirtualKeyCode = vk;
  if (down) {
    if (key.length === 1 && !(mods & ~8)) p.text = p.unmodifiedText = key;
    else if (key === "Enter") p.text = p.unmodifiedText = "\r";
    if ((mods & 6) && key.length === 1 && EDIT_CMDS[key.toLowerCase()]) {
      p.commands = [key.toLowerCase() === "z" && e.shiftKey ? "Redo" : EDIT_CMDS[key.toLowerCase()]];
    } else if ((mods & 4) && EDIT_LINE_CMDS[key]) {
      p.commands = [EDIT_LINE_CMDS[key]];
    }
  }
  return p;
}

/** The URL bar shows nothing for a blank page. */
export const showUrl = (u: string | null | undefined): string => (!u || u === "about:blank" ? "" : u);

/** #furl: only scheme-prefixed, dotted or localhost input is an address; bare words go to Google. */
export const isUrl = (u: string): boolean =>
  /^[a-z][a-z0-9+.-]*:\/\//i.test(u) || (!/\s/.test(u) && /^(localhost|[^\s/?#]+\.[^\s/?#]+)(:\d+)?([/?#]|$)/i.test(u));
export const furlTarget = (u: string): string => (isUrl(u) ? u : "https://www.google.com/search?q=" + encodeURIComponent(u));

// ------------------------------------------------------------------ dialogs ----
export const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
/** "every 60 min", "daily at 09:00 (Mon Tue Wed Thu Fri)", "once at Mon, Sep 8, 3:00 PM". */
export function routineLabel(r: Pick<Routine, "kind" | "minutes" | "time" | "weekdays" | "at">): string {
  if (r.kind === "interval") return `every ${r.minutes} min`;
  if (r.kind === "daily") return `daily at ${r.time}` + ((r.weekdays || []).length < 7 ? ` (${(r.weekdays || []).map((d) => DAYS[d]).join(" ")})` : "");
  return `once at ${fmtWhen(r.at)}`;
}

// Rough relative cost per call by model alias, in sonnet-calls; an estimate for ranking, not a bill.
export const MODEL_WEIGHT: Record<string, number> = { haiku: 0.3, sonnet: 1, opus: 5, fable: 5, "local-4b": 0, "local-9b": 0 };
export const weighted = (b: { models?: Record<string, number> | null }): number =>
  Object.entries(b.models || {}).reduce((s, [m, n]) => s + n * (MODEL_WEIGHT[m] ?? 1), 0);
/** The per-bot model chips, most calls first. */
export const modelChips = (models: Record<string, number> | null | undefined): [string, number][] =>
  Object.entries(models || {}).sort((a, b) => b[1] - a[1]);
/** The usage table order: live bots first, then model-weighted spend, then today's calls (deleted bots last). */
export function rankUsage<T extends { live: boolean; today: number; models?: Record<string, number> }>(rows: T[]): T[] {
  return rows.slice().sort((a, b) => (Number(b.live) - Number(a.live)) || weighted(b) - weighted(a) || b.today - a.today);
}

/** One line under the iMessage field: is the bridge reading Messages, and if not, why. */
export function imessageStatus(handle: string, s: { running: boolean; error: string; last_in: number | null; last_out: number | null } | null, nowMs: number = Date.now()): string {
  if (!handle) return "Off. Enter a number and save; texts from it start tasks within a few seconds.";
  if (!s) return "Bridge status unknown yet.";
  if (s.error) return "Bridge not running: " + s.error;
  if (!s.running) return "Bridge starting…";
  const ago = (t: number | null) => (t ? `${Math.max(0, Math.round((nowMs / 1000 - t) / 60))} min ago` : "never");
  return `Bridge running · last text in ${ago(s.last_in)}, last reply out ${ago(s.last_out)}.`;
}
