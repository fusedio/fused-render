// The one "Rename canvas" dialog. Two doors open it: the sidebar's canvas row
// menu (shell/CurrentAppsSection) and the embedded workbench's canvas title
// (`fused-embed-canvas-rename`, CanvasWorkspace) — the frame only ASKS; the
// host is the canvas's one writer and renames (POST /api/canvases/rename).
//
// After a rename the page on the old canvas's editor follows to the new
// route (`replace`: Back must not land on a name that no longer exists), and
// the desk is told it changed so the sidebar row refetches under its new
// path. Name rules are the create form's (canvas-name.ts).

import { useState } from "react";
import { Modal } from "@platform/ui/modal/Modal";
import { confirmLeave, navigateUrl } from "@platform/lib/router";
import { announceCurrentAppsChanged } from "@platform/lib/tasksChanged";
import { canvasWorkspacePath, renameCanvas, type WorkbenchEnv } from "./api";
import { sanitizeCanvasName, validateCanvasName } from "./canvas-name";

export function RenameCanvasDialog({
  env,
  name,
  onClose,
  onRenamed,
}: {
  env: WorkbenchEnv;
  name: string;
  onClose: () => void;
  /** After a successful rename, before the dialog closes. */
  onRenamed?: (newName: string) => void;
}) {
  const [draft, setDraft] = useState(name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const trimmed = draft.trim();
  const ruleError = trimmed === name ? null : validateCanvasName(trimmed, []);
  const canSubmit = !busy && !!trimmed && trimmed !== name && !ruleError;

  const submit = async () => {
    if (!canSubmit) return;
    // On this canvas's editor the rename ends in a hop to the new route, so
    // ask the leave guard (an unsent composer) BEFORE committing: a refusal
    // after the server and clone moved would strand the page on a dead name.
    // A guard detaches once answered, so the hop below is not asked again.
    const onEditor = window.location.pathname === canvasWorkspacePath(env, name);
    if (onEditor && !(await confirmLeave())) return;
    setBusy(true);
    setError(null);
    try {
      const r = await renameCanvas(env, name, trimmed);
      announceCurrentAppsChanged();
      onRenamed?.(r.name);
      // Follow it, keeping the query (node, chat).
      if (onEditor) {
        await navigateUrl(canvasWorkspacePath(env, r.name) + window.location.search, {
          replace: true,
        });
      }
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const shown = error ?? ruleError;
  return (
    <Modal
      title={"Rename " + name}
      busy={busy}
      onClose={onClose}
      width={460}
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
            {busy ? "Renaming…" : "Rename"}
          </button>
        </>
      }
    >
      <p>
        Renames the canvas in Fused and its folder on this computer. Its tasks and Claude
        sessions move with it.
      </p>
      <input
        type="text"
        className="field-control"
        value={draft}
        autoFocus
        disabled={busy}
        spellCheck={false}
        aria-label="New canvas name"
        aria-invalid={shown ? "true" : undefined}
        aria-describedby={shown ? "canvas-rename-error" : undefined}
        onFocus={(e) => e.currentTarget.select()}
        onChange={(e) => {
          setDraft(sanitizeCanvasName(e.target.value));
          setError(null);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            void submit();
          }
        }}
      />
      {shown && (
        <p id="canvas-rename-error" className="canvas-rename-error" role="alert">
          {shown}
        </p>
      )}
      <p className="canvas-rename-note">
        Links and code that call this canvas by its name stop working. Other computers that
        have it cloned need to clone it again.
      </p>
    </Modal>
  );
}
