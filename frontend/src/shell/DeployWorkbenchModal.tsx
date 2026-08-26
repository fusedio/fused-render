import { useEffect, useId, useState } from "react";
import {
  deployWorkbenchApp,
  planWorkbenchApp,
  type WorkbenchAppDeployment,
  type WorkbenchAppPlan,
} from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Modal } from "@platform/ui/modal/Modal";
import { canvasNameForApp } from "./workbench-deploy-lib";


function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export default function DeployWorkbenchModal({
  page,
  appName,
  onClose,
}: {
  page: string;
  appName: string;
  onClose: () => void;
}) {
  const canvasId = useId();
  const cacheId = useId();
  const shareId = useId();
  const [canvasName, setCanvasName] = useState(() => canvasNameForApp(appName));
  const [cacheMaxAge, setCacheMaxAge] = useState("0s");
  const [share, setShare] = useState(true);
  const [plan, setPlan] = useState<WorkbenchAppPlan | null>(null);
  const [result, setResult] = useState<WorkbenchAppDeployment | null>(null);
  const [busy, setBusy] = useState<"plan" | "deploy" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const request = { page, canvas_name: canvasName, cache_max_age: cacheMaxAge, share };
  const canReview = /^[A-Za-z0-9_]{1,128}$/.test(canvasName) && busy === null;

  useEffect(() => {
    setPlan(null);
    setResult(null);
    setError(null);
  }, [canvasName, cacheMaxAge]);

  const review = async () => {
    setBusy("plan");
    setError(null);
    try {
      setPlan(await planWorkbenchApp(request));
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const deploy = async () => {
    setBusy("deploy");
    setError(null);
    try {
      setResult(await deployWorkbenchApp(request));
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Modal
      title="Deploy to Fused Workbench"
      onClose={onClose}
      busy={busy !== null}
      width={620}
      dialogClassName="workbench-deploy"
      footer={
        result ? (
          <>
            <button type="button" className="btn btn-secondary" onClick={onClose}>
              Done
            </button>
            <a
              className="btn btn-primary workbench-deploy-link"
              href={result.app_url || result.share_url || result.workbench_url}
              target="_blank"
              rel="noreferrer"
            >
              {result.app_url ? "Open app" : "Open Canvas"}
            </a>
          </>
        ) : (
          <>
            <button type="button" className="btn btn-secondary" onClick={onClose} disabled={busy !== null}>
              Cancel
            </button>
            {plan ? (
              <button type="button" className="btn btn-primary" onClick={deploy} disabled={busy !== null}>
                {busy === "deploy" ? "Deploying…" : "Deploy"}
              </button>
            ) : (
              <button type="button" className="btn btn-primary" onClick={review} disabled={!canReview}>
                {busy === "plan" ? "Reviewing…" : "Review deployment"}
              </button>
            )}
          </>
        )
      }
    >
      {result ? (
        <div className="workbench-deploy-stack">
          <p>
            Deployed <code>{result.canvas_name}</code> to the <code>{result.environment}</code>{" "}
            Workbench environment.
          </p>
          <div className="workbench-deploy-links">
            <a href={result.workbench_url} target="_blank" rel="noreferrer">Open in Workbench</a>
            {result.share_url && <a href={result.share_url} target="_blank" rel="noreferrer">Open Canvas share</a>}
            {result.app_url && <a href={result.app_url} target="_blank" rel="noreferrer">Open direct app URL</a>}
          </div>
          {result.warnings.length > 0 && (
            <div className="workbench-deploy-warnings">
              {result.warnings.map((warning) => <p key={warning}>⚠ {warning}</p>)}
            </div>
          )}
        </div>
      ) : (
        <div className="workbench-deploy-stack">
          <p className="deploy-muted">
            This creates or replaces a dedicated Canvas containing one visible HTML shell and
            hidden UDFs for the app's Python calls and embedded assets.
          </p>
          <label className="field" htmlFor={canvasId}>
            <span className="field-label">Canvas name</span>
            <input
              id={canvasId}
              className="field-control"
              value={canvasName}
              onChange={(event) => setCanvasName(event.target.value)}
              spellCheck={false}
            />
            <span className="field-hint">Letters, numbers, and underscores only.</span>
          </label>
          <label className="field" htmlFor={cacheId}>
            <span className="field-label">Python result cache</span>
            <span className="field-select-wrap">
              <select
                id={cacheId}
                className="field-control"
                value={cacheMaxAge}
                onChange={(event) => setCacheMaxAge(event.target.value)}
              >
                <option value="0s">Off</option>
                <option value="5m">5 minutes</option>
                <option value="1h">1 hour</option>
                <option value="24h">24 hours</option>
              </select>
            </span>
          </label>
          <label className="workbench-deploy-check" htmlFor={shareId}>
            <input
              id={shareId}
              type="checkbox"
              checked={share}
              onChange={(event) => setShare(event.target.checked)}
            />
            Resolve a Canvas share URL after pushing
          </label>
          {plan && (
            <div className="workbench-deploy-plan">
              <p>
                <strong>{plan.generated_files.length}</strong> generated files · {formatBytes(plan.generated_bytes)} ·{" "}
                <strong>{Object.keys(plan.entrypoints).length}</strong> Python routes ·{" "}
                <strong>{Object.keys(plan.assets).length}</strong> assets
              </p>
              {plan.warnings.map((warning) => <p key={warning}>⚠ {warning}</p>)}
            </div>
          )}
          {error && <ErrorBanner>{error}</ErrorBanner>}
        </div>
      )}
    </Modal>
  );
}
