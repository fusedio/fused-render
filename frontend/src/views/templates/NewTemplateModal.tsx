import { useEffect, useRef, useState } from "react";
import { createTemplate, openTemplateInClaude } from "../../lib/api";
import type { NewTemplateResult } from "../../lib/api";

// Scaffold a new user template. Name is required (nonempty, no "/"); extensions
// are optional dot-keys bound to the new template as their default — zero is
// fine (bindings can be added later from the File bindings tab). On success the
// panel refreshes and this offers "Open in Claude" to start editing the folder.
export function NewTemplateModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [extensions, setExtensions] = useState<string[]>([]);
  const [extDraft, setExtDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<NewTemplateResult | null>(null);
  // Open-in-Claude runs from the success screen; keep its own busy/error so a
  // failure there doesn't read as the create having failed.
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState<string | null>(null);

  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, busy]);

  // Normalize "csv" / " .CSV " → ".csv"; dedupe against existing chips.
  const addExtension = () => {
    const raw = extDraft.trim().toLowerCase();
    if (!raw) return;
    const ext = raw.startsWith(".") ? raw : "." + raw;
    setExtensions((prev) => (prev.includes(ext) ? prev : [...prev, ext]));
    setExtDraft("");
  };

  const removeExtension = (ext: string) =>
    setExtensions((prev) => prev.filter((e) => e !== ext));

  const onExtKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addExtension();
    } else if (e.key === "Backspace" && extDraft === "" && extensions.length > 0) {
      // Backspace on an empty draft peels off the last chip (standard tag-input).
      removeExtension(extensions[extensions.length - 1]);
    }
  };

  const trimmedName = name.trim();
  const nameError = trimmedName.includes("/") ? 'Name cannot contain "/".' : null;
  const canCreate = trimmedName.length > 0 && !nameError && !busy;

  const create = async () => {
    if (!canCreate) return;
    setBusy(true);
    setError(null);
    try {
      // Fold any half-typed extension still in the draft into the payload so a
      // user who typed one but didn't press Enter doesn't silently lose it.
      const pending = extDraft.trim().toLowerCase();
      const extra = pending ? [pending.startsWith(".") ? pending : "." + pending] : [];
      const exts = Array.from(new Set([...extensions, ...extra]));
      const res = await createTemplate(trimmedName, exts);
      // The template landed server-side — refresh the parent regardless of
      // whether this modal is still mounted (matches ImportWizard's posture).
      onCreated();
      if (!alive.current) return;
      setResult(res);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const openInClaude = async (templateName: string) => {
    setOpening(true);
    setOpenError(null);
    try {
      await openTemplateInClaude(templateName);
      if (alive.current) onClose();
    } catch (e) {
      if (alive.current) setOpenError((e as Error).message);
    } finally {
      if (alive.current) setOpening(false);
    }
  };

  return (
    <div
      className="deploy-overlay"
      onMouseDown={(e) => {
        if (!busy && e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="deploy-dialog templates-new"
        role="dialog"
        aria-modal="true"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="deploy-head">
          <h2>New template</h2>
          <button type="button" className="deploy-close" onClick={onClose} disabled={busy}>
            ✕
          </button>
        </div>
        <div className="deploy-body">
          {result ? (
            <>
              <div className="templates-result">
                <div className="templates-result-line">
                  Created <code>{result.name}</code>.
                </div>
                {result.bindings.length > 0 && (
                  <div className="templates-result-line">
                    <span className="deploy-muted">Bound as default for:</span>{" "}
                    {result.bindings.map((b) => (
                      <code key={b} className="templates-usedby-chip">
                        {b}
                      </code>
                    ))}
                  </div>
                )}
              </div>
              <p className="deploy-muted">
                Edit the template's files from the file explorer, or open Claude Code in its folder
                to build it out.
              </p>
              {openError && <div className="deploy-error">{openError}</div>}
              <div className="templates-actions">
                <button type="button" className="templates-btn-secondary" onClick={onClose}>
                  Done
                </button>
                <button
                  type="button"
                  className="templates-btn-primary"
                  onClick={() => openInClaude(result.name)}
                  disabled={opening}
                >
                  {opening ? "Opening…" : "Open in Claude"}
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="deploy-muted">
                Scaffold a new user template. Bind it to file extensions now, or leave that empty
                and add bindings later from the File bindings tab.
              </p>
              <div className="templates-field">
                <label htmlFor="new-template-name">Name</label>
                <input
                  id="new-template-name"
                  type="text"
                  className="templates-search"
                  placeholder="my-template"
                  value={name}
                  autoFocus
                  disabled={busy}
                  onChange={(e) => setName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && canCreate) create();
                  }}
                />
                {nameError && <div className="templates-key-error">{nameError}</div>}
              </div>
              <div className="templates-field">
                <label htmlFor="new-template-ext">Extensions</label>
                <div className="templates-chip-input">
                  {extensions.map((ext) => (
                    <span key={ext} className="templates-chip small">
                      {ext}
                      <button
                        type="button"
                        className="templates-chip-x"
                        onClick={() => removeExtension(ext)}
                        disabled={busy}
                        aria-label={"Remove " + ext}
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                  <input
                    id="new-template-ext"
                    type="text"
                    className="templates-chip-draft"
                    placeholder={extensions.length === 0 ? ".csv" : ""}
                    value={extDraft}
                    disabled={busy}
                    onChange={(e) => setExtDraft(e.target.value)}
                    onKeyDown={onExtKey}
                    onBlur={addExtension}
                  />
                </div>
                <span className="deploy-muted">
                  Type an extension and press Enter. The leading dot is added for you.
                </span>
              </div>
              {error && <div className="deploy-error">{error}</div>}
              <div className="templates-actions">
                <button
                  type="button"
                  className="templates-btn-secondary"
                  onClick={onClose}
                  disabled={busy}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="templates-btn-primary"
                  onClick={create}
                  disabled={!canCreate}
                >
                  {busy ? "Creating…" : "Create"}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
