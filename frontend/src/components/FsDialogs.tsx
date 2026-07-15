// Small modal dialogs for the explorer's file operations, reusing the Deploy
// modal's overlay/dialog chrome (.deploy-* in shell.css) — same pattern as
// views/Mounts.tsx's Modal. Two shapes:
//   • PromptDialog — a single prefilled text input for New File / New Folder /
//     Rename. Enter confirms, Escape (or a backdrop click) cancels. The name is
//     validated inline: non-empty and no "/" (a rename can't move across dirs).
//   • ConfirmDialog — a message + Cancel/confirm, for Delete (recursive for a
//     non-empty directory is spelled out in the message the caller passes).
import { useEffect, useRef, useState, type ReactNode } from "react";

function Overlay({ onCancel, children }: { onCancel: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
      }
    };
    // Capture so this beats the listing's document-level key handlers.
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onCancel]);

  return (
    <div
      className="deploy-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div
        className="deploy-dialog fs-dialog"
        role="dialog"
        aria-modal="true"
        onMouseDown={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

export function PromptDialog({
  title,
  initialValue,
  confirmLabel = "OK",
  // Whether to preselect only the name (sans extension) on focus, Finder-style,
  // so a Rename edits the stem without wiping the extension.
  selectStem = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  initialValue: string;
  confirmLabel?: string;
  selectStem?: boolean;
  onConfirm: (value: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initialValue);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Focus on open, preselecting the stem (name without extension) for a rename
  // and the whole value otherwise. Reads `initialValue`, never the live `value`,
  // so it doesn't reselect on every keystroke.
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.focus();
    const dot = initialValue.lastIndexOf(".");
    if (selectStem && dot > 0) el.setSelectionRange(0, dot);
    else el.select();
  }, [initialValue, selectStem]);

  const trimmed = value.trim();
  const error = trimmed === "" ? "Name can't be empty" : trimmed.includes("/") ? 'Name can\'t contain "/"' : null;

  const submit = () => {
    if (error) return;
    onConfirm(trimmed);
  };

  return (
    <Overlay onCancel={onCancel}>
      <div className="deploy-head">
        <h2>{title}</h2>
        <button type="button" className="deploy-close" onClick={onCancel} aria-label="Close">
          ✕
        </button>
      </div>
      <div className="deploy-body">
        <input
          ref={inputRef}
          className="fs-dialog-input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              // Stop the confirming Enter from reaching the listing's
              // document-level nav handler, which would otherwise treat it as
              // "open the selected row" the instant the dialog closes.
              e.stopPropagation();
              submit();
            }
          }}
        />
        {error && trimmed !== "" && <div className="deploy-error">{error}</div>}
        <div className="fs-dialog-actions">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="deploy-primary" disabled={!!error} onClick={submit}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </Overlay>
  );
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel = "OK",
  danger = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Overlay onCancel={onCancel}>
      <div className="deploy-head">
        <h2>{title}</h2>
        <button type="button" className="deploy-close" onClick={onCancel} aria-label="Close">
          ✕
        </button>
      </div>
      <div className="deploy-body">
        <p>{message}</p>
        <div className="fs-dialog-actions">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className={danger ? "deploy-danger" : "deploy-primary"}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </Overlay>
  );
}
