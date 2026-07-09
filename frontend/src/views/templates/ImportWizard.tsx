import { useEffect, useRef, useState } from "react";
import { commitImport, importTemplates } from "../../lib/api";
import type { ImportItem, ImportResolution, ImportStageResult } from "../../lib/api";

type WizardStep = "choose" | "manifest" | "done";

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
