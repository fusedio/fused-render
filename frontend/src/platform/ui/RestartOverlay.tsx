// BLOCKING OVERLAY FOR A RESTART-TO-UPDATE. Swapping the server takes the better
// part of a minute (teardown, then a cold start of the new bundle), and until
// now the page stayed fully usable meanwhile — every click in that window talks
// to a process that is going away, so things broke. From the instant a restart
// is requested (any window: the flow store is shared, restart-store.ts) this
// covers the whole window, app iframes included, and holds until the new server
// answers on a different version — at which point ServerStatusBanner's own
// `reduceProbe` reloads the page and this overlay goes with it.
//
// It is the shared `Modal` chassis in its `busy` posture (UpdateDialog's own
// refresh mode does the same): full-window scrim over every iframe, focus trap
// (an iframe is a focusable stop the chassis handles), no ✕, Esc and backdrop
// refused. If the restart does not come back inside RESTART_GIVE_UP_MS the flow
// ends in `gave-up` and the contents become an error with Dismiss / Try again
// rather than a spinner held forever. A press the running app never acted on
// (the server answers on the same version, no probe ever failed) ends sooner,
// in `stuck`: Dismiss only, because pressing again would be dropped the same
// way — the sentence tells the reader to quit and reopen the app themselves.
//
// Mounted once in the top document next to UpdateNotifier (App.tsx), behind
// the same !IS_EMBED guard: a pane drawing its own would stack a second scrim.
import { useEffect, useState } from "react";

import {
  restartInFlight,
  restartStageLabel,
  restartStuckBody,
  restartStuckTitle,
  type RestartStage,
} from "@platform/lib/restart-flow";
import { requestRestart, useRestartFlow } from "@platform/lib/restart-store";
import { displayName } from "@platform/lib/flavor";
import { Modal } from "@platform/ui/modal/Modal";

/** Whether the overlay is up: any in-flight stage, or a give-up / stuck ending
 *  the reader has not dismissed yet. Pure so the rule is one test, not a reading of the JSX. */
export function overlayVisible(stage: RestartStage, dismissed: boolean): boolean {
  if (restartInFlight(stage)) return true;
  return (stage === "gave-up" || stage === "stuck") && !dismissed;
}

export function RestartOverlayView(props: {
  stage: RestartStage;
  onDismiss: () => void;
  onRetry: () => void;
}) {
  const { stage, onDismiss, onRetry } = props;

  // Same trick as UpdateDialog: `busy` only stops the chassis closing THIS
  // modal; document-level Esc listeners elsewhere in the page would still fire
  // for a press, closing something the reader cannot see. Swallowed while the
  // wait is live; the give-up state wants Esc to dismiss, so it is left alone.
  const waiting = stage !== "gave-up" && stage !== "stuck";
  useEffect(() => {
    if (!waiting) return;
    const swallowEscape = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopImmediatePropagation();
    };
    document.addEventListener("keydown", swallowEscape, true);
    return () => document.removeEventListener("keydown", swallowEscape, true);
  }, [waiting]);

  if (stage === "stuck") {
    return (
      <Modal
        title={restartStuckTitle()}
        onClose={onDismiss}
        footer={
          <button type="button" className="btn btn-primary" onClick={onDismiss}>
            Dismiss
          </button>
        }
      >
        <p>{restartStuckBody()}</p>
      </Modal>
    );
  }

  if (stage === "gave-up") {
    return (
      <Modal
        title="Restart didn't finish"
        onClose={onDismiss}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={onDismiss}>
              Dismiss
            </button>
            <button type="button" className="btn btn-primary" onClick={onRetry}>
              Try again
            </button>
          </>
        }
      >
        <p>
          {displayName()} didn&rsquo;t come back after the update. Try again, or reopen the app
          yourself and this page will reconnect.
        </p>
      </Modal>
    );
  }

  return (
    <Modal title="Restarting to update…" busy onClose={() => {}}>
      <div className="restart-overlay-body" role="status" aria-live="polite">
        <span className="restart-spinner" aria-hidden="true" />
        <p>{restartStageLabel(stage)} Please wait — the app is unavailable until it is back.</p>
      </div>
    </Modal>
  );
}

export default function RestartOverlay(): React.ReactElement | null {
  const flow = useRestartFlow();
  // Dismissal is per press: keyed by `requestedAt`, so Try again (a new press,
  // a new timestamp) shows the overlay again instead of staying dismissed.
  const [dismissedAt, setDismissedAt] = useState<number | null>(null);
  const dismissed = flow.requestedAt !== null && dismissedAt === flow.requestedAt;
  if (!overlayVisible(flow.stage, dismissed)) return null;
  return (
    <RestartOverlayView
      stage={flow.stage}
      onDismiss={() => setDismissedAt(flow.requestedAt)}
      onRetry={() => requestRestart()}
    />
  );
}
