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

    const dataSub = term.onData((data) => session.write(data));
    const resizeSub = term.onResize(({ rows, cols }) => session.resize(rows, cols));

    fit.fit();

    const session = new TerminalSession({
      id,
      onData: (chunk) => term.write(chunk),
      onExit: (code) => onExit?.(code),
      onStatus: (status) => {
        if (status === "open") {
          // The server replays the full scrollback on EVERY attach
          // (fused_render/server/routers/terminal.py), and `connect()` is
          // also the reconnect path — nothing else clears xterm's buffer
          // between attempts, so a bare reconnect would paint that replay
          // on top of whatever is already on screen. `reset()` here is a
          // no-op the first time (the pane is already empty) and prevents
          // duplicated output on every subsequent one. Re-send the current
          // size right after: the pty keeps whatever size it had across a
          // reattach, but the SOCKET does not, so every open (first
          // connect and reconnect alike) has to resend it — sending it
          // eagerly right after `new TerminalSession(...)` (the previous
          // code) silently dropped the frame, since `TerminalSession.resize`
          // no-ops until the socket reaches OPEN.
          term.reset();
          session.resize(term.rows, term.cols);
        }
        onStatus?.(status);
      },
    });

    // Coalesced to at most one `fit.fit()` per animation frame: a resize
    // drag (TerminalDrawer.tsx) can hand this observer a burst of
    // intermediate layout sizes within a single frame, and each `fit.fit()`
    // that actually changes rows/cols re-fires `term.onResize` ->
    // `session.resize()` -> a WebSocket frame -> the server's ioctl ->
    // SIGWINCH -> a shell prompt redraw. Running that whole chain once per
    // observed size instead of once per frame is the flicker.
    let fitRaf: number | null = null;
    const observer = new ResizeObserver(() => {
      if (fitRaf !== null) return;
      fitRaf = requestAnimationFrame(() => {
        fitRaf = null;
        fit.fit();
      });
    });
    observer.observe(el);

    return () => {
      if (fitRaf !== null) cancelAnimationFrame(fitRaf);
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

  // `.term-view` carries the padding; `.term-view-surface` is the unpadded
  // element xterm actually opens into and measures against (see the CSS
  // comment in notifications.css for why the split matters to FitAddon).
  return (
    <div className="term-view">
      <div className="term-view-surface" ref={containerRef} />
    </div>
  );
}
