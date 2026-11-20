// The "New canvas" dialog — the catalog's one way to create a canvas (D1497).
//
// It replaced an inline form that swapped the head's "+ New canvas" button for
// an unlabeled name box beside the equally unlabeled search box: two identical
// fields side by side, and people typed the name into search. A dialog gives
// the name one labeled field, room for the naming rules, and room to say
// where the canvas goes — the hosted workbench's own CreateCanvasModal shape,
// which these users already know.
//
// The dialog owns the draft and its two error sources; the caller owns what
// "create" means (create → clone → start sync → navigate), so this file never
// learns the sync handshake. Name rules are canvas-name.ts, shared with
// RenameCanvasDialog.

import { useRef, useState } from "react";
import { Modal } from "@platform/ui/modal/Modal";
import {
  generateCanvasName,
  sanitizeCanvasName,
  validateCanvasName,
} from "./canvas-name";

export function NewCanvasDialog({
  envLabel,
  canvasesDir,
  existingNames,
  onCreate,
  onClose,
}: {
  /** The environment the canvas is created in, as the catalog names it. */
  envLabel: string;
  /** Where clones of this env land (`<dir>/<name>`), home-contracted; null
   *  when the status did not say, and the line naming it is left out. */
  canvasesDir: string | null;
  /** The LIVE listing's names, or null while it reloads. Judged on every
   *  render, so a canvas that appears while the dialog is open (a poll,
   *  another session) is caught before Create, not by the server. */
  existingNames: readonly string[] | null;
  /** Creates, clones and opens `name`. Resolves once the page is moving to
   *  the new canvas; a rejection's message is shown under the field and the
   *  dialog stays open so the same name can be retried. */
  onCreate: (name: string) => Promise<void>;
  onClose: () => void;
}) {
  // The workbench's suggestion, once per open: Enter keeps it, typing
  // replaces it (the field selects on focus).
  const [draft, setDraft] = useState(() => generateCanvasName(existingNames ?? []));
  const [busy, setBusy] = useState(false);
  // Kept apart from the rule error because only the rule error disables
  // Create: a failed create call leaves it on for a retry.
  const [submitError, setSubmitError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const name = draft.trim();
  const ruleError =
    existingNames === null ? "Loading canvases…" : validateCanvasName(name, existingNames);
  const shown = ruleError ?? submitError;
  const canSubmit = !busy && !ruleError;

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setSubmitError(null);
    try {
      await onCreate(name);
      // Stay busy on success: the caller is navigating away, and re-enabling
      // the buttons for the frame before unmount would invite a second press.
    } catch (e) {
      setSubmitError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <Modal
      title="New canvas"
      busy={busy}
      onClose={onClose}
      width={460}
      initialFocus={inputRef}
      footer={
        <>
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!canSubmit}
            onClick={() => void submit()}
          >
            {busy ? "Creating…" : "Create & open"}
          </button>
        </>
      }
    >
      <label className="canvas-new-label" htmlFor="canvas-new-name">
        Canvas name
      </label>
      <input
        ref={inputRef}
        id="canvas-new-name"
        type="text"
        className="field-control canvas-new-input"
        value={draft}
        disabled={busy}
        spellCheck={false}
        autoComplete="off"
        aria-invalid={shown ? "true" : undefined}
        aria-describedby={shown ? "canvas-new-error canvas-new-hint" : "canvas-new-hint"}
        onFocus={(e) => e.currentTarget.select()}
        onChange={(e) => {
          setDraft(sanitizeCanvasName(e.target.value));
          setSubmitError(null);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            void submit();
          }
        }}
      />
      {shown && (
        <p id="canvas-new-error" className="canvas-rename-error" role="alert">
          {shown}
        </p>
      )}
      <p id="canvas-new-hint" className="canvas-rename-note">
        Letters, numbers and underscores. Spaces become underscores.
      </p>
      <p className="canvas-new-where">
        Creates it in <b>{envLabel}</b>
        {canvasesDir ? (
          <>
            , clones it to <code>{`${canvasesDir}/${name || "…"}`}</code>
          </>
        ) : null}{" "}
        and opens it.
      </p>
    </Modal>
  );
}
