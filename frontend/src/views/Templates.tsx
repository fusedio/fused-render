// Templates management view (TEMPLATE_MGMT_SPEC §3) — the `/view/_templates`
// sentinel route, entered from the sidebar footer. Two sections on one page:
//   A. Bindings table — one row per registry key (extension → ordered
//      templates). Edit via the Row editor modal (pattern builder + template
//      list), disable, or reset a user override to core.
//   B. Inventory panel — every resolved template folder grouped by source
//      (core = locked/read-only, user = editable), with export / reveal / open
//      and a multi-step import wizard.
//
// Template file CONTENTS are not edited here — that is the file explorer's job
// (§4 non-goal). This view manages bindings + the template pool only.
import { useEffect, useRef, useState } from "react";
import {
  commitImport,
  exportTemplatesUrl,
  getTemplateInventory,
  getTemplateRegistry,
  importTemplates,
  putRegistryBinding,
  resetRegistryBinding,
  revealPath,
} from "../lib/api";
import type {
  ImportItem,
  ImportResolution,
  ImportStageResult,
  InventoryTemplate,
  KeyKind,
  RegistryEntry,
  RegistryResult,
  TemplateInventory,
} from "../lib/api";
import { navigate } from "../lib/router";

// -- shared helpers ----------------------------------------------------------

