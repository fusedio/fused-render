// Platform detection + the one canonical "primary modifier" test.
//
// Every keyboard shortcut in the app must gate on isMod() rather than reading
// e.metaKey / e.ctrlKey directly. The check is deliberately EXCLUSIVE — on
// macOS it requires Meta and rejects Ctrl, elsewhere it requires Ctrl and
// rejects Meta — so Ctrl+C never fires the Cmd+C handler on a Mac (and vice
// versa on a Mac keyboard plugged into Linux). Accepting "either modifier"
// would make Ctrl+Backspace delete files on macOS, where that chord means
// something else entirely.
// Structural, so the one canonical test covers every event that carries the
// modifier flags: a DOM or React KeyboardEvent (shortcuts) and a DOM or React
// MouseEvent (Mod-click to add/remove a row from a multi-selection).
interface ModifierEvent {
  metaKey: boolean;
  ctrlKey: boolean;
}

// navigator.userAgentData is the modern signal (navigator.platform is
// deprecated but still the only thing Safari reports), userAgent is the last
// resort. Guarded for undefined navigator so importing this module is safe in
// a non-DOM context (SSR, tests, node tooling).
function detectMac(): boolean {
  if (typeof navigator === "undefined") return false;
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
  const source = nav.userAgentData?.platform ?? nav.platform ?? nav.userAgent ?? "";
  return /mac/i.test(source);
}

export const isMac: boolean = detectMac();

export function isMod(e: ModifierEvent): boolean {
  return isMac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
}

/**
 * ONE CHORD, SPELLED THE WAY THE PLATFORM SPELLS CHORDS — ⌘↩ on macOS, Ctrl+Enter
 * off it. The rule `shortcuts.ts` documents ("⌘C on macOS vs Ctrl+C on
 * Windows/Linux"), as a function, because it had been left to each caller and
 * the first caller to inline it shipped `CtrlEnter` — two words run together,
 * naming no key anybody has (Bugbot, PR #1198).
 *
 * The glyphs SIT TOGETHER where they are symbols and take a `+` where they are
 * words, which is the whole of the rule: a symbol already reads as a key, a word
 * needs the join to stop being a different word.
 *
 * `mac` is a parameter with the module's own answer as its default, so the rule
 * is testable on either platform from either platform — the reason the inline
 * version went unnoticed is that on a Mac both spellings agree.
 */
export function chordLabel(parts: readonly string[], mac: boolean = isMac): string {
  return parts.join(mac ? "" : "+");
}

// Display glyphs for the cheat sheet and menu accelerators. Mac users read
// symbols; Windows/Linux users read words.
export const MOD_LABEL: string = isMac ? "⌘" : "Ctrl";
export const ALT_LABEL: string = isMac ? "⌥" : "Alt";
export const SHIFT_LABEL: string = isMac ? "⇧" : "Shift";
export const ENTER_LABEL: string = isMac ? "↩" : "Enter";
export const BACKSPACE_LABEL: string = isMac ? "⌫" : "Backspace";
export const DELETE_LABEL: string = isMac ? "⌦" : "Delete";
