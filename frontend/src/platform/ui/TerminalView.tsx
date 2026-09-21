// The status-bar terminal's xterm.js surface: one `Terminal` + `FitAddon`
// per mounted view, piped to a `TerminalSession` (platform/lib/terminalSession.ts,
// which owns the WebSocket and knows nothing about xterm or the DOM). This
// file is the only place those two meet.
//
// DELIBERATELY UNTESTED (PLAN-status-bar-terminal.md's Decisions): a headless
// renderer cannot measure a canvas or run a real resize/layout pass, so a
// test here could only assert that we called xterm's own API — the protocol
// logic that can actually be wrong (framing, backoff, exit handling) lives in
// terminalSession.ts, which has real tests. Keep this component thin enough
// that "wrong" here is visually obvious rather than a a logic bug worth a
// unit test.
//
// OWNS THE SESSION: this component constructs the `TerminalSession` for the
// `id` it is given (an id `TerminalDrawer` already created/loaded via
// `createTerminalSession`/localStorage) rather than receiving one ready-made
// — `TerminalSession`'s callbacks are fixed at construction, and the one
// thing that needs to receive its output is the xterm instance this
// component owns, so construction has to happen here.
import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

import { TerminalSession, type TerminalStatus } from "@platform/lib/terminalSession";

export interface TerminalViewProps {
  /** A live pty session id (fused_render/pty_session.py). */
  id: string;
  /** The child process has exited; the caller (TerminalDrawer) owns the
   * "press Enter to start a new shell" affordance (Task 6), not this view. */
  onExit?: (code: number | null) => void;
  onStatus?: (status: TerminalStatus) => void;
}

export default function TerminalView({ id, onExit, onStatus }: TerminalViewProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Re-runs whenever `id` changes (a restarted shell gets a new session id
  // from the caller, which this effect treats as a fresh mount).
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const term = new Terminal({
      convertEol: true,
      fontSize: 12,
      cursorBlink: true,
      theme: { background: "transparent" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(el);
    fit.fit();

    const session = new TerminalSession({
      id,
      onData: (chunk) => term.write(chunk),
      onExit: (code) => onExit?.(code),
      onStatus: (status) => onStatus?.(status),
    });
    // Send the initial size once the pty exists on the other end, and again
    // on every subsequent xterm-driven resize (a wrap change from `fit()`,
    // not just a user keystroke).
    session.resize(term.rows, term.cols);

    const dataSub = term.onData((data) => session.write(data));
    const resizeSub = term.onResize(({ rows, cols }) => session.resize(rows, cols));

    const observer = new ResizeObserver(() => {
      fit.fit();
    });
    observer.observe(el);

    return () => {
      observer.disconnect();
      dataSub.dispose();
      resizeSub.dispose();
      session.dispose();
      term.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onExit/onStatus
    // are event callbacks, not reactive inputs; re-subscribing to them would
    // tear down and rebuild the whole terminal on every parent render.
  }, [id]);

  return <div className="term-view" ref={containerRef} />;
}
