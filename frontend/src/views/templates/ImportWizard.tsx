import { Fragment, useEffect, useRef, useState } from "react";
import { commitImport, importTemplates } from "../../lib/api";
import type { ImportItem, ImportResolution, ImportStageResult } from "../../lib/api";

type WizardStep = "choose" | "manifest" | "done";

// One binding chip in step 2. "custom" = user-added via "+ add"; the other
// statuses come from the staging response's recommendedKeys.
type ChipStatus = "new" | "already-bound" | "disabled" | "custom";
interface BindingChip {
  key: string;
  status: ChipStatus;
  on: boolean;
}

export function ImportWizard({
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
  // Author-recommended binding chips, keyed by ORIGINAL staged name.
  const [chips, setChips] = useState<Record<string, BindingChip[]>>({});
  const [applyRecs, setApplyRecs] = useState(true);
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
      // Seed binding chips from the author's recommendations: "new" keys are
      // accepted by default, "disabled" keys are off (the user turned that
      // extension off locally). "already-bound" keys render inert and are
      // never sent — EXCEPT under keep-both, where the import lands under a
      // new name so "already bound" no longer holds; they seed ON so they
      // default to binding when that resolution unlocks them.
      const chipInit: Record<string, BindingChip[]> = {};
      for (const it of res.items) {
        if (it.valid && it.recommendedKeys && it.recommendedKeys.length > 0) {
          chipInit[it.name] = it.recommendedKeys.map((r) => ({
            key: r.key,
            status: r.status,
            on: r.status !== "disabled",
          }));
        }
      }
      setChips(chipInit);
      setApplyRecs(true);
      setStep("manifest");
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const isSkipped = (it: ImportItem) =>
    it.conflictsExisting && (resolutions[it.name] ?? "skip") === "skip";

  const isKeepBoth = (it: ImportItem) =>
    it.conflictsExisting && resolutions[it.name] === "keep-both";

  // Keys to bind, per ORIGINAL staged name: ON chips of non-skipped templates.
  // "already-bound" chips are excluded (a no-op server-side anyway) UNLESS the
  // item resolves keep-both — the renamed copy isn't bound yet, so those keys
  // become real, sendable bindings.
  const activeBindings = (): Record<string, string[]> => {
    const out: Record<string, string[]> = {};
    if (!staged || !applyRecs) return out;
    for (const it of staged.items) {
      if (!it.valid || isSkipped(it)) continue;
      const keepBoth = isKeepBoth(it);
      const keys = (chips[it.name] ?? [])
        .filter((c) => c.on && (keepBoth || c.status !== "already-bound"))
        .map((c) => c.key);
      if (keys.length > 0) out[it.name] = keys;
    }
    return out;
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
      const bindings = activeBindings();
      const res = await commitImport(
        staged.importId,
        payload,
        Object.keys(bindings).length > 0 ? bindings : undefined,
      );
      // The import already landed server-side — refresh the parent even if this
      // wizard unmounted mid-commit. The result/done screen is modal-local, so
      // it stays alive-guarded.
      onImported();
      if (!alive.current) return;
      setResult(res);
      setStep("done");
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const setRes = (name: string, r: ImportResolution) =>
    setResolutions((prev) => ({ ...prev, [name]: r }));

  const toggleChip = (name: string, key: string) =>
    setChips((prev) => ({
      ...prev,
      [name]: (prev[name] ?? []).map((c) => (c.key === key ? { ...c, on: !c.on } : c)),
    }));

  const addChip = (name: string, key: string) =>
    setChips((prev) =>
      (prev[name] ?? []).some((c) => c.key === key)
        ? prev
        : { ...prev, [name]: [...(prev[name] ?? []), { key, status: "custom", on: true }] },
    );

  const validCount = staged?.items.filter((i) => i.valid).length ?? 0;
  const hasRecs = staged?.items.some((i) => i.valid && (chips[i.name]?.length ?? 0) > 0) ?? false;
  const importCount = staged?.items.filter((i) => i.valid && !isSkipped(i)).length ?? 0;
  const bindingCount = Object.values(activeBindings()).reduce((n, keys) => n + keys.length, 0);

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
              {hasRecs && validCount > 0 && (
                <div className="templates-recs-toggle">
                  <label className="templates-recs-toggle-row">
                    <input
                      type="checkbox"
                      checked={applyRecs}
                      onChange={(e) => setApplyRecs(e.target.checked)}
                    />
                    <span>Apply author's recommended bindings</span>
                  </label>
                  <div className="templates-recs-subline deploy-muted">
                    {applyRecs
                      ? "Author of this bundle suggests file extensions for each template. Toggle chips to accept or reject."
                      : "Bindings skipped — templates import as unbound. Bind later in File bindings tab."}
                  </div>
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
                      <Fragment key={it.name}>
                        <ImportRow
                          item={it}
                          resolution={resolutions[it.name] ?? "skip"}
                          onResolution={(r) => setRes(it.name, r)}
                        />
                        {it.valid && (chips[it.name]?.length ?? 0) > 0 && (
                          <ChipStrip
                            item={it}
                            chips={chips[it.name]}
                            enabled={applyRecs}
                            skipped={isSkipped(it)}
                            keepBoth={isKeepBoth(it)}
                            onToggle={(key) => toggleChip(it.name, key)}
                            onAdd={(key) => addChip(it.name, key)}
                          />
                        )}
                      </Fragment>
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
                  {busy
                    ? "Importing…"
                    : hasRecs
                      ? `Import ${importCount} template${importCount === 1 ? "" : "s"}` +
                        (bindingCount > 0 ? ` · ${bindingCount} binding${bindingCount === 1 ? "" : "s"}` : "")
                      : "Import"}
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
                {(result.bindingsApplied?.length ?? 0) > 0 && (
                  <>
                    <div className="templates-result-line">
                      <span className="deploy-muted">Bindings applied:</span>{" "}
                      {result.bindingsApplied!.length}
                    </div>
                    <div className="templates-result-line templates-result-bindings">
                      {groupAppliedBindings(result.bindingsApplied!)}
                    </div>
                  </>
                )}
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

// Compact grouped summary of applied bindings, e.g.
// ".geojson, .parquet → geo-heatmap · .csv, .tsv → csv-2".
function groupAppliedBindings(applied: { key: string; template: string }[]): string {
  const byTemplate = new Map<string, string[]>();
  for (const b of applied) {
    const keys = byTemplate.get(b.template);
    if (keys) keys.push(b.key);
    else byTemplate.set(b.template, [b.key]);
  }
  return Array.from(byTemplate.entries())
    .map(([template, keys]) => keys.join(", ") + " → " + template)
    .join(" · ");
}

function ResultLine({ label, names }: { label: string; names: string[] }) {
  if (names.length === 0) return null;
  return (
    <div className="templates-result-line">
      <span className="deploy-muted">{label}:</span> {names.join(", ")}
    </div>
  );
}

// A registry key is a dot extension (".csv"), a directory pattern ("dir/") or
// the root directory key ("/") — the same shapes the bindings tab accepts.
function isValidCustomKey(key: string): boolean {
  return key.length > 0 && (key.startsWith(".") || key.endsWith("/"));
}

// Strip of recommended-binding chips under a manifest row. Inert (greyed,
// non-interactive) when the master toggle is off or the template resolves to
// skip — nothing is sent for those.
function ChipStrip({
  item,
  chips,
  enabled,
  skipped,
  keepBoth,
  onToggle,
  onAdd,
}: {
  item: ImportItem;
  chips: BindingChip[];
  enabled: boolean;
  skipped: boolean;
  keepBoth: boolean;
  onToggle: (key: string) => void;
  onAdd: (key: string) => void;
}) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const [draftErr, setDraftErr] = useState(false);
  const inert = !enabled || skipped;

  const commitDraft = () => {
    const key = draft.trim();
    if (!isValidCustomKey(key)) {
      setDraftErr(true);
      return;
    }
    onAdd(key);
    setDraft("");
    setDraftErr(false);
    setAdding(false);
  };

  const reEnabled = chips.filter((c) => c.status === "disabled" && c.on);
  const anyOn = chips.some((c) => c.on && (keepBoth || c.status !== "already-bound"));

  return (
    <tr className={"templates-rec-strip" + (inert ? " inert" : "")}>
      <td colSpan={3}>
        <div className="templates-rec-row">
          <span className="templates-rec-label">Recommended for:</span>
          {chips.map((c) =>
            // "already bound" only holds for the ORIGINAL name — under
            // keep-both the copy lands renamed and unbound, so these chips
            // become normal toggles (default ON, badge dropped).
            c.status === "already-bound" && !keepBoth ? (
              <span key={c.key} className="templates-rec-chip bound">
                <span className="templates-rec-key">{c.key}</span>
                <span className="templates-rec-badge bound">already bound</span>
              </span>
            ) : (
              <button
                key={c.key}
                type="button"
                className={"templates-rec-chip" + (c.on ? " on" : " off")}
                onClick={() => onToggle(c.key)}
                disabled={inert}
                title={c.on ? "Click to skip binding " + c.key : "Click to bind " + c.key}
              >
                <span className="templates-rec-key">
                  {c.on ? "✓ " : ""}
                  {c.key}
                </span>
                {c.status === "disabled" && (
                  <span className="templates-rec-badge disabled">disabled by you</span>
                )}
              </button>
            ),
          )}
          {adding ? (
            <input
              type="text"
              className={"templates-rec-add-input" + (draftErr ? " err" : "")}
              value={draft}
              placeholder=".ext or dir/"
              autoFocus
              onChange={(e) => {
                setDraft(e.target.value);
                setDraftErr(false);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitDraft();
                if (e.key === "Escape") {
                  // Cancel the draft without letting the wizard's window-level
                  // Escape handler close the whole modal.
                  e.stopPropagation();
                  setDraft("");
                  setDraftErr(false);
                  setAdding(false);
                }
              }}
              onBlur={() => {
                if (draft.trim() === "") {
                  setAdding(false);
                  setDraftErr(false);
                }
              }}
            />
          ) : (
            <button
              type="button"
              className="templates-rec-chip add"
              onClick={() => setAdding(true)}
              disabled={inert}
              title="Bind an extra extension to this template"
            >
              + add
            </button>
          )}
        </div>
        {!inert && reEnabled.map((c) => (
          <div key={c.key} className="templates-rec-warn">
            Checking {c.key} re-enables an extension you disabled.
          </div>
        ))}
        {!inert && keepBoth && anyOn && (
          <div className="templates-rec-warn">
            Will bind under the renamed copy — added after your existing templates on these extensions.
          </div>
        )}
      </td>
    </tr>
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
