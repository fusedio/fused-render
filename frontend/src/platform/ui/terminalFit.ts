// Fit a terminal only against a container that has a real box. A hidden
// (display:none), detached or not-yet-laid-out element measures 0x0: FitAddon
// then leaves xterm at its 80x24 default, and remembering that as "the fitted
// size" poisons the next pty's create-time size (terminalSizeHint.ts) and any
// resize it sends makes the shell redraw its prompt (zsh's PROMPT_SP padding)
// at a width the replayed bytes will not be shown at.
import { rememberTerminalSize } from "@platform/ui/terminalSizeHint";

export function isLaidOut(el: { clientWidth: number; clientHeight: number }): boolean {
  return el.clientWidth > 0 && el.clientHeight > 0;
}

/** Fit and remember the size, or do nothing when `el` has no box. Returns
 * whether a fit ran. A later ResizeObserver tick (0x0 -> real) retries. */
export function fitWhenVisible(
  el: { clientWidth: number; clientHeight: number },
  fit: { fit(): void },
  term: { rows: number; cols: number },
): boolean {
  if (!isLaidOut(el)) return false;
  fit.fit();
  rememberTerminalSize(term.rows, term.cols);
  return true;
}
