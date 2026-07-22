import { useEffect, useId, useMemo, useRef, useState } from "react";
import { putRegistryBinding, resetRegistryBinding } from "../../lib/api";
import type { RegistryEntry, RegistryResult, TemplateInventory } from "../../lib/api";
import { KeyBuilder } from "./KeyBuilder";
import { TemplatePicker } from "./TemplatePicker";
import { Modal } from "../../components/modal/Modal";
import { ErrorBanner } from "../../components/ErrorBanner";

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
  const formId = useId();
  const keyInputId = useId();

  // Create mode: the key comes from the builder. Edit mode: the key is fixed.
  const [builtKey, setBuiltKey] = useState("");
  const [keyValid, setKeyValid] = useState(false);
  const key = mode === "edit" && entry ? entry.key : builtKey;

  // Ordered names to edit. Seed from the RAW user value (which keeps a `"..."`
  // splice token and any sentinels intact) so a plain save round-trips the
  // override unchanged instead of collapsing the splice into the expanded core
  // names. Fall back to the effective (expanded) names only when there is no
  // user override yet — i.e. overriding a core-only key, where those names are
  // a sensible starting point. A disabled binding starts empty.
  const initialChosen = useMemo<string[]>(() => {
    if (mode !== "edit" || !entry || entry.disabled) return [];
    if (Array.isArray(entry.userValue)) return entry.userValue;
    return entry.templates.map((t) => t.name);
  }, [mode, entry]);
  const [chosen, setChosen] = useState<string[]>(initialChosen);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [busy, setBusy] = useState<"save" | "disable" | "reset" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmDisable, setConfirmDisable] = useState(false);
  const dragIndex = useRef<number | null>(null);

  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

  const known = new Set(inventory.templates.map((t) => t.name));
  // Names that resolve to no template folder (and aren't a "_" sentinel) —
  // dangling registry pointers. The old "..." splice token is no longer
  // special: it lands here like any other dangling name. We only surface them;
  // the user decides whether to remove or keep (never auto-removed).
  const brokenNames = chosen.filter((n) => !n.startsWith("_") && !known.has(n));
  const move = (from: number, to: number) => {
    if (to < 0 || to >= chosen.length || from === to) return;
    setChosen((prev) => {
      const next = prev.slice();
      const [x] = next.splice(from, 1);
      next.splice(to, 0, x);
      return next;
    });
  };

  const canSave = (mode === "edit" || keyValid) && chosen.length > 0 && busy === null;

  // Wired to the shared Modal's dirty guard: an unsaved edit intercepts the
  // first close attempt. Create mode is dirty once a key or a template is set;
  // edit mode once the ordered list differs from what was loaded.
  const dirty =
    mode === "create"
      ? builtKey.trim() !== "" || chosen.length > 0
      : JSON.stringify(chosen) !== JSON.stringify(initialChosen);

  const doSave = async () => {
    if (!canSave) return;
    setBusy("save");
    setError(null);
    try {
      await putRegistryBinding(key, chosen);
      // onSaved/onClose act on the still-mounted parent page — call them even
      // if this modal already unmounted (Escape/✕ closed it mid-request), so a
      // succeeded server write is always reflected. Only modal-local setState
      // in the catch stays alive-guarded.
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
    <Modal
      title={mode === "create" ? "Add extension" : "Edit binding"}
      onClose={onClose}
      busy={busy !== null}
      dirty={dirty}
      dialogClassName="templates-editor"
      footer={
        <>
          <button
            type="button"
            className="btn btn-danger-text"
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
              className="btn btn-secondary"
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
          {/* Intentionally bypasses the dirty guard: an explicit Cancel click
              is explicit intent, unlike Esc/backdrop/✕. */}
          <button
            type="button"
            className="btn btn-secondary"
            onClick={onClose}
            disabled={busy !== null}
          >
            Cancel
          </button>
          <button
            type="submit"
            form={formId}
            className="btn btn-primary"
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
        </>
      }
    >
      <form
        id={formId}
        onSubmit={(e) => {
          e.preventDefault();
          void doSave();
        }}
      >
        {mode === "create" ? (
          <div className="templates-field">
            <label htmlFor={keyInputId}>Key</label>
            <KeyBuilder
              inputId={keyInputId}
              onChange={(k, valid) => {
                setBuiltKey(k);
                setKeyValid(valid);
              }}
            />
          </div>
        ) : (
          <div className="templates-field">
            <span className="templates-field-label">Key</span>
            <div>
              <code className="templates-key-fixed">{key}</code>
              {entry && <span className="deploy-muted"> · {entry.keyKind}</span>}
            </div>
          </div>
        )}

        <div className="templates-field">
          <span className="templates-field-label">Templates (first is the default)</span>
          <div className="templates-chiplist">
            {chosen.length === 0 && (
              <span className="deploy-muted">
                No templates — add at least one, or disable previews for this type.
              </span>
            )}
            {chosen.map((name, i) => {
              // "_"-prefixed names are shell sentinels (_render/_listing) —
              // valid without a template folder. Everything else that has no
              // folder is broken (including the retired "..." token).
              const isSentinel = name.startsWith("_");
              const broken = !isSentinel && !known.has(name);
              return (
                <span
                  key={i + " " + name}
                  className={
                    "templates-chip" + (i === 0 ? " default" : "") + (broken ? " broken" : "")
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
          {brokenNames.length > 0 && (
            <div className="templates-broken-note">
              ⚠ {brokenNames.join(", ")} {brokenNames.length === 1 ? "resolves" : "resolve"} to no
              template folder — a dangling registry pointer that won't render. Remove{" "}
              {brokenNames.length === 1 ? "it" : "them"} here, or add the missing template. Nothing
              is removed automatically.
            </div>
          )}
        </div>

        {error && <ErrorBanner>{error}</ErrorBanner>}

        {mode === "edit" && entry?.overridesCore && coreDefault && (
          <div className="deploy-muted">
            Core default: {coreDefault.length > 0 ? coreDefault.join(" → ") : "(none)"}
          </div>
        )}
      </form>
    </Modal>
  );
}
