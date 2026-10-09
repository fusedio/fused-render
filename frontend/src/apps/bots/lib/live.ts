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

/** A page rect as [left, top, width, height] in CSS px (what the probes return). */
export type Box = [number, number, number, number];
/**
 * A page rect -> the same box in stage coordinates: the frame's CSS px scale to the drawn image, offset by where the image
 * sits in the stage, plus the stage's own scroll. `dims` is how many CSS px the frame covers (frameDims).
 */
export function toStageBox(r: Box, img: RectLike, stage: RectLike, dims: [number, number], scroll = { x: 0, y: 0 }): { left: number; top: number; width: number; height: number } {
  const sx = img.width / (dims[0] || 1), sy = img.height / (dims[1] || 1);
  return { left: img.left - stage.left + scroll.x + r[0] * sx, top: img.top - stage.top + scroll.y + r[1] * sy, width: r[2] * sx, height: r[3] * sy };
}
/** The CDP `button` for a held-button bitmask (DOM `buttons`): Chrome starts a drag only from moves that name it. */
export const heldButton = (buttons: number): "left" | "right" | "middle" | "none" =>
  buttons & 1 ? "left" : buttons & 2 ? "right" : buttons & 4 ? "middle" : "none";
/** i moved by delta inside [0, n), wrapping. */
export const wrapIndex = (i: number, delta: number, n: number): number => (n > 0 ? (((i + delta) % n) + n) % n : 0);
/** A key that changes a field's text (so datalist suggestions should refresh): no ⌘/Ctrl chord, not a navigation or modifier key. */
export const typesText = (e: KeyLike): boolean =>
  !e.metaKey && !e.ctrlKey && !["Tab", "Escape", "Enter", "Shift", "Control", "Alt", "Meta", "CapsLock"].includes(e.key) && !e.key.startsWith("Arrow") && !/^F\d+$/.test(e.key);
/** One HTTP auth challenge's identity (source | origin | realm): a repeat means the password was wrong. */
export const authKey = (ch: { source?: string; origin?: string; realm?: string }): string => `${ch.source || ""}|${ch.origin || ""}|${ch.realm || ""}`;

export interface LastDown { t: number; x: number; y: number; n: number }
/** mousedown click counting: another press within 400 ms and 6 px of the last one is a double (triple, …) click. */
export function nextDown(last: LastDown, t: number, p: { x: number; y: number }): LastDown {
  const n = t - last.t < 400 && Math.hypot(p.x - last.x, p.y - last.y) < 6 ? last.n + 1 : 1;
  return { t, x: p.x, y: p.y, n };
}

// US-layout key table (Puppeteer's USKeyboardLayout): code -> [Windows virtual-key code, unshifted char, shifted char].
// The VK has to come from the viewer's own `event.keyCode` (what DevTools' screencast forwards) or from this table,
// never from the character: Blink maps the VK to an editing command BEFORE it reads `text`, and ord(".") is 46 =
// VK_DELETE (deletes forward instead of typing a dot), ord("'") = 39 = ArrowRight (moves the caret). Verified against Chrome 155.
export const KEYS: Record<string, [number, string, string]> = {};
")!@#$%^&*(".split("").forEach((s, i) => { KEYS["Digit" + i] = [48 + i, String(i), s]; });
for (let i = 0; i < 26; i++) KEYS["Key" + String.fromCharCode(65 + i)] = [65 + i, String.fromCharCode(97 + i), String.fromCharCode(65 + i)];
Object.assign(KEYS, {
  Minus: [189, "-", "_"], Equal: [187, "=", "+"], BracketLeft: [219, "[", "{"], BracketRight: [221, "]", "}"], Backslash: [220, "\\", "|"],
  Semicolon: [186, ";", ":"], Quote: [222, "'", '"'], Backquote: [192, "`", "~"], Comma: [188, ",", "<"], Period: [190, ".", ">"],
  Slash: [191, "/", "?"], Space: [32, " ", " "],
} satisfies Record<string, [number, string, string]>);
/** char -> code, for events whose `code` is missing (tests) or not in the table. */
export const CHAR_CODE: Record<string, string> = {};
for (const [c, [, a, b]] of Object.entries(KEYS)) { CHAR_CODE[a] ??= c; CHAR_CODE[b] ??= c; }
export const NAMED_VK: Record<string, number> = { Enter: 13, Tab: 9, Backspace: 8, Delete: 46, Escape: 27, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Home: 36, End: 35, PageUp: 33, PageDown: 34, Insert: 45, Shift: 16, Control: 17, Alt: 18, Meta: 91, CapsLock: 20 };
for (let n = 1; n <= 12; n++) NAMED_VK["F" + n] = 111 + n;
// macOS editing commands (Playwright's macEditingCommands): CDP key events bypass Cocoa key bindings, so without
// `commands` ⌘A/⌘C/⌘X/⌘V/⌘Z and ⌘/⌥+arrow do nothing in the bot's Chrome. Ctrl is read like ⌘ so a non-Mac keyboard works too.
export const EDIT_CMDS: Record<string, string> = { a: "SelectAll", c: "Copy", x: "Cut", v: "Paste", z: "Undo" };
export const CMD_ARROW: Record<string, string> = { ArrowLeft: "MoveToBeginningOfLine", ArrowRight: "MoveToEndOfLine", ArrowUp: "MoveToBeginningOfDocument", ArrowDown: "MoveToEndOfDocument" };
export const WORD_ARROW: Record<string, string> = { ArrowLeft: "MoveWordLeft", ArrowRight: "MoveWordRight", ArrowUp: "MoveToBeginningOfParagraph", ArrowDown: "MoveToEndOfParagraph" };

