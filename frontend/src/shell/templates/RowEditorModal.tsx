import { useEffect, useId, useMemo, useRef, useState } from "react";
import { PlusIcon, TriangleAlertIcon } from "lucide-react";
import { putRegistryBinding, resetRegistryBinding } from "@platform/lib/api";
import type { RegistryEntry, RegistryResult, TemplateInventory } from "@platform/lib/api";
import { KeyBuilder } from "@shell/templates/KeyBuilder";
import { TemplatePicker } from "@shell/templates/TemplatePicker";
import { TemplatesDialog } from "@shell/templates/TemplatesDialog";
import { TemplateChip, WarnText } from "@shell/templates/chips";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Button } from "@platform/shadcn/ui/button";
import { Field, FieldLabel, FieldTitle } from "@platform/shadcn/ui/field";
import { Identifier, Muted, Tiny } from "@platform/ui/flow/Typography";

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
  const keyInputRef = useRef<HTMLInputElement>(null);

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
  useEffect(() => () => {
    alive.current = false;
  }, []);

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

  // Wired to the dialog's dirty guard: an unsaved edit intercepts the first
  // close attempt. Create mode is dirty once a key or a template is set; edit
  // mode once the ordered list differs from what was loaded.
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
      // if this dialog already unmounted (Escape/✕ closed it mid-request), so a
      // succeeded server write is always reflected. Only dialog-local setState
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
    <TemplatesDialog
      title={mode === "create" ? "Add extension" : "Edit binding"}
      onClose={onClose}
      busy={busy !== null}
      dirty={dirty}
      initialFocus={mode === "create" ? keyInputRef : undefined}
      footer={
        <>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="text-destructive hover:text-destructive sm:mr-auto"
            onClick={doDisable}
            disabled={busy !== null || (mode === "create" && !keyValid)}
            title="Write a null binding — previews are disabled for this type"
          >
            {busy === "disable"
              ? "Disabling…"
              : confirmDisable
                ? "Click again to disable"
                : "Disable for this type"}
          </Button>
          {mode === "edit" && entry?.overridesCore && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={doReset}
              disabled={busy !== null}
              title={
                coreDefault
                  ? "Revert to the core default: " + coreDefault.join(", ")
                  : "Remove this user override"
              }
            >
              {busy === "reset" ? "Resetting…" : "Reset to core"}
            </Button>
          )}
          {/* Intentionally bypasses the dirty guard: an explicit Cancel click
              is explicit intent, unlike Esc/backdrop/✕. */}
          <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={busy !== null}>
            Cancel
          </Button>
          <Button
            type="submit"
            form={formId}
            size="sm"
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
          </Button>
        </>
      }
    >
      <form
        id={formId}
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          void doSave();
        }}
      >
        {mode === "create" ? (
          <Field>
            <FieldLabel htmlFor={keyInputId}>Key</FieldLabel>
            <KeyBuilder
              inputId={keyInputId}
              inputRef={keyInputRef}
              onChange={(k, valid) => {
                setBuiltKey(k);
                setKeyValid(valid);
              }}
            />
          </Field>
        ) : (
          <Field>
            <FieldTitle>Key</FieldTitle>
            <div className="flex items-center gap-2">
              <Identifier className="rounded-md border border-border bg-muted px-1.5 py-0.5 text-foreground">
                {key}
              </Identifier>
              {entry && <Tiny>{entry.keyKind}</Tiny>}
            </div>
          </Field>
        )}

        <Field>
          <FieldTitle>Templates (first is the default)</FieldTitle>
          <div className="flex min-h-9 flex-wrap items-center gap-1.5 rounded-lg border border-border bg-card p-2">
            {chosen.length === 0 && (
              <Muted className="text-xs">
                No templates — add at least one, or disable previews for this type.
              </Muted>
            )}
            {chosen.map((name, i) => {
              // "_"-prefixed names are shell sentinels (_render/_listing) —
              // valid without a template folder. Everything else that has no
              // folder is broken (including the retired "..." token).
              const isSentinel = name.startsWith("_");
              const broken = !isSentinel && !known.has(name);
              return (
                <TemplateChip
                  key={i + " " + name}
                  name={name}
                  isDefault={i === 0}
                  broken={broken}
                  draggable
                  onDragStart={() => (dragIndex.current = i)}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={() => {
                    if (dragIndex.current !== null) move(dragIndex.current, i);
                    dragIndex.current = null;
                  }}
                  title={broken ? "no template folder resolves to this name" : undefined}
                  onRemove={() => setChosen((prev) => prev.filter((_, j) => j !== i))}
                  removeLabel={"Remove " + name}
                />
              );
            })}
            <TemplatePicker
              inventory={inventory}
              registry={registry}
              exclude={chosen}
              open={pickerOpen}
              onOpenChange={setPickerOpen}
              onPick={(name) => {
                setChosen((prev) => (prev.includes(name) ? prev : [...prev, name]));
                setPickerOpen(false);
              }}
              trigger={
                <Button type="button" variant="ghost" size="xs" onClick={() => setPickerOpen((v) => !v)}>
                  <PlusIcon data-icon="inline-start" />
                  Add template
                </Button>
              }
            />
          </div>
          <Tiny>Drag chips to reorder — the first is the default mode.</Tiny>
          {brokenNames.length > 0 && (
            <WarnText className="flex items-start gap-1.5">
              <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
              <span>
                {brokenNames.join(", ")} {brokenNames.length === 1 ? "resolves" : "resolve"} to no
                template folder — a dangling registry pointer that won't render. Remove{" "}
                {brokenNames.length === 1 ? "it" : "them"} here, or add the missing template. Nothing
                is removed automatically.
              </span>
            </WarnText>
          )}
        </Field>

        {error && <ErrorBanner>{error}</ErrorBanner>}

        {mode === "edit" && entry?.overridesCore && coreDefault && (
          <Tiny>Core default: {coreDefault.length > 0 ? coreDefault.join(" → ") : "(none)"}</Tiny>
        )}
      </form>
    </TemplatesDialog>
  );
}
