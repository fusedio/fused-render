import { useState } from "react";
import { downloadTemplatesExport, rawUrl, revealPath } from "../../lib/api";
import type { InventoryTemplate, TemplateInventory } from "../../lib/api";
import { navigate } from "../../lib/router";

type UseFilter = "all" | "used" | "unused";

export function InventoryPanel({
  inventory,
  onImport,
}: {
  inventory: TemplateInventory;
  onImport: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const [useFilter, setUseFilter] = useState<UseFilter>("all");

  const toggle = (name: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  // Reveal/Open target the template's absolute folder path from the inventory
  // (works for core under .core-templates AND user), NOT a dir derived from the
  // user registry. Open reuses the file explorer's navigate() (Listing.tsx),
  // which stats the path and shows the directory listing.
  const reveal = async (path: string) => {
    setError(null);
    try {
      await revealPath(path);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const open = (path: string) => {
    navigate(path);
  };
  const runExport = async (names: string[]) => {
    setError(null);
    try {
      await downloadTemplatesExport(names);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const selectedNames = Array.from(selected);

  const q = query.trim().toLowerCase();
  const matches = (t: InventoryTemplate): boolean => {
    if (sourceFilter !== "all" && t.source !== sourceFilter) return false;
    if (useFilter === "used" && t.usedBy.length === 0) return false;
    if (useFilter === "unused" && t.usedBy.length > 0) return false;
    if (q) {
      const hit =
        t.name.toLowerCase().includes(q) || t.usedBy.some((k) => k.toLowerCase().includes(q));
      if (!hit) return false;
    }
    return true;
  };

  const groups = inventory.sources
    .slice()
    // User (higher precedence) group first, core after.
    .sort((a, b) => b.precedence - a.precedence)
    .map((s) => ({ source: s, items: inventory.templates.filter((t) => t.source === s.id && matches(t)) }))
    .filter((g) => g.items.length > 0);

  return (
    <section className="templates-tabpanel">
      <div className="templates-toolbar">
        <input
          type="text"
          className="templates-search"
          placeholder="Search by name or used-by key…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="templates-seg">
          <button
            type="button"
            className={"templates-seg-btn" + (useFilter === "all" ? " active" : "")}
            onClick={() => setUseFilter("all")}
          >
            All
          </button>
          <button
            type="button"
            className={"templates-seg-btn" + (useFilter === "used" ? " active" : "")}
            onClick={() => setUseFilter("used")}
          >
            Used
          </button>
          <button
            type="button"
            className={"templates-seg-btn" + (useFilter === "unused" ? " active" : "")}
            onClick={() => setUseFilter("unused")}
          >
            Unused
          </button>
        </div>
        <select value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)}>
          <option value="all">All sources</option>
          {inventory.sources.map((s) => (
            <option key={s.id} value={s.id}>
              {s.label}
            </option>
          ))}
        </select>
        <button type="button" className="templates-btn-secondary" onClick={onImport}>
          Import zip
        </button>
        <button
          type="button"
          className="templates-btn-primary templates-toolbar-push"
          disabled={selectedNames.length === 0}
          onClick={() => runExport(selectedNames)}
          title={
            selectedNames.length === 0
              ? "Select one or more templates to export"
              : "Export the selected templates as a zip"
          }
        >
          Export selected{selectedNames.length > 0 ? ` (${selectedNames.length})` : ""}
        </button>
      </div>
      {error && <div className="deploy-error">{error}</div>}
      {groups.length === 0 ? (
        <div className="deploy-muted">No templates match.</div>
      ) : (
        groups.map((g) => (
          <div key={g.source.id} className="templates-inv-group">
            <div className="templates-inv-grouphead">
              {g.source.label}
              <span className="templates-inv-count">{g.items.length}</span>
              {!g.source.editable && <span className="templates-lock" title="Read-only source">🔒</span>}
            </div>
            <table className="templates-table templates-inv-table">
              <tbody>
                {g.items.map((t) => (
                  <InventoryRow
                    key={t.name}
                    t={t}
                    checked={selected.has(t.name)}
                    onToggle={() => toggle(t.name)}
                    onExport={() => runExport([t.name])}
                    onReveal={() => reveal(t.path)}
                    onOpen={() => open(t.path)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ))
      )}
      {/* Deleting a template folder has no API in the frozen contract — do it
          from the file explorer (Open in explorer / Reveal in Finder). */}
      <p className="templates-hint">
        Edit or delete a template's files from the file explorer — this view manages the pool and
        its bindings, not template internals.
      </p>
    </section>
  );
}

// The template's icon.svg rendered via the same mask-image + currentColor
// idiom as ModeSwitcher (monochrome, theme-tinted). Templates without an
// icon get the first-letter placeholder box.
function TemplateIcon({ t }: { t: InventoryTemplate }) {
  if (!t.hasIcon) {
    return <span className="templates-inv-icon mode-icon-placeholder">{t.name.charAt(0).toUpperCase()}</span>;
  }
  const mask = `url("${rawUrl(t.path + "/icon.svg")}")`;
  return (
    <span
      className="templates-inv-icon mode-icon-mask"
      style={{ WebkitMaskImage: mask, maskImage: mask }}
    />
  );
}

function InventoryRow({
  t,
  checked,
  onToggle,
  onExport,
  onReveal,
  onOpen,
}: {
  t: InventoryTemplate;
  checked: boolean;
  onToggle: () => void;
  onExport: () => void;
  onReveal: () => void;
  onOpen: () => void;
}) {
  return (
    <tr className="templates-row">
      <td className="templates-inv-check">
        <input type="checkbox" checked={checked} onChange={onToggle} aria-label={"Select " + t.name} />
      </td>
      <td className="templates-inv-name">
        <TemplateIcon t={t} />
        <span>{t.name}</span>
        {t.shadowsCore && (
          <span className="templates-pill" title="A user folder shadows a core folder of the same name">
            shadows core
          </span>
        )}
      </td>
      <td className="templates-inv-usedby">
        {t.usedBy.length === 0 ? (
          <span className="deploy-muted">unused</span>
        ) : (
          t.usedBy.map((k) => (
            <code key={k} className="templates-usedby-chip">
              {k}
            </code>
          ))
        )}
      </td>
      <td className="templates-inv-actions">
        <button type="button" className="templates-ghost-btn" onClick={onExport} title="Export this template as a zip">
          Export
        </button>
        <button type="button" className="templates-ghost-btn" onClick={onReveal} title="Reveal in Finder">
          Reveal
        </button>
        <button type="button" className="templates-ghost-btn" onClick={onOpen} title="Open the folder in the file explorer">
          Open
        </button>
      </td>
    </tr>
  );
}
