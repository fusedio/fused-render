import { useEffect, useRef } from "react";
import type { RegistryResult, TemplateInventory } from "../../lib/api";
import { sourceLabel } from "./helpers";

export function TemplatePicker({
  inventory,
  registry,
  exclude,
  onPick,
  onClose,
}: {
  inventory: TemplateInventory;
  registry: RegistryResult;
  exclude: string[];
  onPick: (name: string) => void;
  onClose: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<Element | null>(null);

  // Focus the first cell on open so the popover owns the keyboard, and so Esc
  // (handled below) closes the popover — not the surrounding modal. On close,
  // restore focus to the element that opened the picker (same pattern as the
  // Modal chassis) so the host modal's focus never drops to <body>.
  useEffect(() => {
    restoreRef.current = document.activeElement;
    rootRef.current?.querySelector<HTMLElement>("button")?.focus();
    return () => {
      (restoreRef.current as HTMLElement | null)?.focus?.();
    };
  }, []);

  // Escape must close only the picker even when focus has tabbed outside it
  // (the host modal's trap still includes its own footer controls). Capture
  // phase beats the Modal chassis's document-level bubble listener.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const excludeSet = new Set(exclude);
  const groups = inventory.sources
    .slice()
    // User (higher precedence) group first, core after — matches the Library tab.
    .sort((a, b) => b.precedence - a.precedence)
    .map((s) => ({
      source: s,
      items: inventory.templates.filter((t) => t.source === s.id && !excludeSet.has(t.name)),
    }))
    .filter((g) => g.items.length > 0);
  // Shell sentinels (PT-12) are valid registry names but back no template
  // folder, so they aren't in the inventory — offer them explicitly so a
  // removed `_render`/`_listing` can be added back from the UI.
  const sentinels = ["_render", "_listing"].filter((n) => !excludeSet.has(n));
  const empty = groups.length === 0 && sentinels.length === 0;
  return (
    <div
      className="templates-picker"
      ref={rootRef}
      role="dialog"
      aria-label="Add template"
    >
      <div className="templates-picker-head">
        <span className="deploy-muted">Add template</span>
        <button type="button" className="deploy-close" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </div>
      <div className="templates-picker-body">
        {empty && <div className="deploy-muted">No more templates to add.</div>}
        {groups.map((g) => (
          <div key={g.source.id} className="templates-picker-group">
            <div className="templates-picker-cat">{sourceLabel(registry, g.source.id)}</div>
            {g.items.map((t) => (
              <button
                key={t.name}
                type="button"
                className="templates-picker-cell"
                onClick={() => onPick(t.name)}
              >
                {t.hasIcon && <span className="templates-icon-dot" title="has icon.svg" />}
                <span>{t.name}</span>
              </button>
            ))}
          </div>
        ))}
        {sentinels.length > 0 && (
          <div className="templates-picker-group">
            <div className="templates-picker-cat">Special modes</div>
            {sentinels.map((name) => (
              <button
                key={name}
                type="button"
                className="templates-picker-cell"
                onClick={() => onPick(name)}
                title="Shell built-in mode (no template folder)"
              >
                <span>{name}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
