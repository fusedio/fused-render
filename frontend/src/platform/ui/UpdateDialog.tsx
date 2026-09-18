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
// button; after it, the button is REPLACED by the stage line — the press has no
// second meaning, and a button left on screen under a progress word invites one.
// The stages and their words are `restart-flow.ts`.
import { useEffect } from "react";

import { Modal } from "@platform/ui/modal/Modal";
import { restartStageLabel, type RestartStage } from "@platform/lib/restart-flow";

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
      onRestart: () => void;
    };

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

  const label = restartStageLabel(props.stage);
  return (
    <Modal
      title={`fused-render v${props.installedVersion} is ready`}
      busy
      onClose={() => {}}
      footer={
        // NOT WHILE THE ANSWER IS STILL COMING. `verifying` means this window
        // read a restart record and has asked the server whether that restart is
        // still running; until it answers, the stage reads `ready` and a press
        // would start a SECOND restart on top of the one being verified. The
        // button keeps its box and its label and simply cannot be pressed —
        // showing nothing there would move the footer under the reader's cursor
        // for the fraction of a second the request takes.
        //
        // THE BUTTON IS DRAWN FOR `gave-up` AS WELL AS `ready`. Both are
        // stages with nothing in flight to narrate, and `gave-up` is the one
        // the reader most needs a control on: the restart did not take, the
        // dialog is still blocking the page, and `reduceRestart` re-arms from
        // any stage — so offering the press again is both possible and the only
        // way out that is not a page reload. Drawing the (empty) stage line
        // there instead left a blocking dialog with no control at all.
        props.stage === "ready" || props.stage === "gave-up" ? (
          <button
            type="button"
            className="btn btn-primary"
            onClick={props.onRestart}
            disabled={props.verifying === true}
          >
            Restart fused-render
          </button>
        ) : (
          // The live region is the LINE, not the dialog: only this text
          // changes, and announcing the whole dialog on every stage would read
          // the title and the sentence out three times over one restart.
          <div className="update-dialog-stage" role="status" aria-live="polite">
            {label}
          </div>
        )
      }
    >
      <p>
        The app is still running v{props.version}. Restart to finish the update.
      </p>
    </Modal>
  );
}

export default UpdateDialog;
