// The size a NEW pty should start at. The session is created over HTTP before
// the xterm that will show it exists, so there is no fitted size to send yet;
// without one the pty is 0x0 and zsh assumes 80 columns for its first prompt
// (a stray inverse `%` is the PROMPT_SP padding drawn for the wrong width).
// The server applies whatever rows/cols the create request carries before the
// shell starts (fused_render/pty_session.py), so this module's job is a good
// guess: the last size any terminal here actually fitted, else an estimate
// from the drawer's box and the terminal font's cell width. A wrong guess is
// harmless — the real fit still arrives as a resize — it just may not
// suppress the glitch.
import { documentCssVarLookup, loadTerminalFont, terminalFontFamily } from "@platform/ui/terminalTheme";

export interface GridSize {
  rows: number;
  cols: number;
}

// TerminalView.tsx's own metrics, which the estimate has to subtract the way
// FitAddon does: `.term-view` padding (8px each side) and the scrollbar.
const PADDING_PX = 16;
const SCROLLBAR_PX = 14;
const TABS_PX = 34;
const FONT_PX = 12;
// xterm's cell height for this font at lineHeight 1 is about fontSize * 1.2.
const CELL_HEIGHT_PX = 15;

let lastFitted: GridSize | null = null;

function validDim(n: number): boolean {
  return Number.isInteger(n) && n >= 1 && n <= 65535;
}

/** Called by TerminalView with every fitted size. Ignores nonsense. */
export function rememberTerminalSize(rows: number, cols: number): void {
  if (validDim(rows) && validDim(cols)) lastFitted = { rows, cols };
}

/** Test-only. */
export function forgetTerminalSize(): void {
  lastFitted = null;
}

/** Grid that fits in `box` for a cell of `cell` px, or null when either is
 * degenerate (a hidden drawer, a failed measurement). */
export function estimateGrid(
  box: { width: number; height: number },
  cell: { width: number; height: number },
): GridSize | null {
  if (!(cell.width > 0) || !(cell.height > 0)) return null;
  const cols = Math.floor((box.width - PADDING_PX - SCROLLBAR_PX) / cell.width);
  const rows = Math.floor((box.height - PADDING_PX - TABS_PX) / cell.height);
  return validDim(cols) && validDim(rows) ? { rows, cols } : null;
}

function measureCellWidth(): number {
  try {
    const ctx = document.createElement("canvas").getContext("2d");
    if (!ctx) return 0;
    ctx.font = `${FONT_PX}px ${terminalFontFamily(documentCssVarLookup())}`;
    return ctx.measureText("M").width;
  } catch {
    return 0;
  }
}

/** The size to create a pty with, or null when there is no basis for one (the
 * server then keeps its no-size behaviour). Never rejects. */
export async function terminalSizeHint(): Promise<GridSize | null> {
  if (lastFitted) return lastFitted;
  try {
    const drawer = typeof document === "undefined" ? null : document.querySelector(".term-drawer");
    if (!drawer) return null;
    // Measure the cell on the real font, not the fallback's.
    await loadTerminalFont();
    return estimateGrid(
      { width: drawer.clientWidth, height: drawer.clientHeight },
      { width: measureCellWidth(), height: CELL_HEIGHT_PX },
    );
  } catch {
    return null;
  }
}
