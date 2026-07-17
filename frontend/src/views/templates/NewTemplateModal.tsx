import { useEffect, useRef, useState } from "react";
import { createTemplate, openTemplateInClaude } from "../../lib/api";
import type { NewTemplateResult } from "../../lib/api";
import { Modal } from "../../components/modal/Modal";
import { ErrorBanner } from "../../components/ErrorBanner";

// Scaffold a new user template. Name is required (nonempty, no "/"); extensions
// are optional dot-keys the new template gets appended to — additive only, it
// never replaces an existing binding's mode list — zero is fine (bindings can
// be added later from the File bindings tab). On success the panel refreshes
// and this offers "Open in Claude" to start editing the folder.
export function NewTemplateModal({
  knownExtensions,
  onClose,
  onCreated,
}: {
  knownExtensions: string[]; // extensions already bound in the registry, offered one-click
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
  const extInputRef = useRef<HTMLInputElement>(null);
  useEffect(() => () => {
    alive.current = false;
  }, []);

  // Normalize "csv" / " .CSV " → ".csv"; dedupe against existing chips.
  const normalizeExt = (raw: string) => {
    const v = raw.trim().toLowerCase();
    if (!v) return null;
    return v.startsWith(".") ? v : "." + v;
  };

  // Case-insensitive: the registry can't hold both ".csv" and ".CSV" (D87 RMW
  // drops the case-colliding key), so picking both here would silently lose
  // one server-side while this chip list still showed two.
  const addExt = (ext: string) =>
    setExtensions((prev) =>
      prev.some((e) => e.toLowerCase() === ext.toLowerCase()) ? prev : [...prev, ext],
    );

  const addExtension = () => {
    const ext = normalizeExt(extDraft);
    if (ext) addExt(ext);
    setExtDraft("");
  };

  const removeExtension = (ext: string) =>
    setExtensions((prev) => prev.filter((e) => e !== ext));

  // Registry extensions not already picked, matching the current draft as a
  // filter — a quick-pick list on top of free typing.
  const draftNorm = extDraft.trim().toLowerCase().replace(/^\.+/, "");
  const pickedLower = new Set(extensions.map((e) => e.toLowerCase()));
  const suggestions = knownExtensions.filter(
    (ext) =>
      !pickedLower.has(ext.toLowerCase()) && (!draftNorm || ext.slice(1).toLowerCase().includes(draftNorm)),
  );

  const trimmedName = name.trim();
  // Mirror _template_name_error (templates_api.py) exactly, so obvious rejects
  // give an instant inline hint instead of a server roundtrip: one plain path
  // segment, no "/", "\", or "." anywhere, and no leading "_".
  const nameError = trimmedName.includes("/")
    ? 'Name cannot contain "/".'
    : trimmedName.includes("\\")
      ? 'Name cannot contain "\\".'
      : trimmedName.includes(".")
        ? 'Name cannot contain ".".'
        : trimmedName.startsWith("_")
          ? 'Name cannot start with "_".'
          : null;
  const canCreate = trimmedName.length > 0 && !nameError && !busy;

  // Cmd/Ctrl+Enter submits from any field when the name is valid.
  const isSubmitChord = (e: React.KeyboardEvent) => e.key === "Enter" && (e.metaKey || e.ctrlKey);

  const onNameKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (isSubmitChord(e)) {
      if (canCreate) create();
    } else if (canCreate) {
      // Plain Enter with a valid name jumps to the extensions field rather than
      // submitting blind, so bindings can be added in the same keyboard flow.
      extInputRef.current?.focus();
    }
  };

  const onExtKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (isSubmitChord(e)) {
      e.preventDefault();
      if (canCreate) create();
    } else if (e.key === "Enter") {
      e.preventDefault();
      addExtension();
    } else if (e.key === "Backspace" && extDraft === "" && extensions.length > 0) {
      // Backspace on an empty draft peels off the last chip (standard tag-input).
      removeExtension(extensions[extensions.length - 1]);
    }
  };

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
    <Modal
      title="New template"
      onClose={onClose}
      busy={busy}
      dialogClassName="templates-new"
      footer={
        result ? (
          <>
            <button type="button" className="btn btn-secondary" onClick={onClose}>
              Done
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => openInClaude(result.name)}
              disabled={opening}
            >
              {opening ? "Opening…" : "Open in Claude"}
            </button>
          </>
        ) : (
          <>
            <button type="button" className="btn btn-secondary" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button type="button" className="btn btn-primary" onClick={create} disabled={!canCreate}>
              {busy ? "Creating…" : "Create"}
            </button>
          </>
        )
      }
    >
      {result ? (
        <>
          <div className="templates-result">
                <div className="templates-result-line">
                  Created <code>{result.name}</code>.
                </div>
                {result.bindings.length > 0 && (
                  <div className="templates-result-line">
                    <span className="deploy-muted">Added as a mode for:</span>{" "}
                    {result.bindings.map((b) => (
                      <code key={b} className="templates-usedby-chip">
                        {b}
                      </code>
                    ))}
                  </div>
                )}
              </div>
          <p className="deploy-muted">
            Edit the template's files from the file explorer, or open Claude Code in its folder to
            build it out.
          </p>
          {openError && <ErrorBanner>{openError}</ErrorBanner>}
        </>
      ) : (
        <>
              <p className="deploy-muted templates-field-hint">
                Scaffold a new user template. Add it as a mode for file extensions now — it's
                appended to any existing bindings, never replacing them — or leave that empty and
                add bindings later from the File bindings tab.
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
                  onKeyDown={onNameKey}
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
                    ref={extInputRef}
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
                {suggestions.length > 0 && (
                  <div className="templates-ext-suggest-wrap">
                    <span className="deploy-muted templates-field-hint">From your bindings:</span>
                    {/* Truncated to ~3 rows; typing narrows the list to surface the rest. */}
                    <div className="templates-ext-suggestions">
                      {suggestions.map((ext) => (
                        <button
                          key={ext}
                          type="button"
                          className="templates-chip small templates-ext-suggestion"
                          disabled={busy}
                          // preventDefault on mousedown so the input's onBlur (which
                          // would commit the draft) doesn't fire before this click.
                          onMouseDown={(e) => e.preventDefault()}
                          onClick={() => {
                            addExt(ext);
                            setExtDraft("");
                            extInputRef.current?.focus();
                          }}
                        >
                          + {ext}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                <span className="deploy-muted templates-field-hint">
                  Pick a known extension above, or type your own and press Enter (the leading dot is
                  added for you).
                </span>
              </div>
          {error && <ErrorBanner>{error}</ErrorBanner>}
        </>
      )}
    </Modal>
  );
}
