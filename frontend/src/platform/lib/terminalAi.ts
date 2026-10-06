// Pure logic behind the terminal's "Ask Claude" (selection) and "Fix with AI"
// (failed command) affordances. TerminalView.tsx is deliberately untested, so
// everything that can be wrong without a real canvas lives here.

export type ShellFailure = { command: string; exitCode: number };
export type OscState = {
  pendingCommand: string;
  failure: ShellFailure | null;
  /** True between 133;C and its 133;D. The shims also emit 133;D on the first
   * prompt (carrying whatever `$?` the rc files left), which is no command the
   * user ran, so a D outside a C..D pair is ignored. */
  running: boolean;
};

export function initialOscState(): OscState {
  return { pendingCommand: "", failure: null, running: false };
}

/** Mirrors `_unescape_command` in fused_render/shell_integration.py: the shims
 * escape `\` as `\\` and `;`, newline, ESC, BEL as `\xNN`. */
function unescapeCommand(text: string): string {
  let out = "";
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "\\" && i + 1 < text.length) {
      const next = text[i + 1];
      if (next === "\\") {
        out += "\\";
        i += 1;
        continue;
      }
      if (next === "x" && i + 3 < text.length) {
        const hex = text.slice(i + 2, i + 4);
        if (/^[0-9a-fA-F]{2}$/.test(hex)) {
          out += String.fromCharCode(parseInt(hex, 16));
          i += 3;
          continue;
        }
      }
    }
    out += ch;
  }
  return out;
}

/** Fold one shell-integration OSC (133 or 633) into the failure tracker. Exit
 * 130 (Ctrl+C) is not reported: the user interrupted on purpose. */
export function reduceShellOsc(state: OscState, ident: 133 | 633, data: string): OscState {
  if (ident === 633) {
    if (data.startsWith("E;")) return { ...state, pendingCommand: unescapeCommand(data.slice(2)).trim() };
    return state;
  }
  if (data === "C" || data.startsWith("C;")) return { ...state, failure: null, running: true };
  if (data === "D" || data.startsWith("D;")) {
    if (!state.running) return state;
    const n = data.startsWith("D;") ? parseInt(data.slice(2), 10) : NaN;
    if (Number.isFinite(n) && n !== 0 && n !== 130) {
      return { ...state, running: false, failure: { command: state.pendingCommand, exitCode: n } };
    }
    return { ...state, running: false, failure: null };
  }
  return state;
}

/** Top (px, within the surface) for the selection pill: just under the row the
 * selection ends on. `endRow` is a 0-based buffer row. xterm's typings call
 * `getSelectionPosition()` coordinates 1-based, but the implementation returns
 * its internal 0-based buffer coordinates (the same ones `select()` takes), so
 * the caller passes `end.y` unchanged. Null when that row is scrolled out. */
export function selectionPillTop(a: {
  endRow: number;
  viewportY: number;
  rows: number;
  surfaceHeight: number;
  pillHeight: number;
}): number | null {
  if (a.rows <= 0) return null;
  const visibleRow = a.endRow - a.viewportY;
  if (visibleRow < 0 || visibleRow >= a.rows) return null;
  const rowPx = a.surfaceHeight / a.rows;
  const top = (visibleRow + 1) * rowPx + 2;
  return Math.max(0, Math.min(top, a.surfaceHeight - a.pillHeight));
}

/** Cmd+L on macOS, Ctrl+Shift+L elsewhere (plain Ctrl+L is the shell's clear). */
export function isAskSelectionChord(
  e: { key: string; metaKey: boolean; ctrlKey: boolean; shiftKey: boolean; altKey: boolean; type: string },
  mac: boolean,
): boolean {
  if (e.type !== "keydown") return false;
  if (e.key !== "l" && e.key !== "L") return false;
  if (mac) return e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
  return e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey;
}
