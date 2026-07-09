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
  const excludeSet = new Set(exclude);
  const groups = inventory.sources
    .map((s) => ({
      source: s,
      items: inventory.templates.filter((t) => t.source === s.id && !excludeSet.has(t.name)),
    }))
    .filter((g) => g.items.length > 0);
  return (
    <div className="templates-picker">
      <div className="templates-picker-head">
        <span className="deploy-muted">Add template</span>
        <button type="button" className="deploy-close" onClick={onClose}>
          ✕
        </button>
      </div>
      <div className="templates-picker-body">
        {groups.length === 0 && <div className="deploy-muted">No more templates to add.</div>}
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
      </div>
    </div>
  );
}