export interface KeyLike extends ModsLike { type: string; key: string; code: string; keyCode?: number; repeat?: boolean }
export interface KeyParams {
  type: "keyDown" | "rawKeyDown" | "keyUp"; key: string; code: string; modifiers: number; autoRepeat: boolean;
  windowsVirtualKeyCode?: number; text?: string; unmodifiedText?: string; commands?: string[];
}
/** What one DOM keyboard event becomes: an Input.dispatchKeyEvent, an Input.insertText (a printable character the US table
 *  does not know: é, 日, emoji, a non-US layout), or nothing (that character's keyup). Never `nativeVirtualKeyCode`: on macOS
 *  it flips the tab hidden and the screencast stops. */
export type KeyAction = { kind: "key"; params: KeyParams } | { kind: "insert"; text: string } | null;
export function keyAction(e: KeyLike): KeyAction {
  const mods = CDP_MODS(e), key = e.key, down = e.type === "keydown";
  const prim = e.metaKey || e.ctrlKey, word = e.altKey, sel = e.shiftKey ? "AndModifySelection" : "";
  const mk = (vk: number, text?: string, commands?: string[]): KeyAction => {
    const p: KeyParams = { type: down ? (text ? "keyDown" : "rawKeyDown") : "keyUp", key, code: e.code, modifiers: mods, autoRepeat: !!e.repeat };
    if (vk) p.windowsVirtualKeyCode = vk;
    if (down && text) p.text = p.unmodifiedText = text;
    if (down && commands?.length) p.commands = commands;
    return { kind: "key", params: p };
  };
  if ([...key].length > 1 && /^[\x00-\x7f]+$/.test(key)) {  // a named key (Enter, ArrowLeft, F5); a multi-code-point emoji is text
    let cmds: string[] | undefined;
    if (CMD_ARROW[key] && prim) cmds = [CMD_ARROW[key] + sel];
    else if (WORD_ARROW[key] && word) cmds = [WORD_ARROW[key] + sel];
    else if (key === "Backspace" && prim) cmds = ["DeleteToBeginningOfLine"];
    else if (key === "Backspace" && word) cmds = ["DeleteWordBackward"];
    else if (key === "Delete" && word) cmds = ["DeleteWordForward"];
    else if (key === "Home" || key === "End") cmds = [(key === "Home" ? "MoveToBeginningOfLine" : "MoveToEndOfLine") + sel];
    return mk(e.keyCode || NAMED_VK[key] || 0, key === "Enter" ? "\r" : undefined, cmds);
  }
  const code = e.code in KEYS ? e.code : CHAR_CODE[key] || "";
  if (prim && !(e.ctrlKey && e.altKey)) {  // a chord: no text, a command where macOS needs one
    const lk = key.toLowerCase(), cmd = EDIT_CMDS[lk];
    return mk(code ? KEYS[code][0] : e.keyCode || 0, undefined, cmd && !e.altKey ? [lk === "z" && e.shiftKey ? "Redo" : cmd] : undefined);
  }
  if (code && (key === KEYS[code][1] || key === KEYS[code][2])) return mk(KEYS[code][0], key);
  return down ? { kind: "insert", text: key } : null;
}


/** The URL bar shows nothing for a blank page. */
export const showUrl = (u: string | null | undefined): string => (!u || u === "about:blank" ? "" : u);

/** Where the driven page is. `browser.url` is what the worker recorded after its last action (session/tabs.json), so while
 *  you drive over the live socket it never moves; `tabs[]` comes from Chrome's live list on every detail poll. The URL bar
 *  once mirrored the recorded url and reset itself to it 400 ms after every navigation you made (store poll). */
export const pageUrl = (b: { browser?: { url?: string; tabs?: Array<{ active: boolean; url: string }> } } | undefined): string | undefined =>
  b?.browser?.tabs?.find((t) => t.active)?.url || b?.browser?.url;

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
export function imessageStatus(handle: string, s: { running: boolean; error: string; last_in: number | null; last_out: number | null; identity?: { mode: string; label: string } } | null, nowMs: number = Date.now()): string {
  if (!handle) return "Off. Enter a number and save; texts from it start tasks within a few seconds.";
  if (!s) return "Bridge status unknown yet.";
  if (s.error) return "Bridge not running: " + s.error;
  if (!s.running) return "Bridge starting…";
  const ago = (t: number | null) => (t ? `${Math.max(0, Math.round((nowMs / 1000 - t) / 60))} min ago` : "never");
  // Who the bot speaks as (docs §10 D7): through your own account its texts carry "@name"; a bot Apple ID needs no prefix.
  const who = s.identity?.mode === "dedicated" ? ` Texting as ${s.identity.label || "a separate account"}.`
    : s.identity?.mode === "own" ? " Texting from your own account, so each text starts with @name." : "";
  return `Bridge running · last text in ${ago(s.last_in)}, last reply out ${ago(s.last_out)}.${who}`;
}
