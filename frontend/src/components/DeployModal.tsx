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
//      current deployment card (URL + copy/open), Deploy/Redeploy, Revoke.
// The env-wide share list (every mount on an env, with revoke) lives on the
// Preferences page's Deployments section, not here — this modal is scoped to
// the current page.
import { useEffect, useMemo, useRef, useState } from "react";
import {
  deployPage,
  getDeployConfig,
  getDeployPreview,
  getDeployStatus,
  installFused,
  revokeDeployment,
} from "../lib/api";
import type { DeployConfig, DeployPreview, Deployment } from "../lib/api";
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
  // Display each file as a plain relative name. The backend gives the page as a
  // bare basename but entrypoints/assets as their literal fused.runPython/rawUrl
  // argument, which usually carries a leading "./" — so the list mixed "sine.html"
  // with "./sine.py". Strip a leading "./" for the shown text (one consistent root);
  // the exact literal stays in the title tooltip so nothing is lost.
  const rel = (p: string) => p.replace(/^\.\//, "");
  return (
    <div className="deploy-preview">
      <span className="deploy-muted">Will publish:</span>
      <code>{rel(preview.page)}</code>
      {preview.entrypoints.map((e) => (
        <code key={"e" + e.path} title={`fused.runPython(${JSON.stringify(e.path)}) → route “${e.name}”`}>
          {rel(e.path)}
        </code>
      ))}
      {preview.assets.map((a) => (
        <code key={"a" + a.path} title={`asset “${a.name}” (fused.rawUrl/readFile) — ${a.path}`}>
          {rel(a.path)}
        </code>
      ))}
      {preview.entrypoints.length === 0 && preview.assets.length === 0 && (
        <span className="deploy-muted">(the page only — no runPython/rawUrl targets)</span>
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

  // The modal can be closed while an action is still running (#12): guard the
  // modal's own post-action setState so a deploy/revoke/install that resolves
  // after unmount doesn't setState on a dead tree. onChange still fires — it
  // updates the parent header dot, which stays mounted.
  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);

  const applyDeployment = (d: Deployment | null) => {
    if (alive.current) setDeployment(d);
    onChange(d);
  };

  // One load per open: config + the pointer reconciled against `share list`
  // (truth), so a CLI-side revoke shows through the moment the dialog opens.
  // Sequence-guarded: a superseded fetch (fsPath switch, or Retry racing a
  // slow first load) must not land its stale response over a newer one.
  const loadSeq = useRef(0);
  // `background` = a focus/visibility re-reconcile of an already-loaded modal:
  // it must update in place, never tear the form down. So it does NOT clear
  // `config` (which would flash the whole form to "Loading…") and, on
  // failure, leaves the current view intact instead of replacing it with an
  // error. The initial mount load (background=false) still shows "Loading…"
  // and surfaces a load error, since there is nothing to preserve yet.
  const load = async (background = false) => {
    const seq = ++loadSeq.current;
    if (!background) {
      setLoadError(null);
      setConfig(null);
    }
    try {
      const [cfg, status, prev] = await Promise.all([
        getDeployConfig(),
        getDeployStatus(fsPath, true),
        // A preview failure (unexportable file type, file deleted since the
        // header rendered, …) must not kill the whole dialog — degrade it to
        // an export blocker: the form renders, the reason shows in the
        // blocker list, and Deploy stays disabled.
        getDeployPreview(fsPath).catch(
          (e): DeployPreview => ({
            page: basename(fsPath),
            entrypoints: [],
            assets: [],
            errors: [(e as Error).message],
          }),
        ),
      ]);
      if (seq !== loadSeq.current) return;
      setLoadError(null);
      setConfig(cfg);
      setPreview(prev);
      applyDeployment(status.deployment);
      setReconciled(status.reconciled);
      setLive(status.live ?? null);
      // Preselect the deployment's env (or the default) — but only if it is
      // actually in the picker; an env removed from envs.json since deploy
      // must fall back to a selectable one, or the <select> renders blank
      // and Deploy is silently disabled with no matching option (#6). Keep the
      // user's current pick across a background refresh, but only while it still
      // exists in the refreshed list — an env deleted from envs.json while the
      // modal stayed open (a focus/visibility or post-error reconcile can now
      // observe this) must re-derive too, else the select points at a gone option.
      setSelectedEnv((prev2) => {
        if (prev2 !== null && cfg.envs.some((e) => e.name === prev2)) return prev2;
        const preferred = status.deployment?.env ?? cfg.default_env;
        if (preferred && cfg.envs.some((e) => e.name === preferred)) return preferred;
        return cfg.default_env;
      });
    } catch (e) {
      if (seq !== loadSeq.current || background) return; // keep the view on a background failure
      setLoadError((e as Error).message);
    }
  };
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsPath]);

  // Latest-ref pattern: `load` and `busy` are captured fresh every render, so
  // the focus effect below (which subscribes once) always calls the current
  // load with the current busy gate — never a stale closure from the render
  // when the listener was attached.
  const loadRef = useRef(load);
  loadRef.current = load;
  const busyRef = useRef(busy);
  busyRef.current = busy;

  // Re-reconcile when the tab regains focus/visibility (unless an action is
  // running) so the open modal doesn't drift from truth — e.g. the same page
  // revoked out-of-band from the Preferences tab. Without this the header dot
  // (which re-reads on focus) and the open modal would contradict each other
  // (#5). A *background* refresh: it updates in place without flashing the
  // form to "Loading…" or replacing it with an error. loadSeq keeps a focus
  // load from racing the mount load. Subscribed once — freshness comes from
  // the refs, not the dep array.
  useEffect(() => {
    const refresh = () => {
      if (busyRef.current === null && document.visibilityState === "visible") {
        void loadRef.current(true);
      }
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  // Escape closes. Allowed even mid-action (#12): the action continues
  // server-side and onChange still updates the header dot, so the user is
  // never trapped waiting on a slow/hung CLI child.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const envs = config?.envs ?? [];
  const env = useMemo(
    () => envs.find((e) => e.name === selectedEnv) ?? null,
    [envs, selectedEnv],
  );

  // Each handler applies its result (onChange always propagates to the header
  // dot), then guards the modal's OWN setState on `alive` — the dialog may
  // have been closed mid-action (#12).
  const onDeploy = async () => {
    if (!env) return;
    setBusy("deploy");
    setActionError(null);
    try {
      const record = await deployPage(fsPath, env.name);
      applyDeployment(record);
      if (!alive.current) return;
      setReconciled(true);
      setLive("active");
    } catch (e) {
      // A deploy can fail AFTER the server mutated the pointer — the
      // failed-revive compensation path (deploy.py) persists status active or
      // revoked before raising. Show the error, then background-refresh so the
      // card/dot reflect what the server actually left behind, not stale state.
      if (alive.current) {
        setActionError((e as Error).message);
        void load(true);
      }
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const onRevoke = async () => {
    setBusy("revoke");
    setActionError(null);
    try {
      const record = await revokeDeployment(fsPath);
      applyDeployment(record);
      if (!alive.current) return;
      setLive("revoked");
    } catch (e) {
      // Same as onDeploy: a revoke may have partially applied server-side, so
      // pull the true post-action state instead of leaving the card stale.
      if (alive.current) {
        setActionError((e as Error).message);
        void load(true);
      }
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const onInstall = async () => {
    setBusy("install");
    setActionError(null);
    try {
      await installFused();
      if (!alive.current) return;
      await load(); // re-probe: the CLI should now be found
    } catch (e) {
      if (alive.current) setActionError((e as Error).message);
    } finally {
      if (alive.current) setBusy(null);
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
          <button type="button" onClick={() => load()}>
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
            Create one in a terminal with <code>{config.setup_cli} cloud setup</code> — it opens
            a browser sign-in to Fused first, then creates the managed environment (or use{" "}
            <code>{config.setup_cli} env create</code> for a self-hosted AWS one). Environments
            are read from <code>{config.envs_file}</code>.
          </p>
          <button type="button" onClick={() => load()}>
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
        {deployment &&
          selectedEnv !== null &&
          deployment.env !== selectedEnv &&
          envs.some((e) => e.name === deployment.env) && (
            <div className="deploy-note">
              This page is already deployed on <b>{deployment.env}</b> — deploying to{" "}
              <b>{selectedEnv}</b> mints an independent new link and this dialog will track
              that one instead (the old mount stays live until revoked from the CLI).
            </div>
          )}
        {deployment && !envs.some((e) => e.name === deployment.env) && (
          <div className="deploy-note">
            This page was deployed to <b>{deployment.env}</b>, which is no longer a
            configured environment. Deploying here starts a new mount on{" "}
            <b>{selectedEnv}</b>; the old one is unmanaged from this dialog.
          </div>
        )}
        {samePointerEnv && mountAbsent && (
          <div className="deploy-note">
            The recorded mount no longer exists on <b>{deployment!.env}</b> (e.g. after
            an infra teardown) — deploying mints a fresh link with a <b>new URL</b>.
          </div>
        )}
        {env?.backend === "fused" && !config.fused_logged_in && (
          <div className="deploy-note">
            You don't appear to be signed in to Fused — deploying to <b>{env.name}</b> will
            fail until you run <code>{config.setup_cli} cloud login</code> in a terminal (a
            one-time browser sign-in).
          </div>
        )}
        <div className="deploy-note deploy-muted">
          Deploys publish as a <b>public share link</b> — an unguessable URL; anyone with
          the link can open it.
        </div>
        {actionError && <div className="deploy-error">{actionError}</div>}
      </>
    );
  };

  return (
    <div
      className="deploy-overlay"
      onMouseDown={(e) => {
        // Backdrop click closes. Guarded on `busy` so an accidental click-away
        // doesn't abandon an in-flight action — the deliberate ✕/Escape still
        // close mid-action (#12). Clicks inside the dialog don't bubble here
        // because of the stopPropagation below.
        if (busy === null && e.target === e.currentTarget) onClose();
      }}
    >
      <div className="deploy-dialog" role="dialog" aria-modal="true" onMouseDown={(e) => e.stopPropagation()}>
        <div className="deploy-head">
          <h2>Deploy {basename(fsPath)}</h2>
          {/* Always closeable, even mid-action (#12): the action continues
              server-side and onChange keeps the header dot correct, so a slow
              or hung CLI child can never trap the user. */}
          <button
            type="button"
            className="deploy-close"
            title={busy !== null ? "Close (the action keeps running)" : "Close"}
            onClick={onClose}
          >
            ✕
          </button>
        </div>
        <div className="deploy-body">{body()}</div>
      </div>
    </div>
  );
}
