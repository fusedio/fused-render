// The app's own confirm, on the shared Modal chassis (Escape and the backdrop
// cancel; focus starts on Cancel so Enter never deletes by accident). Used for
// Delete in Settings › General and Settings › Skills instead of the browser's
// window.confirm.
import { useRef, useState } from "react";
import { Modal } from "@platform/ui/modal/Modal";

export function ConfirmDialog({
  title,
  text,
  okLabel = "Delete",
  onCancel,
  onConfirm,
}: {
  title: string;
  text: string;
  okLabel?: string;
  onCancel: () => void;
  /** Runs the action; the dialog shows it busy until it settles. */
  onConfirm: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  const cancel = useRef<HTMLButtonElement>(null);
  return (
    <Modal
      title={title}
      onClose={onCancel}
      busy={busy}
      width="min(440px, calc(100vw - 32px))"
      initialFocus={cancel}
      dialogClassName="spx-dialog spx-confirm"
      footer={
        <>
          <button ref={cancel} type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={busy}
            aria-busy={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await onConfirm();
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Deleting…" : okLabel}
          </button>
        </>
      }
    >
      <p>{text}</p>
    </Modal>
  );
}
