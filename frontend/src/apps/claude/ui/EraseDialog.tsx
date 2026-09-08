// "Delete this task", confirmed (T:4423-4442 markup, T:13286-13372 behaviour).
//
// The kebab item opens this and NOTHING else; only "Delete forever" calls the
// endpoint. Archiving is one click because it is reversible — this is not, and
// the two items sit 30px apart in the same menu, so the second one has to ask.
//
// A dialog of our own rather than `confirm()`: a native confirm cannot say
// WHICH task in the app's own vocabulary (TASK-0042), cannot carry the 409 back
// onto the button the reader pressed, and in one pane of a split it reads as
// the browser talking, not the app. shadcn's AlertDialog is the chassis;
// shell/EraseTaskModal is the same dialog on the Tasks page and may not be
// imported from an app, so its copy and its numbers are ported here.
import { useCallback, useEffect, useState } from "react";
import {
  AlertDialog,
  AlertDialogContent,
} from "@platform/shadcn/ui/alert-dialog";
import { eraseTask } from "@platform/lib/api";

export interface EraseDialogProps {
  open: boolean;
  /** The session the dialog is ABOUT, captured at open time: the reader can
   *  walk away with the confirm still up, and a POST must never be aimed at
   *  whatever the page moved on to (T:13280-13285). */
  sessionKey: string;
  /** TASK-nnn when it is known — the name every other surface uses for this
   *  thing. Falls back to the generic wording rather than to the session hash:
   *  a uuid in a delete confirm is a fact the reader cannot check (T:13295). */
  taskId?: string;
  /** The control the dialog was opened FROM. Opened programmatically with no
   *  `AlertDialogTrigger`, Base UI has nothing to return focus to, so focus
   *  lands on `<body>` and the reader's next Tab starts from the top of the
   *  page — where T focuses `#kebabbtn` on cancel, ✕, the scrim and Esc alike
   *  (T:13293, T:13319-13321). */
  returnFocusTo?: React.RefObject<HTMLElement | null>;
  /** Refused while a request is in flight (T:13312). */
  onClose(): void;
  /** The session is GONE: drop every cache keyed by it and leave the
   *  transcript — "Back to chats" IS the way out (T:13352-13366). */
  onErased(key: string): void;
}

export function EraseDialog({
  open,
  sessionKey,
  taskId,
  returnFocusTo,
  onClose,
  onErased,
}: EraseDialogProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (open) {
      setBusy(false);
      setError("");
    }
  }, [open]);

  const close = useCallback(() => {
    // Never closes from under a request in flight: the reply is about to write
    // either a navigation or an error into this box.
    if (busy) return;
    onClose();
  }, [busy, onClose]);

  const go = useCallback(async () => {
    if (!sessionKey || busy) return;
    setBusy(true);
    setError("");
    try {
      await eraseTask(sessionKey);
      setBusy(false);
      onErased(sessionKey);
    } catch (err) {
      // The 409 is a SENTENCE the server wrote ("that task is running — stop
      // the run first, then delete") and it is the one thing the reader can act
      // on, so it is shown verbatim rather than translated (T:13339).
      setBusy(false);
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [sessionKey, busy, onErased]);

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => (next ? undefined : close())}
    >
      <AlertDialogContent
        aria-label={taskId ? `Delete ${taskId}?` : "Delete this task?"}
        // EVERY close path, which is the point: cancel, ✕, the scrim and Esc all
        // end here (T:13293, T:13319-13321). Opened programmatically with no
        // `AlertDialogTrigger`, Base UI has nothing of its own to return focus
        // to and leaves it on `<body>`.
        {...(returnFocusTo ? { finalFocus: returnFocusTo } : {})}
        className="c-overlay c-erasedlg-box flex w-[min(600px,calc(100vw-48px))] max-w-none flex-col gap-0 overflow-hidden rounded-[10px] border border-[var(--c-border)] bg-[var(--c-panel)] p-0 text-[var(--c-fg)] ring-0"
      >
        <div className="c-erasedlg-head">
          <h3 className="c-erasedlg-title">
            {taskId ? `Delete ${taskId}?` : "Delete this task?"}
          </h3>
          <button
            type="button"
            className="c-erasedlg-x"
            aria-label="Close"
            title="Close"
            onClick={close}
          >
            ✕
          </button>
        </div>
        <div className="c-erasedlg-body">
          <p>This deletes the Claude session transcript behind this task.</p>
          <p>
            <strong>This is permanent and cannot be undone.</strong>
          </p>
          {error ? (
            <p className="c-erasedlg-err" role="alert">
              {error}
            </p>
          ) : null}
        </div>
        <div className="c-erasedlg-bar">
          {/* CANCEL takes the focus, not the destructive button: Enter on a
                freshly opened confirm must not be the answer "yes"
                (T:13306-13308). */}
          <button
            type="button"
            className="c-erasedlg-btn"
            disabled={busy}
            autoFocus
            onClick={close}
          >
            Cancel
          </button>
          <button
            type="button"
            className="c-erasedlg-btn c-erasedlg-danger"
            disabled={busy}
            onClick={() => void go()}
          >
            {busy ? "Deleting…" : "Delete forever"}
          </button>
        </div>
      </AlertDialogContent>
    </AlertDialog>
  );
}
