import { useEffect, useRef, useState } from "react";
import {
  deleteTemplate,
  downloadTemplatesExport,
  openTemplateInClaude,
  rawUrl,
} from "../../lib/api";
import type { InventoryTemplate, TemplateInventory } from "../../lib/api";
import { navigate } from "../../lib/router";
import { Modal } from "../../components/modal/Modal";
import { ErrorBanner } from "../../components/ErrorBanner";

type UseFilter = "all" | "used" | "unused";

export function InventoryPanel({
  inventory,
  onImport,
  onNewTemplate,
  onChanged,
}: {
  inventory: TemplateInventory;
  onImport: () => void;
  onNewTemplate: () => void;
  onChanged: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const [useFilter, setUseFilter] = useState<UseFilter>("all");
  const [deleting, setDeleting] = useState<InventoryTemplate | null>(null);

  const toggle = (name: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  // Open targets the template's absolute folder path from the inventory
  // (works for core under .core-templates AND user), NOT a dir derived from the
  // user registry. Open reuses the file explorer's navigate() (Listing.tsx),
  // which stats the path and shows the directory listing.
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
  const openClaude = async (name: string) => {
    setError(null);
    try {
      await openTemplateInClaude(name);
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
        <div className="templates-toolbar-actions">
          <button type="button" className="btn btn-secondary" onClick={onNewTemplate}>
            New template
          </button>
          <button type="button" className="btn btn-secondary" onClick={onImport}>
            Import zip
          </button>
          <button
            type="button"
            className="btn btn-primary"
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
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
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
                    onOpen={() => open(t.path)}
                    onOpenInClaude={g.source.editable ? () => openClaude(t.name) : undefined}
                    onDelete={g.source.editable ? () => setDeleting(t) : undefined}
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
        Edit a template's files from the file explorer — this view manages the pool and its
        bindings, not template internals.
      </p>
      {deleting && (
        <DeleteConfirm
          t={deleting}
          // Pass the THROWING export (not runExport, which swallows errors into
          // panel state) so an export-first delete only proceeds when the
          // recovery zip actually downloaded (TV-16/D92 export-first guarantee).
          onExport={() => downloadTemplatesExport([deleting.name])}
          onClose={() => setDeleting(null)}
          onDeleted={() => {
            // Drop the deleted name from the multi-select so "Export selected"
            // never carries a name the server no longer has.
            setSelected((prev) => {
              if (!prev.has(deleting.name)) return prev;
              const next = new Set(prev);
              next.delete(deleting.name);
              return next;
            });
            setDeleting(null);
            onChanged();
          }}
        />
      )}
    </section>
  );
}

// Confirm modal for deleting a user template (TV-16 / SPEC §2.8, D109). No
// accent/safe-looking button in a destructive dialog: the recommended path
// ("Export & delete") carries honest danger styling, and the riskier
// "Delete without export" is a text-only danger action anchored far left.
// A checkbox controls the orthogonal registry-bindings cleanup. Core templates
// never reach here (no Delete action rendered for a read-only source).
function DeleteConfirm({
  t,
  onExport,
  onClose,
  onDeleted,
}: {
  t: InventoryTemplate;
  onExport: () => Promise<void>;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [cleanBindings, setCleanBindings] = useState(true);
  const [busy, setBusy] = useState<"export" | "delete" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

  const run = async (withExport: boolean) => {
    if (busy !== null) return;
    setError(null);
    try {
      if (withExport) {
        setBusy("export");
        await onExport(); // must succeed before we destroy the folder
      }
      setBusy("delete");
      await deleteTemplate(t.name, cleanBindings);
      onDeleted();
    } catch (e) {
      if (alive.current) {
        setError((e as Error).message);
        setBusy(null);
      }
    }
  };

  return (
    <Modal
      title={`Delete “${t.name}”?`}
      onClose={onClose}
      busy={busy !== null}
      dialogClassName="templates-delete"
      footer={
        <>
          <button
            type="button"
            className="btn btn-danger-text"
            onClick={() => void run(false)}
            disabled={busy !== null}
            title="Delete the folder without saving a recovery zip first"
          >
            Delete without export
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={onClose}
            disabled={busy !== null}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger"
            onClick={() => void run(true)}
            disabled={busy !== null}
          >
            {busy === "export" ? "Exporting…" : busy === "delete" ? "Deleting…" : "Export & delete"}
          </button>
        </>
      }
    >
      <p className="deploy-muted">
        This removes the user template folder for <code>{t.name}</code>. Without a bindings
        cleanup, bindings that use it keep the name and show as broken until you rebind or remove
        them.
      </p>
      <div className="templates-delete-opts">
        <label className="templates-delete-opt">
          <input
            type="checkbox"
            checked={cleanBindings}
            disabled={busy !== null}
            onChange={(e) => setCleanBindings(e.target.checked)}
          />
          <span>Remove registry bindings for this template</span>
        </label>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </Modal>
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
  onOpen,
  onOpenInClaude,
  onDelete,
}: {
  t: InventoryTemplate;
  checked: boolean;
  onToggle: () => void;
  onExport: () => void;
  onOpen: () => void;
  onOpenInClaude?: () => void; // only for editable (user) sources; core is read-only
  onDelete?: () => void; // only for editable (user) sources; core is undeletable
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
        {t.hasCondition && (
          <span
            className="templates-pill"
            title="Has a condition.py — this template only shows for files its condition accepts"
          >
            conditional
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
        <button type="button" className="templates-ghost-btn" onClick={onOpen} title="Open the folder in the file explorer">
          Open
        </button>
        {onOpenInClaude && (
          <button
            type="button"
            className="templates-ghost-btn"
            onClick={onOpenInClaude}
            title="Open Claude Code in this template's folder (Terminal, macOS only)"
          >
            Open in Claude
          </button>
        )}
        {onDelete && (
          <button
            type="button"
            className="templates-ghost-btn danger"
            onClick={onDelete}
            title="Delete this user template"
          >
            Delete
          </button>
        )}
      </td>
    </tr>
  );
}
