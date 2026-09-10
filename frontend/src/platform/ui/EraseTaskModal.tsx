// The one dialog on the Tasks page that DESTROYS something.
//
// Archive files a task away and keeps its conversation (D306); this deletes the
// Claude session behind the task — the transcript, the read/triage bookkeeping
// and any run still booked — and there is no way back. So no control anywhere on
// this page goes straight to the call: every trash press becomes a target the
// reader has to read back first, which is the whole reason this component exists
// rather than a `confirm()`.
//
// ONE MODAL, FOUR SURFACES (design.md §2): the List row's trash, the Cards
// wall's trash door, the Cards popup's head and the Claude chat's own kebab item
// all say the same words, because a reader who learns what "Delete forever"
// means in one place must not have to relearn it in the next.
//
// It LIVES IN PLATFORM for the fourth of those (Akshil, 2026-09-08): the chat is
// an app, an app may not import from `shell`, and the chat had grown a
// second dialog of its own with the same copy re-typed into it — which is how the
// two drift. `shell/EraseTaskModal.tsx` re-exports this for the three Tasks-page
// call sites. Both dependencies were already platform-only, so nothing else
// moved.
//
// Shaped on ai_models/local/DeleteDialogs — the app's other irreversible dialog:
// shared `Modal` chassis, the target named in the title, the consequence in the
// body, the path in mono, and a `btn-danger` whose word is the verb rather than
// "OK".
import { useState } from "react";
import { eraseTask } from "@platform/lib/api";
import type { Task } from "@platform/lib/api";
import { Modal } from "@platform/ui/modal/Modal";

export function EraseTaskModal({
  task,
  onClose,
  onDone,
  dialogClassName,
}: {
  /** What is about to go. Named in the title, because the id is the thing the
   *  reader recognises and the title is where they look for it.
   *
   *  Only these TWO fields are read, and the type says so: the chat's kebab
   *  knows a session key and a TASK number and has no `Task` record to hand
   *  over, and widening the prop is cheaper — and truer — than making it
   *  fabricate one (Akshil, 2026-09-08). */
  task: Pick<Task, "key" | "task_id">;
  onClose: () => void;
  /** An extra class for the dialog box, passed straight through to `Modal`.
   *
   *  OPTIONAL AND OFF BY DEFAULT, so every existing caller renders
   *  byte-identical markup. It exists for the native chat, which opens this
   *  dialog from inside its own token scope but portals it to `document.body`:
   *  without a hook the dialog painted the shell's danger ink where the chat
   *  (and the template it ports) paints its own, from tokens whose values are
   *  identical (`styles/chat.css`'s `.c-tokens`). */
  dialogClassName?: string;
  /** The server said yes. The caller re-reads its list and raises the toast —
   *  the row this dialog was opened from is about to leave the page, so the
   *  sentence about it cannot live on the row (Bugbot, 2026-08-18). */
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  // The server's own sentence, shown INSIDE the dialog rather than as a toast:
  // the 409 ("that task is running — stop the run first, then delete") is an
  // answer to the button still under the pointer, and the reader is going to
  // press Cancel next. Verbatim, so the words the row's hint promises and the
  // words the refusal gives are the same words.
  const [err, setErr] = useState("");

  const confirm = async () => {
    if (busy) return;
    setBusy(true);
    setErr("");
    try {
      await eraseTask(task.key);
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`Delete ${task.task_id}?`}
      busy={busy}
      onClose={onClose}
      {...(dialogClassName ? { dialogClassName } : {})}
      footer={
        <>
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn btn-danger" disabled={busy} onClick={confirm}>
            {busy ? "Deleting…" : "Delete forever"}
          </button>
        </>
      }
    >
      {/* Two sentences and nothing else (Akshil, 2026-09-07): what goes, and
          that it cannot come back. No path and no session id — a uuid is a
          fact the reader cannot check anything against. */}
      <p>This deletes the Claude session transcript behind this task.</p>
      <p>
        <b>This is permanent and cannot be undone.</b>
      </p>
      {/* The app's own error card (fields.css `.deploy-error`), not a class of
          this component's own: the same shape every other modal's refusal
          wears, and `role="alert"` because it arrives without a press. */}
      {err && (
        <p className="deploy-error" role="alert">
          {err}
        </p>
      )}
    </Modal>
  );
}
