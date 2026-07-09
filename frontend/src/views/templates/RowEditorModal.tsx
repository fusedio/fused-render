import { useEffect, useRef, useState } from "react";
import { putRegistryBinding, resetRegistryBinding } from "../../lib/api";
import type { RegistryEntry, RegistryResult, TemplateInventory } from "../../lib/api";
import { KeyBuilder } from "./KeyBuilder";
import { TemplatePicker } from "./TemplatePicker";

export function RowEditorModal({
  mode,
  entry,
  inventory,
  registry,
  onClose,
  onSaved,
}: {
  mode: "create" | "edit";
  entry: RegistryEntry | null;
  inventory: TemplateInventory;
  registry: RegistryResult;
  onClose: () => void;
  onSaved: () => void;
}) {
  // Create mode: the key comes from the builder. Edit mode: the key is fixed.
  const [builtKey, setBuiltKey] = useState("");
  const [keyValid, setKeyValid] = useState(false);
  const key = mode === "edit" && entry ? entry.key : builtKey;

  // Effective ordered names. A disabled binding starts empty; adding names and
  // saving re-enables it.
  const [chosen, setChosen] = useState<string[]>(
    mode === "edit" && entry && !entry.disabled ? entry.templates.map((t) => t.name) : [],
  );
  const [pickerOpen, setPickerOpen] = useState(false);
  const [busy, setBusy] = useState<"save" | "disable" | "reset" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmDisable, setConfirmDisable] = useState(false);
  const dragIndex = useRef<number | null>(null);

  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const known = new Set(inventory.templates.map((t) => t.name));
  const move = (from: number, to: number) => {
    if (to < 0 || to >= chosen.length || from === to) return;
    setChosen((prev) => {
      const next = prev.slice();
      const [x] = next.splice(from, 1);
      next.splice(to, 0, x);
      return next;
    });
  };

  const canSave =
    (mode === "edit" || keyValid) && chosen.length > 0 && busy === null;

  const doSave = async () => {
    if (!canSave) return;
    setBusy("save");
    setError(null);
    try {
      await putRegistryBinding(key, chosen);
      if (!alive.current) return;
      onSaved();
      onClose();
    } catch (e) {
      if (alive.current) {
        setError((e as Error).message);
        setBusy(null);
      }
    }
  };

  const doDisable = async () => {
    if (busy !== null) return;
    if (!confirmDisable) {
      setConfirmDisable(true);
      return;
    }
    if (mode === "create" && !keyValid) return;
    setBusy("disable");
    setError(null);
    try {
      await putRegistryBinding(key, null);
      if (!alive.current) return;
      onSaved();
      onClose();
    } catch (e) {
      if (alive.current) {
        setError((e as Error).message);
        setBusy(null);
        setConfirmDisable(false);
      }
    }
  };

  const doReset = async () => {
    if (busy !== null || !entry) return;
    setBusy("reset");
    setError(null);
    try {
      await resetRegistryBinding(entry.key);
      if (!alive.current) return;
      onSaved();
      onClose();
    } catch (e) {
      if (alive.current) {
        setError((e as Error).message);
        setBusy(null);
      }
    }
  };

  const coreDefault = entry?.coreTemplates ?? null;

  return (
    <div
      className="deploy-overlay"
      onMouseDown={(e) => {
        if (busy === null && e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="deploy-dialog templates-editor"
        role="dialog"
        aria-modal="true"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="deploy-head">
          <h2>{mode === "create" ? "Add extension" : "Edit binding"}</h2>
          <button type="button" className="deploy-close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="deploy-body">
          {mode === "create" ? (
            <div className="templates-field">
              <label>Key</label>
              <KeyBuilder
                onChange={(k, valid) => {
                  setBuiltKey(k);
                  setKeyValid(valid);
                }}
              />
            </div>
          ) : (
            <div className="templates-field">
              <label>Key</label>
              <div>
                <code className="templates-key-fixed">{key}</code>
                {entry && (
                  <span className="deploy-muted"> · {entry.keyKind}</span>
                )}
              </div>
            </div>
          )}

          <div className="templates-field">
            <label>Templates (first is the default)</label>
            <div className="templates-chiplist">
              {chosen.length === 0 && (
                <span className="deploy-muted">
                  No templates — add at least one, or disable previews for this type.
                </span>
              )}
              {chosen.map((name, i) => {
                const broken = !known.has(name);
                return (
                  <span
                    key={name}
                    className={
                      "templates-chip" +
                      (i === 0 ? " default" : "") +
                      (broken ? " broken" : "")
                    }
                    draggable
                    onDragStart={() => (dragIndex.current = i)}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={() => {
                      if (dragIndex.current !== null) move(dragIndex.current, i);
                      dragIndex.current = null;
                    }}
                    title={broken ? "no template folder resolves to this name" : undefined}
                  >
                    {i === 0 && <span className="templates-chip-badge">default</span>}
                    <span className="templates-chip-name">{name}</span>
                    <button
                      type="button"
                      className="templates-chip-x"
                      title="Remove"
                      onClick={() => setChosen((prev) => prev.filter((_, j) => j !== i))}
                    >
                      ✕
                    </button>
                  </span>
                );
              })}
            </div>
            <div className="templates-add-wrap">
              <button
                type="button"
                className="templates-add-btn"
                onClick={() => setPickerOpen((v) => !v)}
              >
                + Add template
              </button>
              {pickerOpen && (
                <TemplatePicker
                  inventory={inventory}
                  registry={registry}
                  exclude={chosen}
                  onPick={(name) => {
                    setChosen((prev) => (prev.includes(name) ? prev : [...prev, name]));
                    setPickerOpen(false);
                  }}
                  onClose={() => setPickerOpen(false)}
                />
              )}
            </div>
            <div className="deploy-muted templates-reorder-hint">
              Drag chips to reorder — the first is the default mode.
            </div>
          </div>

          {error && <div className="deploy-error">{error}</div>}

          <div className="templates-actions">
            <button
              type="button"
              className="templates-danger-text"
              onClick={doDisable}
              disabled={busy !== null || (mode === "create" && !keyValid)}
              title="Write a null binding — previews are disabled for this type"
            >
              {busy === "disable"
                ? "Disabling…"
                : confirmDisable
                  ? "Click again to disable"
                  : "Disable for this type"}
            </button>
            {mode === "edit" && entry?.overridesCore && (
              <button
                type="button"
                className="templates-btn-secondary"
                onClick={doReset}
                disabled={busy !== null}
                title={
                  coreDefault
                    ? "Revert to the core default: " + coreDefault.join(", ")
                    : "Remove this user override"
                }
              >
                {busy === "reset" ? "Resetting…" : "Reset to core"}
              </button>
            )}
            <button type="button" className="templates-btn-secondary" onClick={onClose} disabled={busy !== null}>
              Cancel
            </button>
            <button
              type="button"
              className="templates-btn-primary"
              onClick={doSave}
              disabled={!canSave}
              title={
                chosen.length === 0
                  ? "Add at least one template"
                  : mode === "create" && !keyValid
                    ? "Enter a valid key first"
                    : undefined
              }
            >
              {busy === "save" ? "Saving…" : "Save"}
            </button>
          </div>
          {mode === "edit" && entry?.overridesCore && coreDefault && (
            <div className="deploy-muted">
              Core default: {coreDefault.length > 0 ? coreDefault.join(" → ") : "(none)"}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