// Download via a synthetic <a download> click — the export endpoint streams the
// zip as an attachment, so no fetch/blob dance is needed.
function triggerDownload(url: string): void {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

const sourceLabel = (registry: RegistryResult, id: string): string =>
  registry.sources.find((s) => s.id === id)?.label ?? id;

// -- key builder (create mode) ----------------------------------------------

const SEGMENT = /^[A-Za-z0-9_-]+$/;

// Compute the key string + a client-side validity check for a chosen shape and
// the literal the user typed. The server validates authoritatively (§2.3); this
// is just live feedback.
function buildKey(kind: KeyKind, raw: string): { key: string; error: string | null } {
  const literal = raw.trim().replace(/^\.+/, "").replace(/\/+$/, "");
  const segs = literal.split(".").filter((s) => s.length > 0);
  const segsOk = segs.length > 0 && segs.every((s) => SEGMENT.test(s));
  if (kind === "simple") {
    if (segs.length !== 1 || !segsOk) return { key: "." + literal, error: "Enter one extension, e.g. csv" };
    return { key: "." + segs[0], error: null };
  }
  if (kind === "compound") {
    if (segs.length < 2 || !segsOk)
      return { key: "." + literal, error: "Enter at least two segments, e.g. geo.parquet" };
    return { key: "." + segs.join("."), error: null };
  }
  if (kind === "wildcard") {
    if (segs.length < 1 || !segsOk)
      return { key: ".*." + literal, error: "Enter the literal part after the wildcard, e.g. json" };
    return { key: ".*." + segs.join("."), error: null };
  }
  // directory
  if (segs.length !== 1 || !segsOk)
    return { key: "." + literal + "/", error: "Enter one extension, e.g. zarr" };
  return { key: "." + segs[0] + "/", error: null };
}

const KEY_KINDS: { kind: KeyKind; label: string; hint: string }[] = [
  { kind: "simple", label: "Simple", hint: ".ext" },
  { kind: "compound", label: "Compound", hint: ".a.b" },
  { kind: "wildcard", label: "Wildcard", hint: ".*.json" },
  { kind: "directory", label: "Directory", hint: ".ext/" },
];

function KeyBuilder({
  onChange,
}: {
  onChange: (key: string, valid: boolean) => void;
}) {
  const [kind, setKind] = useState<KeyKind>("simple");
  const [raw, setRaw] = useState("");
  const { key, error } = buildKey(kind, raw);

  // Report the derived key up on every change.
  useEffect(() => {
    onChange(key, error === null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, error]);

  const prefix = kind === "wildcard" ? ".*." : ".";
  const suffix = kind === "directory" ? "/" : "";
  return (
    <div className="templates-keybuilder">
      <div className="templates-seg">
        {KEY_KINDS.map((k) => (
          <button
            key={k.kind}
            type="button"
            className={"templates-seg-btn" + (kind === k.kind ? " active" : "")}
            onClick={() => setKind(k.kind)}
            title={k.hint}
          >
            {k.label}
          </button>
        ))}
      </div>
      <div className="templates-key-input">
        <span className="templates-key-fix">{prefix}</span>
        <input
          type="text"
          value={raw}
          autoFocus
          placeholder={kind === "compound" ? "geo.parquet" : kind === "wildcard" ? "json" : "csv"}
          onChange={(e) => setRaw(e.target.value)}
        />
        {suffix && <span className="templates-key-fix">{suffix}</span>}
      </div>
      <div className="templates-key-preview">
        Key: <code>{key}</code>
      </div>
      {raw.trim() !== "" && error && <div className="templates-key-error">{error}</div>}
    </div>
  );
}

// -- template picker (inside the row editor) --------------------------------

function TemplatePicker({
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

// -- row editor modal --------------------------------------------------------

function RowEditorModal({
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

// -- import wizard modal -----------------------------------------------------

type WizardStep = "choose" | "manifest" | "done";

function ImportWizard({
  onClose,
  onImported,
}: {
  onClose: () => void;
  onImported: () => void;
}) {
  const [step, setStep] = useState<WizardStep>("choose");
  const [staged, setStaged] = useState<ImportStageResult | null>(null);
  // Per-item resolution for CONFLICTING valid items; non-conflicting valid
  // items are imported implicitly (see commit below).
  const [resolutions, setResolutions] = useState<Record<string, ImportResolution>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof commitImport>> | null>(null);

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

  const onFile = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const res = await importTemplates(file);
      if (!alive.current) return;
      setStaged(res);
      // Default every conflicting valid item to the safe "skip".
      const init: Record<string, ImportResolution> = {};
      for (const it of res.items) if (it.valid && it.conflictsExisting) init[it.name] = "skip";
      setResolutions(init);
      setStep("manifest");
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const doCommit = async () => {
    if (!staged) return;
    setBusy(true);
    setError(null);
    try {
      // Valid non-conflicting items have no existing folder, so "overwrite"
      // simply lands them at their own name; conflicting items use the user's
      // pick (defaulting to skip). Items with no entry default to skip
      // server-side (§2.7), so we must name every item we want imported.
      const payload: Record<string, ImportResolution> = {};
      for (const it of staged.items) {
        if (!it.valid) continue;
        payload[it.name] = it.conflictsExisting ? resolutions[it.name] ?? "skip" : "overwrite";
      }
      const res = await commitImport(staged.importId, payload);
      if (!alive.current) return;
      setResult(res);
      setStep("done");
      onImported();
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const setRes = (name: string, r: ImportResolution) =>
    setResolutions((prev) => ({ ...prev, [name]: r }));

  const validCount = staged?.items.filter((i) => i.valid).length ?? 0;

  return (
    <div
      className="deploy-overlay"
      onMouseDown={(e) => {
        if (!busy && e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="deploy-dialog templates-import"
        role="dialog"
        aria-modal="true"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="deploy-head">
          <h2>Import templates</h2>
          <button type="button" className="deploy-close" onClick={onClose} disabled={busy}>
            ✕
          </button>
        </div>
        <div className="deploy-body">
          {step === "choose" && (
            <>
              <p className="deploy-muted">
                Choose a <code>.zip</code> of template folders. Each top-level folder with a{" "}
                <code>template.html</code> is a template. The registry is never imported (folders
                only).
              </p>
              <input
                type="file"
                accept=".zip"
                disabled={busy}
                onChange={(e) => onFile(e.target.files?.[0])}
              />
              {busy && <div className="deploy-muted">Staging…</div>}
              {error && <div className="deploy-error">{error}</div>}
            </>
          )}

          {step === "manifest" && staged && (
            <>
              {staged.warnings.length > 0 && (
                <div className="templates-warnings">
                  {staged.warnings.map((w, i) => (
                    <div key={i} className="deploy-muted">
                      ⚠ {w}
                    </div>
                  ))}
                </div>
              )}
              {validCount === 0 ? (
                <div className="deploy-muted">
                  No valid template folders found in this zip (each needs a{" "}
                  <code>template.html</code>).
                </div>
              ) : (
                <table className="templates-import-table">
                  <tbody>
                    {staged.items.map((it) => (
                      <ImportRow
                        key={it.name}
                        item={it}
                        resolution={resolutions[it.name] ?? "skip"}
                        onResolution={(r) => setRes(it.name, r)}
                      />
                    ))}
                  </tbody>
                </table>
              )}
              {error && <div className="deploy-error">{error}</div>}
              <div className="templates-actions">
                <button type="button" className="templates-btn-secondary" onClick={onClose} disabled={busy}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="templates-btn-primary"
                  onClick={doCommit}
                  disabled={busy || validCount === 0}
                >
                  {busy ? "Importing…" : "Import"}
                </button>
              </div>
            </>
          )}

          {step === "done" && result && (
            <>
              <div className="templates-result">
                <ResultLine label="Imported" names={result.imported} />
                <ResultLine
                  label="Renamed"
                  names={Object.entries(result.renamed).map(([from, to]) => from + " → " + to)}
                />
                <ResultLine label="Overwritten" names={result.overwritten} />
                <ResultLine label="Skipped" names={result.skipped} />
              </div>
              <div className="templates-actions">
                <button type="button" className="templates-btn-primary" onClick={onClose}>
                  Done
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function ResultLine({ label, names }: { label: string; names: string[] }) {
  if (names.length === 0) return null;
  return (
    <div className="templates-result-line">
      <span className="deploy-muted">{label}:</span> {names.join(", ")}
    </div>
  );
}

function ImportRow({
  item,
  resolution,
  onResolution,
}: {
  item: ImportItem;
  resolution: ImportResolution;
  onResolution: (r: ImportResolution) => void;
}) {
  if (!item.valid) {
    return (
      <tr className="templates-import-invalid">
        <td className="templates-import-name">{item.name}</td>
        <td colSpan={2} className="deploy-muted">
          skipped — no <code>template.html</code>
        </td>
      </tr>
    );
  }
  return (
    <tr>
      <td className="templates-import-name">
        {item.name}
        <span className="deploy-muted"> · {item.fileCount} files</span>
      </td>
      <td>
        {item.conflictsExisting ? (
          <span className="templates-pill warn">conflicts existing</span>
        ) : (
          <span className="deploy-muted">new</span>
        )}
      </td>
      <td>
        {item.conflictsExisting ? (
          <div className="templates-seg">
            {(["overwrite", "skip", "keep-both"] as ImportResolution[]).map((r) => (
              <button
                key={r}
                type="button"
                className={
                  "templates-seg-btn" +
                  (resolution === r ? " active" : "") +
                  (r === "overwrite" ? " danger" : "")
                }
                onClick={() => onResolution(r)}
                title={
                  r === "overwrite"
                    ? "Replace the existing folder (destructive)"
                    : r === "keep-both"
                      ? "Land as a new -2 folder"
                      : "Keep the existing folder, drop this one"
                }
              >
                {r === "keep-both" ? "Keep both" : r === "overwrite" ? "Overwrite" : "Skip"}
              </button>
            ))}
          </div>
        ) : (
          <span className="deploy-muted">will import</span>
        )}
      </td>
    </tr>
  );
}

// -- Section A: bindings table ----------------------------------------------

type BindFilter = "all" | "modified";

function BindingsTable({
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
  const rows = registry.entries.filter((e) => {
    if (filter === "modified" && !e.overridesCore) return false;
    if (sourceFilter !== "all" && e.resolvedSource !== sourceFilter) return false;
    if (q) {
      const hit =
        e.key.toLowerCase().includes(q) ||
        e.templates.some((t) => t.name.toLowerCase().includes(q));
      if (!hit) return false;
    }
    return true;
  });

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

// -- Section B: inventory panel ---------------------------------------------

function InventoryPanel({
  inventory,
  onImport,
}: {
  inventory: TemplateInventory;
  onImport: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

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

  const selectedNames = Array.from(selected);

  return (
    <section className="templates-tabpanel">
      <div className="templates-toolbar">
        <button type="button" className="templates-btn-secondary" onClick={onImport}>
          Import zip
        </button>
        <button
          type="button"
          className="templates-btn-primary templates-toolbar-push"
          disabled={selectedNames.length === 0}
          onClick={() => triggerDownload(exportTemplatesUrl(selectedNames))}
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
      {inventory.sources.map((s) => {
        const items = inventory.templates.filter((t) => t.source === s.id);
        if (items.length === 0) return null;
        return (
          <div key={s.id} className="templates-inv-group">
            <div className="templates-inv-grouphead">
              {s.label}
              <span className="templates-inv-count">{items.length}</span>
              {!s.editable && <span className="templates-lock" title="Read-only source">🔒</span>}
            </div>
            <table className="templates-table templates-inv-table">
              <tbody>
                {items.map((t) => (
                  <InventoryRow
                    key={t.name}
                    t={t}
                    checked={selected.has(t.name)}
                    onToggle={() => toggle(t.name)}
                    onExport={() => triggerDownload(exportTemplatesUrl([t.name]))}
                    onReveal={() => reveal(t.path)}
                    onOpen={() => open(t.path)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        );
      })}
      {/* Deleting a template folder has no API in the frozen contract — do it
          from the file explorer (Open in explorer / Reveal in Finder). */}
      <p className="templates-hint">
        Edit or delete a template's files from the file explorer — this view manages the pool and
        its bindings, not template internals.
      </p>
    </section>
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
        {t.hasIcon && <span className="templates-icon-dot" title="has icon.svg" />}
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

// -- page --------------------------------------------------------------------

type PageTab = "bindings" | "library";

export default function Templates() {
  const [inventory, setInventory] = useState<TemplateInventory | null>(null);
  const [registry, setRegistry] = useState<RegistryResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editor, setEditor] = useState<{ mode: "create" | "edit"; entry: RegistryEntry | null } | null>(
    null,
  );
  const [importing, setImporting] = useState(false);
  const [tab, setTab] = useState<PageTab>("bindings");
  const loadSeq = useRef(0);

  const load = async () => {
    const seq = ++loadSeq.current;
    try {
      const [inv, reg] = await Promise.all([getTemplateInventory(), getTemplateRegistry()]);
      if (seq !== loadSeq.current) return;
      setInventory(inv);
      setRegistry(reg);
      setError(null);
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="templates-page">
      <div className="templates-header">
        <h1>Templates</h1>
        <p className="templates-subtitle">
          Manage which templates render each file type, browse the template pool, and import or
          export user templates.
        </p>
      </div>
      <div className="templates-tabs">
        <button
          type="button"
          className={"templates-tab" + (tab === "bindings" ? " active" : "")}
          onClick={() => setTab("bindings")}
        >
          File bindings
        </button>
        <button
          type="button"
          className={"templates-tab" + (tab === "library" ? " active" : "")}
          onClick={() => setTab("library")}
        >
          Library
        </button>
      </div>
      {error && <div className="deploy-error">{error}</div>}
      {!error && (!inventory || !registry) && <div className="deploy-muted">Loading…</div>}
      {inventory && registry && tab === "bindings" && (
        <BindingsTable
          registry={registry}
          onEdit={(entry) => setEditor({ mode: "edit", entry })}
          onAdd={() => setEditor({ mode: "create", entry: null })}
        />
      )}
      {inventory && registry && tab === "library" && (
        <InventoryPanel inventory={inventory} onImport={() => setImporting(true)} />
      )}

      {editor && inventory && registry && (
        <RowEditorModal
          mode={editor.mode}
          entry={editor.entry}
          inventory={inventory}
          registry={registry}
          onClose={() => setEditor(null)}
          onSaved={load}
        />
      )}
      {importing && (
        <ImportWizard onClose={() => setImporting(false)} onImported={load} />
      )}
    </div>
  );
}
