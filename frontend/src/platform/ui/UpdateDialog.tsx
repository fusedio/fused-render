// ONE DIALOG FOR BOTH UPDATE CASES (D1, Akshil 2026-09-18). The app has two
// ways to be out of date and they want the same chrome, the same wording and
// the same blocking behaviour — so they are two MODES of one component, not two
// components that drift apart:
//
//   "refresh" — the server serves a newer version than this bundle was built
//               from. A page refresh picks up the new shell. (This was the
//               only dialog; its behaviour here is unchanged.)
//   "restart" — the version installed on disk is newer than the running app.
//               A refresh would change nothing; only the app restarting helps.
//               It used to be a card in the notification stack that sat there
//               forever with a bare link on it, which said nothing about
//               whether the press had worked.
//
// BOTH BLOCK THE PAGE, for the same reason: the page behind the scrim is
// talking to a version that is going away, and every click from here is a guess
// about which side answers. `busy` is the chassis' lever for that — it drops the
// ✕ entirely and makes `decideClose` answer "block" for Esc and for a backdrop
// press — and `onClose` is therefore a no-op, because with `busy` set nothing in
// the chassis can reach it.
//
// ON THE SHARED CHASSIS (Akshil, 2026-09-14: "check the delete task modal, reuse
// that same component"), used exactly the way `EraseTaskModal` uses it: title, a
// sentence in the body, the one control in the footer.
//
// THE RESTART MODE'S FOOTER IS THE WHOLE INTERACTION. Before the press it is a
// button; after it, the button is REPLACED by the THREE-STEP STRIP — the press
// has no second meaning, and a button left on screen under a progress word
// invites one. The stages, their words and the strip's shape are
// `restart-flow.ts`; nothing about which step is lit is decided here.
//
// WHY A STRIP AND NOT A WORD (the follow-up to D1). One word swapped for
// another said what the app was doing and nothing about how far along it was:
// "Restarting…" reads the same in the first second and the fiftieth, and the
// only question a reader of a dialog they cannot close actually has is whether
// it is getting anywhere. Three named steps answer that with no percentage —
// which is the honest choice, because nothing in this flow knows one. Not a
// progress bar for the same reason.
//
// THE FOOTER IS THE SAME HEIGHT IN EVERY STAGE. The strip stands in the box the
// button stood in (32px, notifications.css), and `gave-up` — the one stage that
// shows both — puts them side by side in the footer's own flex row rather than
// stacking them, so the dialog never resizes under a reader mid-restart.
import { Fragment, useEffect, useState } from "react";

import { Modal } from "@platform/ui/modal/Modal";
import {
  restartInFlight,
  restartIsSlow,
  restartStageLabel,
  restartSteps,
  type RestartStage,
  type RestartStep,
} from "@platform/lib/restart-flow";

export type UpdateDialogProps =
  | {
      kind: "refresh";
      /** The version the server now serves. */
      version: string;
      /** The version this bundle was built from. */
      buildVersion: string;
    }
  | {
      kind: "restart";
      /** The version still running. */
      version: string;
      /** The version sitting on disk, waiting for the restart. */
      installedVersion: string;
      stage: RestartStage;
      /** This window found a restart RECORD and is still asking the server
       *  whether that restart is actually in flight. The stage says `ready`
       *  meanwhile, and it may be about to say otherwise. */
      verifying?: boolean;
      /** Epoch ms of the press — the store's own, shared verbatim across
       *  windows, so the "taking longer" sentence flips at the same instant in
       *  all of them rather than at each window's own 25s. */
      requestedAt?: number | null;
      /** Freeze the clock. Tests and the dev preview only; the real path leaves
       *  it out and the dialog ticks on its own. */
      now?: number;
      onRestart: () => void;
    };

/**
 * The one-second clock the "taking longer" sentence runs on.
 *
 * NOT THE STORE'S TICK, although the store has one: `dispatch` drops a tick
 * that changed nothing (by value — otherwise `useSyncExternalStore` re-renders
 * every subscriber on every poll), and a wait crossing 25s changes neither the
 * stage nor the press instant. So the sentence would never arrive. This clock
 * exists for exactly the thing the store deliberately does not publish.
 *
 * It runs only while there is a wait to time — a dialog with nothing in flight
 * must not hold an interval open for the life of the page — and stops dead when
 * a `now` is injected, so a test's frozen instant cannot be overwritten a second
 * later by the real one.
 */
