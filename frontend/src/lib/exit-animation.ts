// Exit animations for overlays that are unmounted by their CALLER.
//
// Every dialog in the app is rendered as `{open && <Modal …/>}` and the
// slide-over the same way, so an overlay has no `open` prop of its own and
// cannot keep itself mounted for an exit animation — the only thing it can
// delay is the `onClose` that makes the caller unmount it. That is exactly what
// this does: `request()` puts the overlay into a `closing` phase (the class the
// exit CSS keys off) and fires the real `onClose` once the animation has had its
// duration.
//
// DOM- and React-free on purpose so the timing is unit-testable
// (exit-animation.test.ts); `useDeferredClose` in lib/hooks.ts is the thin React
// wrapper that every overlay actually uses.

// Exit durations, pinned by comment to the motion tokens in shell.css: the CSS
// owns the actual transition, these only decide when the caller may unmount.
export const OVERLAY_EXIT_MS = 150; // --dur-med: modals, dialogs
export const PANEL_EXIT_MS = 200; // --dur-slow: the Home slide-over

export interface CloseDeferrer {
  // Begin (or keep) the exit. Idempotent: a second ✕/Esc/backdrop click landing
  // mid-animation must not restart it or queue a second onClose.
  request(): void;
  // Drop a pending close without firing onClose — for unmount cleanup, so a
  // timer can't call back into a component that is already gone.
  cancel(): void;
  readonly closing: boolean;
}

export function createCloseDeferrer(
  durationMs: number,
  onClose: () => void,
  onPhase: (closing: boolean) => void,
): CloseDeferrer {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return {
    request() {
      if (timer !== null) return;
      onPhase(true);
      // setTimeout even at duration 0: callers unmount inside onClose, and
      // doing that synchronously from the event handler that is still reading
      // the overlay's node is the hazard this indirection exists to avoid.
      timer = setTimeout(() => {
        timer = null;
        onClose();
      }, durationMs);
    },
    cancel() {
      if (timer === null) return;
      clearTimeout(timer);
      timer = null;
      onPhase(false);
    },
    get closing() {
      return timer !== null;
    },
  };
}
