// ONE DIALOG, ONE MODE NOW (D1, Akshil 2026-09-18; SPEC-update-notifications.md).
// The app used to have two ways to be out of date sharing one chassis:
//
//   "refresh" — the server serves a newer version than this bundle was built
//               from. A page refresh picks up the new shell. (This was the
//               only dialog originally; its behaviour here is unchanged.)
//   "restart" — the version installed on disk is newer than the running app.
//               DELETED (SPEC-update-notifications.md): the decision moment
//               for a restart is now a status-bar notification
//               (`platform/ui/UpdateNotifier.tsx`), not a blocking dialog.
//               Everything that only the restart mode needed — the stage
//               prop, the three-step strip, the "taking longer" clock, the
//               live-region announcer — went with it. See git history on
//               this file (pre-SPEC-update-notifications) for that mode if
//               it is ever needed again.
//
// THE ONE REMAINING MODE STILL BLOCKS THE PAGE, for the same reason it always
// did: the page behind the scrim is talking to a version that is going away,
// and every click from here is a guess about which side answers. `busy` is
// the chassis' lever for that — it drops the ✕ entirely and makes
// `decideClose` answer "block" for Esc and for a backdrop press — and
// `onClose` is therefore a no-op, because with `busy` set nothing in the
// chassis can reach it.
//
// ON THE SHARED CHASSIS (Akshil, 2026-09-14: "check the delete task modal,
// reuse that same component"), used exactly the way `EraseTaskModal` uses it:
// title, a sentence in the body, the one control in the footer.
import { useEffect } from "react";

import { Modal } from "@platform/ui/modal/Modal";

export type UpdateDialogProps = {
  kind: "refresh";
  /** The version the server now serves. */
  version: string;
  /** The version this bundle was built from. */
  buildVersion: string;
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

export default UpdateDialog;
