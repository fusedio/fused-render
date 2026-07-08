// Deploy modal (SPEC §19): publish the current page to a hosted environment
// through the fused CLI, from the preview header's Deploy button.
//
// Everything real happens server-side (fused_render/deploy.py): the page is
// re-exported to a temp bundle and handed to `fused share create/repoint`; the
// modal is a thin client over /api/deploy*. Its states, in order of checking:
//   1. loading  — config + (reconciled) status fetch in flight
//   2. fused CLI missing — install panel (one-click when the server can pip
//      install the pinned [fused] extra, else the manual hint)
//   3. no hosted envs — guidance to create one (`fused env create`)
//   4. the form — env picker (default: the managed fused-backend env),
//      current deployment card (URL + copy/open), Deploy/Redeploy, Revoke,
//      and the env's share list joined to local pages.
import { useEffect, useMemo, useRef, useState } from "react";
import {
  deployPage,
  getDeployConfig,
  getDeployPreview,
  getDeployStatus,
  installFused,
  listShares,
  revokeDeployment,
} from "../lib/api";
import type { DeployConfig, DeployPreview, Deployment, ShareMount } from "../lib/api";
import { basename } from "../lib/format";

interface DeployModalProps {
  fsPath: string;
  onClose: () => void;
  // Fired whenever the page's deployment pointer changes (deploy/revoke/
  // reconcile), so the header button's live-dot stays in sync.
  onChange: (deployment: Deployment | null) => void;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);
  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard can be unavailable (permissions); the URL is selectable text.
    }
  };
  return (
    <button type="button" className="deploy-copy" onClick={onCopy}>
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

// What deploying would publish, resolved fresh from the page's on-disk state
// (GET /api/deploy/preview) — shown BEFORE the click, and export blockers
// disable Deploy with the exact list instead of a post-click failure.
function PreviewSection({ preview }: { preview: DeployPreview }) {
  if (preview.errors.length > 0) {
    return (
      <div className="deploy-error">
        This page can't be deployed yet:
        {preview.errors.map((e, i) => (
          <div key={i}>• {e}</div>
        ))}
      </div>
    );
  }
  return (
    <div className="deploy-preview">
      <span className="deploy-muted">Will publish:</span>
      <code>{preview.page}</code>
      {preview.entrypoints.map((e) => (
        <code key={"e" + e.path} title={`fused.runPython(${JSON.stringify(e.path)}) → route “${e.name}”`}>
          {e.path}
        </code>
      ))}
      {preview.assets.map((a) => (
        <code key={"a" + a.path} title={`asset “${a.name}” (fused.rawUrl/readFile)`}>
          {a.path}
        </code>
      ))}
      {preview.entrypoints.length === 0 && preview.assets.length === 0 && (
        <span className="deploy-muted">(the page only — no runPython/rawUrl targets)</span>
      )}
    </div>
  );
}

// EVERY mount `fused share list` reports on the env — not just this page's —
// joined server-side to the local pages that deployed them, so this doubles
// as the "which of my files is deployed" view. Loaded lazily: a share list
// shells the CLI (and may hit the network), so it only runs when expanded.
function SharesSection({ env, fsPath }: { env: string; fsPath: string }) {
  const [open, setOpen] = useState(false);
  const [mounts, setMounts] = useState<ShareMount[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await listShares(env);
      setMounts(res.mounts);
    } catch (e) {
      setError((e as Error).message);
      setMounts(null);
    } finally {
      setLoading(false);
    }
  };

  // Re-fetch when the section is open and the env changes.
  useEffect(() => {
    if (open) void load();
    else {
      setMounts(null);
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, env]);

  return (
    <div className="deploy-shares">
      <button
        type="button"
        className="deploy-shares-toggle"
        title={"Everything `fused share list` reports on " + env + " — not just this page"}
        onClick={() => setOpen(!open)}
      >
        {open ? "▾" : "▸"} All deployments on {env}
      </button>
      {open && (
        <div className="deploy-shares-body">
          <div className="deploy-muted">
            Everything deployed to this environment (from <code>fused share list</code>), not
            just this page. Rows with a file name were deployed from this app.
          </div>
          {loading && <div className="deploy-muted">Loading share list…</div>}
          {error && <div className="deploy-error">{error}</div>}
          {mounts && mounts.length === 0 && (
            <div className="deploy-muted">Nothing is deployed on this environment.</div>
          )}
          {mounts && mounts.length > 0 && (
            <table className="deploy-shares-table">
              <tbody>
                {mounts.map((m) => {
                  const mine = m.page === fsPath;
                  return (
                    <tr key={m.token} className={mine ? "mine" : undefined}>
                      <td className="share-page" title={m.page ?? "Deployed by the CLI, another app, or another machine"}>
                        {m.page ? (
                          basename(m.page) + (mine ? " (this page)" : "")
                        ) : (
                          <span className="deploy-muted">not from this app</span>
                        )}
                      </td>
                      <td className="share-token" title={m.token}>
                        {m.token}
                      </td>
                      <td>
                        <span className={"share-status " + m.status}>{m.status}</span>
                      </td>
                      <td>
                        {m.url ? (
                          <a href={m.url} target="_blank" rel="noreferrer">
                            Open ↗
                          </a>
                        ) : (
                          <span
                            className="deploy-muted"
                            title="`fused share list` doesn't report URLs; a link shows once this app has recorded (or can derive) one for this environment"
                          >
                            —
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {mounts && (
            <button type="button" className="deploy-shares-refresh" onClick={load} disabled={loading}>
              Refresh
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default function DeployModal({ fsPath, onClose, onChange }: DeployModalProps) {
  const [config, setConfig] = useState<DeployConfig | null>(null);
  const [preview, setPreview] = useState<DeployPreview | null>(null);
  const [deployment, setDeployment] = useState<Deployment | null>(null);
  const [reconciled, setReconciled] = useState(false);
  // The mount's raw `share list` classification from the last reconcile —
  // "absent" (e.g. after an infra teardown) redeploys as a FRESH create with
  // a NEW link, unlike a revoked tombstone (same-URL revive), so the action
  // label branches on it. null = not checked.
  const [live, setLive] = useState<"active" | "revoked" | "absent" | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedEnv, setSelectedEnv] = useState<string | null>(null);
  const [busy, setBusy] = useState<"deploy" | "revoke" | "install" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const applyDeployment = (d: Deployment | null) => {
    setDeployment(d);
    onChange(d);
  };

  // One load per open: config + the pointer reconciled against `share list`
  // (truth), so a CLI-side revoke shows through the moment the dialog opens.
  // Sequence-guarded: a superseded fetch (fsPath switch, or Retry racing a
  // slow first load) must not land its stale response over a newer one.
  const loadSeq = useRef(0);
  const load = async () => {
    const seq = ++loadSeq.current;
    setLoadError(null);
    setConfig(null);
    try {
      const [cfg, status, prev] = await Promise.all([
        getDeployConfig(),
        getDeployStatus(fsPath, true),
        getDeployPreview(fsPath),
      ]);
      if (seq !== loadSeq.current) return;
      setConfig(cfg);
      setPreview(prev);
      applyDeployment(status.deployment);
      setReconciled(status.reconciled);
      setLive(status.live ?? null);
      setSelectedEnv(
        (prev2) => prev2 ?? status.deployment?.env ?? cfg.default_env,
      );
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setLoadError((e as Error).message);
    }
  };
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsPath]);

  // Escape closes (unless an action is running — don't orphan a deploy click).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && busy === null) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const envs = config?.envs ?? [];
  const env = useMemo(
    () => envs.find((e) => e.name === selectedEnv) ?? null,
    [envs, selectedEnv],
  );

  const onDeploy = async () => {
    if (!env) return;
    setBusy("deploy");
    setActionError(null);
    try {
      applyDeployment(await deployPage(fsPath, env.name));
      setReconciled(true);
      setLive("active");
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const onRevoke = async () => {
    setBusy("revoke");
    setActionError(null);
    try {
      applyDeployment(await revokeDeployment(fsPath));
      setLive("revoked");
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const onInstall = async () => {
    setBusy("install");
    setActionError(null);
    try {
      await installFused();
      await load(); // re-probe: the CLI should now be found
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  // Deploy/Redeploy semantics for the button label. The URL promises are
  // driven by `live` — the mount's VERIFIED `share list` classification —
  // never by the stored pointer alone: an active mount repoints the SAME
  // token (stable URL), a revoked tombstone is revived at the same URL, an
  // ABSENT mount (nothing left to revive; the server does a fresh create)
  // mints a fresh link, and when the check never ran (env unreachable at
  // open, `live` null) the label is a plain "Redeploy" that promises
  // nothing — the card's "unconfirmed" note explains why.
  const samePointerEnv = deployment !== null && deployment.env === selectedEnv;
  const mountAbsent = live === "absent";
  const deployLabel =
    busy === "deploy"
      ? "Deploying…"
      : !samePointerEnv
        ? "Deploy"
        : live === "active"
          ? "Redeploy (same URL)"
          : live === "revoked"
            ? "Redeploy (restore URL)"
            : mountAbsent
              ? "Deploy"
              : "Redeploy";

  const body = () => {
    if (loadError) {
      return (
        <>
          <div className="deploy-error">{loadError}</div>
          <button type="button" onClick={load}>
            Retry
          </button>
        </>
      );
    }
    if (!config) return <div className="deploy-muted">Loading…</div>;

    if (!config.cli.found) {
      return (
        <div className="deploy-section">
          <p>
            Deploying publishes this page through the <code>fused</code> CLI
            (<code>fused share</code>), which is not installed in the server's
            Python environment.
          </p>
          {config.cli.installable ? (
            <button
              type="button"
              className="deploy-primary"
              onClick={onInstall}
              disabled={busy !== null}
            >
              {busy === "install" ? "Installing fused…" : "Install fused into this environment"}
            </button>
          ) : (
            <p className="deploy-muted">
              {config.cli.reason ?? "It cannot be installed automatically."} Install it
              manually: <code>{config.cli.install_hint}</code>
            </p>
          )}
          {actionError && <div className="deploy-error">{actionError}</div>}
        </div>
      );
    }

    if (envs.length === 0) {
      return (
        <div className="deploy-section">
          <p>
            No hosted environments are configured — deploying needs a managed{" "}
            <code>fused</code> environment or an <code>aws</code> environment with a
            provisioned serving plane.
          </p>
          <p className="deploy-muted">
            Create one in a terminal with <code>{config.setup_cli} cloud setup</code> (managed
            backend) or <code>{config.setup_cli} env create</code>; environments are read from{" "}
            <code>{config.envs_file}</code>.
          </p>
          <button type="button" onClick={load}>
            Re-check
          </button>
        </div>
      );
    }

    return (
      <>
        {deployment && (
          <div className={"deploy-current " + deployment.status}>
            <div className="deploy-current-head">
              <span className={"share-status " + deployment.status}>{deployment.status}</span>
              <span className="deploy-muted">
                on {deployment.env}
                {reconciled ? "" : " (unconfirmed — environment unreachable)"}
              </span>
            </div>
            {deployment.url ? (
              <div className="deploy-url-row">
                <a
                  className="deploy-url"
                  href={deployment.url}
                  target="_blank"
                  rel="noreferrer"
                  title={deployment.url}
                >
                  {deployment.url}
                </a>
                <CopyButton text={deployment.url} />
              </div>
            ) : (
              <div className="deploy-muted">
                Token <code>{deployment.token}</code> — this backend doesn't report an
                absolute URL; it is served under your environment's serving-plane base URL.
              </div>
            )}
          </div>
        )}

        {preview && <PreviewSection preview={preview} />}
        <div className="deploy-form-row">
          <label htmlFor="deploy-env-select">Deploy to</label>
          <select
            id="deploy-env-select"
            value={selectedEnv ?? ""}
            onChange={(e) => setSelectedEnv(e.target.value)}
            disabled={busy !== null}
          >
            {envs.map((e) => (
              <option key={e.name} value={e.name}>
                {e.name} ({e.backend === "fused" ? "fused — managed" : e.backend})
              </option>
            ))}
          </select>
          <button
            type="button"
            className="deploy-primary"
            onClick={onDeploy}
            disabled={busy !== null || env === null || (preview !== null && preview.errors.length > 0)}
            title={
              preview !== null && preview.errors.length > 0
                ? "Fix the export problems listed above first"
                : undefined
            }
          >
            {busy === "deploy" && <span className="deploy-spinner" />}
            {deployLabel}
          </button>
          {deployment?.status === "active" && (
            <button
              type="button"
              className="deploy-danger"
              onClick={onRevoke}
              disabled={busy !== null}
              title="Take the URL down (the link stops working until you deploy again)"
            >
              {busy === "revoke" ? "Revoking…" : "Revoke"}
            </button>
          )}
        </div>
        {deployment && selectedEnv !== null && deployment.env !== selectedEnv && (
          <div className="deploy-note">
            This page is already deployed on <b>{deployment.env}</b> — deploying to{" "}
            <b>{selectedEnv}</b> mints an independent new link and this dialog will track
            that one instead (the old mount stays live until revoked from the CLI).
          </div>
        )}
        {samePointerEnv && mountAbsent && (
          <div className="deploy-note">
            The recorded mount no longer exists on <b>{deployment!.env}</b> (e.g. after
            an infra teardown) — deploying mints a fresh link with a <b>new URL</b>.
          </div>
        )}
        <div className="deploy-note deploy-muted">
          Deploys publish as a <b>public share link</b> — an unguessable URL; anyone with
          the link can open it.
        </div>
        {actionError && <div className="deploy-error">{actionError}</div>}
        {selectedEnv !== null && <SharesSection env={selectedEnv} fsPath={fsPath} />}
      </>
    );
  };

  return (
    <div
      className="deploy-overlay"
      onMouseDown={(e) => {
        // Backdrop click closes; clicks inside the dialog don't bubble here
        // because of the stopPropagation below.
        if (busy === null && e.target === e.currentTarget) onClose();
      }}
    >
      <div className="deploy-dialog" role="dialog" aria-modal="true" onMouseDown={(e) => e.stopPropagation()}>
        <div className="deploy-head">
          <h2>Deploy {basename(fsPath)}</h2>
          {/* Same guard as Escape/backdrop: an in-flight deploy/revoke/install
              must not be dismissed with no in-flight indication left visible. */}
          <button
            type="button"
            className="deploy-close"
            title={busy !== null ? "An action is still running" : "Close"}
            onClick={onClose}
            disabled={busy !== null}
          >
            ✕
          </button>
        </div>
        <div className="deploy-body">{body()}</div>
      </div>
    </div>
  );
}
