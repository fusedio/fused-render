import { useState } from "react";
import type { RegistryEntry, RegistryResult } from "../../lib/api";
import { sourceLabel, type BindFilter } from "./helpers";

export function BindingsTable({
  registry,
  onEdit,
  onAdd,
}: {
  registry: RegistryResult;
  onEdit: (entry: RegistryEntry) => void;
  onAdd: () => void;
}) {
  const [filter, setFilter] = useState<BindFilter>("all");
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const [query, setQuery] = useState("");

  const q = query.trim().toLowerCase();
  const rows = registry.entries
    .filter((e) => {
      if (filter === "modified" && !e.overridesCore) return false;
      if (sourceFilter !== "all" && e.resolvedSource !== sourceFilter) return false;
      if (q) {
        const hit =
          e.key.toLowerCase().includes(q) ||
          e.templates.some((t) => t.name.toLowerCase().includes(q));
        if (!hit) return false;
      }
      return true;
    })
    // User-overridden bindings first, then core; key order preserved within
    // each group (stable sort).
    .sort((a, b) => Number(b.overridesCore) - Number(a.overridesCore));

  return (
    <section className="templates-tabpanel">
      <div className="templates-toolbar">
        <input
          type="text"
          className="templates-search"
          placeholder="Search by key or template…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="templates-seg">
          <button
            type="button"
            className={"templates-seg-btn" + (filter === "all" ? " active" : "")}
            onClick={() => setFilter("all")}
          >
            All
          </button>
          <button
            type="button"
            className={"templates-seg-btn" + (filter === "modified" ? " active" : "")}
            onClick={() => setFilter("modified")}
          >
            Modified
          </button>
        </div>
        <select value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)}>
          <option value="all">All sources</option>
          {registry.sources.map((s) => (
            <option key={s.id} value={s.id}>
              {s.label}
            </option>
          ))}
        </select>
        <button type="button" className="templates-btn-primary templates-toolbar-push" onClick={onAdd}>
          + Add extension
        </button>
      </div>
      {rows.length === 0 ? (
        <div className="deploy-muted">No bindings match.</div>
      ) : (
        <table className="templates-table">
          <thead>
            <tr>
              <th>Pattern</th>
              <th>Templates</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.key} onClick={() => onEdit(e)} className="templates-row">
                <td className="templates-col-pattern">
                  {e.overridesCore && <span className="templates-dot" title="User override" />}
                  <code className="templates-key-pill">{e.key}</code>
                </td>
                <td className="templates-col-templates">
                  {e.disabled ? (
                    <span className="templates-pill">Disabled</span>
                  ) : (
                    e.templates.map((t, i) => (
                      <span
                        key={t.name + i}
                        className={
                          "templates-chip small" +
                          (i === 0 ? " default" : "") +
                          (t.exists ? "" : " broken")
                        }
                        title={
                          !t.exists
                            ? "no template folder resolves to this name"
                            : i === 0
                              ? "default mode"
                              : undefined
                        }
                      >
                        {i === 0 && <span className="templates-chip-badge">default</span>}
                        {t.name}
                      </span>
                    ))
                  )}
                  {e.error && (
                    <div className="templates-key-error" title={e.error}>
                      ⚠ {e.error}
                    </div>
                  )}
                </td>
                <td>
                  <span className={"registry-source " + (e.overridesCore ? "user" : "")}>
                    {sourceLabel(registry, e.resolvedSource)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