function useRestartClock(active: boolean, nowOverride?: number): number {
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    if (!active || nowOverride !== undefined) return;
    // Read once on the way in as well: the wait may have started before this
    // dialog mounted (another window's press, a record adopted on wake), and
    // waiting a second to find that out would paint the wrong sentence first.
    setClock(Date.now());
    const timer = setInterval(() => setClock(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [active, nowOverride]);
  return nowOverride ?? clock;
}

/** The mark before each step's word. Three glyphs, one box: they must occupy
 *  the same width or the words shuffle sideways every time a step completes.
 *  The live one is the app's OWN update spinner (`.update-spinner`, the sidebar
 *  badge's) rather than a new one — it is the same wait, told twice. */
function RestartStepMark({ state }: { state: RestartStep["state"] }) {
  if (state === "live") return <span className="update-spinner" aria-hidden="true" />;
  if (state === "done")
    return (
      <span className="update-dialog-step-mark update-dialog-step-tick" aria-hidden="true">
        ✓
      </span>
    );
  return <span className="update-dialog-step-mark update-dialog-step-dot" aria-hidden="true" />;
}

/**
 * The strip. Derived from the stage every render (`restartSteps`), never
 * accumulated: `reduceRestart` sends `reconnecting` BACK to `quitting` when the
 * old process answers on the same version, and a strip that remembered its
 * furthest point would have to animate backwards to show it. This one simply
 * un-ticks.
 *
 * ONE LIVE REGION FOR THE WHOLE STRIP, not one per step: the words change
 * together and three regions would announce three times for one transition. The
 * marks are `aria-hidden` — a tick and a hollow circle are the same fact the
 * word's tense already carries, and read aloud they are noise.
 */
function RestartSteps({ stage }: { stage: RestartStage }) {
  const steps = restartSteps(stage);
  return (
    <div
      className={
        "update-dialog-steps" +
        // The restart did not take. Every step goes quiet — no tick claiming a
        // stage that did not finish, and above all no spinner, which held here
        // would be the forever-promise the cap exists to stop making.
        (stage === "gave-up" ? " is-stalled" : "") +
        // The moment before the reload. See the tint in notifications.css.
        (stage === "back" ? " is-back" : "")
      }
      // NOT a live region. The body sentence below the title is (see
      // UpdateDialog): one region per dialog, one announcement per transition —
      // a second one here would read every stage change twice.
    >
      {steps.map((step, i) => (
        <Fragment key={step.stage}>
          {i > 0 && <span className="update-dialog-steps-link" aria-hidden="true" />}
          <span className={"update-dialog-step is-" + step.state}>
            <RestartStepMark state={step.state} />
            {/* THE WORD IS SIZED FOR ITS LONGEST FORM whichever form shows: a
                hidden ghost of the live label ("Restarting…") sits under the
                visible word ("Restart") in the same grid cell, so the strip is
                the same width in every stage and no mark slides when a step
                goes live or completes. */}
            <span className="update-dialog-step-word">
              <span className="update-dialog-step-ghost" aria-hidden="true">
                {restartStageLabel(step.stage)}
              </span>
              <span className="update-dialog-step-text">{step.label}</span>
            </span>
          </span>
        </Fragment>
      ))}
    </div>
  );
}

/**
 * The sentence under the title. Stage-aware, and PROSE — the one-word rule
 * (Akshil, 2026-09-08) is about the step names in the strip, not about this: a
 * body that may not name a version could not say which version is closing and
 * which is starting, which is the whole reason the dialog is up.
 */
function restartBody(
  props: Extract<UpdateDialogProps, { kind: "restart" }>,
  slow: boolean,
): string {
  if (props.stage === "ready") {
    return `The app is still running v${props.version}. Restart to finish the update.`;
  }
  if (props.stage === "gave-up") {
    // The way out that does not depend on this page: open the app yourself
    // (Akshil, 2026-09-19: "instead of give up message show this: try opening
    // the app again"). The button beside it is the other one, and if the app
    // is truly gone it cannot work.
    return "The app didn't come back. Try opening fused-render again.";
  }
  if (props.stage === "back") {
    return `Back on v${props.installedVersion} — reloading…`;
  }
  // THE ESTIMATE IS WITHDRAWN RATHER THAN REPEATED. "about 15 seconds" that has
  // visibly run out is worse than no number at all, so past `RESTART_SLOW_MS`
  // the same slot says so plainly instead. It is shorter than the sentence it
  // replaces; `.update-dialog-body` holds the two lines so nothing moves under
  // a reader who is already waiting.
  if (slow) return "Taking a little longer than usual — still working on it.";
  return (
    `Closing v${props.version} and starting v${props.installedVersion}. ` +
    "This page comes back on its own — usually in about 15 seconds."
  );
}

/** What assistive tech hears: the live step's label ahead of the sentence, so
 *  a stage change is audible even when the sentence did not change. Stages with
 *  no live step (`ready`, `back`, `gave-up`) are the sentence alone — their
 *  sentences differ, so they announce on their own. */
export function restartAnnouncement(stage: RestartStage, body: string): string {
  const live = restartSteps(stage).find((s) => s.state === "live");
  return live ? `${live.label} ${body}` : body;
}

export function UpdateDialog(props: UpdateDialogProps) {
  // ESCAPE IS SWALLOWED FOR THE WHOLE PAGE, which `busy` alone does not do:
  // `busy` only stops the chassis from closing THIS dialog. The page behind the
  // scrim is still mounted and still listening — another modal's Esc stack, the
  // sidebar, a peek — so a press here would close something the reader cannot
  // see instead of doing nothing at all. Capture phase on `document` with
  // `stopImmediatePropagation`, because the listeners being headed off are
  // document-level ones React's synthetic propagation never reaches; the dialog
  // blocks every other input by covering the page, and this is the one key that
  // gets past a scrim.
  useEffect(() => {
    const swallowEscape = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopImmediatePropagation();
    };
    document.addEventListener("keydown", swallowEscape, true);
    return () => document.removeEventListener("keydown", swallowEscape, true);
  }, []);

  // Above the refresh branch because hooks cannot live below one. `back` is in
  // flight for the banner's purposes (the reload is a beat away) but is not a
  // WAIT — there is nothing left to take too long — so it does not run a clock.
  const stage: RestartStage = props.kind === "restart" ? props.stage : "ready";
  const waiting = restartInFlight(stage) && stage !== "back";
  const now = useRestartClock(waiting, props.kind === "restart" ? props.now : undefined);

  if (props.kind === "refresh") {
    return (
      <Modal
        title={`fused-render updated to v${props.version}`}
        busy
        onClose={() => {}}
        footer={
          <button type="button" className="btn btn-primary" onClick={() => window.location.reload()}>
            Refresh page
          </button>
        }
      >
        <p>This page is still on v{props.buildVersion}. Refresh to load the new version.</p>
      </Modal>
    );
  }

  const slow = waiting && restartIsSlow(props.requestedAt ?? null, now);
  // THE BUTTON IS DRAWN FOR `gave-up` AS WELL AS `ready`. Both are stages with
  // nothing in flight to narrate, and `gave-up` is the one the reader most needs
  // a control on: the restart did not take, the dialog is still blocking the
  // page, and `reduceRestart` re-arms from any stage — so offering the press
  // again is both possible and the only way out that is not a page reload.
  const button = props.stage === "ready" || props.stage === "gave-up";
  // …and the strip is drawn for every stage but `ready`, `gave-up` INCLUDED:
  // ending on a bare button would erase the wait that just failed, and the
  // greyed strip is what says the app tried and did not come back.
  const strip = props.stage !== "ready";
  return (
    <Modal
      title={`fused-render v${props.installedVersion} is ready`}
      busy
      onClose={() => {}}
      footer={
        <>
          {strip && <RestartSteps stage={props.stage} />}
          {button && (
            <button
              type="button"
              className="btn btn-primary"
              onClick={props.onRestart}
              // NOT WHILE THE ANSWER IS STILL COMING. `verifying` means this
              // window read a restart record and has asked the server whether
              // that restart is still running; until it answers, the stage reads
              // `ready` and a press would start a SECOND restart on top of the
              // one being verified. The button keeps its box and its label and
              // simply cannot be pressed — showing nothing there would move the
              // footer under the reader's cursor for the fraction of a second
              // the request takes.
              disabled={props.verifying === true}
            >
              Restart fused-render
            </button>
          )}
        </>
      }
    >
      {/* `.update-dialog-body` reserves two lines so the shorter sentences (the
          25s one, `back`) do not let the footer jump up under a reader who
          cannot dismiss this. Not a live region: see the announcer below. */}
      <p className="update-dialog-body">{restartBody(props, slow)}</p>
      {/* THE ONE LIVE REGION of the dialog, and it is neither the strip nor the
          body — each alone is silent for half the changes. The body reads the
          same sentence for all three in-flight stages, so on it the strip
          advancing is never heard; the strip does not change at the 25s flip
          or say the versions, so on it the story is never heard. This hidden
          line says BOTH — the live step, then the sentence — so every change a
          sighted reader can see is one announcement, and only one. */}
      <span className="sr-only" role="status" aria-live="polite">
        {restartAnnouncement(props.stage, restartBody(props, slow))}
      </span>
    </Modal>
  );
}

export default UpdateDialog;
