// "You are about to leave a message you never sent."
//
// THE COMPOSER DOES NOT AUTOSAVE ANY MORE (Akshil, 2026-09-16: "when I am in the
// composer, don't autosave as a draft — I'm already there. If I do an operation
// that could lose the text, pop up a warning: save as draft or discard"). So the
// moment the box is about to go off screen is the moment the words become a
// record or stop existing, and that is a question with exactly one person who
// can answer it.
//
// THREE ANSWERS AND NO FOURTH. "Save as draft" writes the one chat record and
// carries on; "Discard" carries on with nothing written; "Cancel" stays. Esc,
// the backdrop and the ✕ are all Cancel — the safe answer is the one a dismissal
// falls to, because a reader who pressed Escape has not decided anything.
//
// IT LIVES IN PLATFORM for `EraseTaskModal`'s reason: the asker is the chat,
// which is an app, and an app may not import from `shell`. Its dependencies are
// the shared modal chassis and the app's own button vocabulary, both of which
// are already here.
import { useState } from "react";
import { Modal } from "@platform/ui/modal/Modal";

/** The title, the body and the three words on the buttons, in ONE place: this
 *  dialog is asked for from more than one call site and a re-word in one of them
 *  would be two dialogs asking the same question differently. */
export const UNSENT_TITLE = "Unsent message";
export const UNSENT_BODY = "Save it as a draft in Upcoming, or discard it?";

export function UnsentMessageModal({
  onSave,
  onDiscard,
  onCancel,
}: {
  /** Write the record, then let the navigation happen. Awaited, because the hop
   *  it is holding up must not land before the words are on the server — the
   *  card on the other side seeds from `GET /api/drafts`. A refusal is the
   *  caller's to report; this dialog only stays up while the write is out. */
  onSave(): Promise<void>;
  /** Leave, write nothing. */
  onDiscard(): void;
  /** Stay, with the text exactly as it was. */
  onCancel(): void;
}) {
  // ONE PRESS. The write is a round trip, and a second "Save as draft" in that
  // window is a second PUT for one answer.
  const [busy, setBusy] = useState(false);
  const save = () => {
    if (busy) return;
    setBusy(true);
    void onSave().finally(() => setBusy(false));
  };

  return (
    <Modal
      title={UNSENT_TITLE}
      busy={busy}
      onClose={onCancel}
      width={420}
      footer={
        <>
          <button
            type="button"
            className="btn btn-secondary"
            disabled={busy}
            onClick={onCancel}
          >
            Cancel
          </button>
          {/* DESTRUCTIVE, AND NOT THE EMPHASIZED ONE. `btn-danger-text` is the
              app's text-weight danger: it says what the press costs without
              competing with the answer that keeps the words. */}
          <button
            type="button"
            className="btn btn-danger-text"
            disabled={busy}
            onClick={onDiscard}
          >
            Discard
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy}
            onClick={save}
          >
            {busy ? "Saving…" : "Save as draft"}
          </button>
        </>
      }
    >
      <p>{UNSENT_BODY}</p>
    </Modal>
  );
}
