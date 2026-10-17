// Client half of the terminal stream's flow control (the server half is
// PtySession.pause/resume in fused_render/pty_session.py). xterm's `write()`
// only queues: a flooding program (`cat bigfile`, a build log) delivers bytes
// faster than the parser and renderer drain them, the queue grows without
// bound, and keystrokes (Ctrl-C included) wait behind megabytes of output.
// This counts the bytes handed to `term.write` that its completion callback
// has not yet acknowledged, and asks the server to stop reading the pty above
// a high watermark and to carry on below a low one. The gap between the two
// stops it flapping on every chunk. Kept out of TerminalView.tsx, which has
// no test harness, so the accounting is unit-tested on its own.

/** Pending bytes above which the server is asked to pause. */
export const FLOW_HIGH_BYTES = 1024 * 1024;
/** Pending bytes below which a paused stream is resumed. */
export const FLOW_LOW_BYTES = 256 * 1024;

/** While still above HIGH, a pause is re-sent this often. The server drops a
 * pause after `_PAUSE_MAX_S` (5 s, pty_session.py) so a wedged tab cannot
 * freeze the shell; this stays well below that so a long flood is never left
 * running unthrottled after the expiry. */
export const REPAUSE_MS = 2000;

export interface FlowControlSink {
  pause(): void;
  resume(): void;
}

export interface FlowControl {
  /** Account for a chunk about to be handed to `term.write`; pass the
   * returned function as that call's completion callback. */
  chunk(length: number): () => void;
  /** Forget everything in flight (a reattach: `term.reset()` plus a fresh
   * socket, so the server's pause state is gone too). Completion callbacks of
   * chunks counted before the reset are ignored. */
  reset(): void;
}

export function createFlowControl(
  sink: FlowControlSink,
  opts: { now?: () => number } = {},
): FlowControl {
  const now = opts.now ?? Date.now;
  let lastPauseAt = 0;
  let pending = 0;
  let paused = false;
  let generation = 0;
  return {
    chunk(length) {
      const gen = generation;
      pending += length;
      if (pending > FLOW_HIGH_BYTES && (!paused || now() - lastPauseAt >= REPAUSE_MS)) {
        paused = true;
        lastPauseAt = now();
        sink.pause();
      }
      return () => {
        if (gen !== generation) return;
        pending = Math.max(0, pending - length);
        if (paused && pending < FLOW_LOW_BYTES) {
          paused = false;
          sink.resume();
        }
      };
    },
    reset() {
      generation += 1;
      pending = 0;
      paused = false;
    },
  };
}
