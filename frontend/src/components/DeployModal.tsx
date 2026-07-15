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
  walkDir,
} from "../lib/api";
import type { DeployConfig, DeployPreview, Deployment, WalkEntry } from "../lib/api";
import { basename, dirname, formatSize } from "../lib/format";

// A path's bundle key: what dedup/exclude match on. Mirrors the server's
// _asset_key (export.py) for the common case — strip a leading "./"; the exact
// literal is preserved elsewhere for display/tooltips.
const relKey = (p: string) => p.replace(/^\.\//, "");

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

// What deploying would publish, resolved fresh from on-disk state
// (POST /api/deploy/preview) with the user's include/exclude selection applied —
// shown BEFORE the click. Export blockers disable Deploy with the exact list;
// warnings are advisory. The list is editable: drop a file (× → exclude),
// restore it, add extra files (a folder picker), add everything, or reset to the
// auto-detected default.
function FileSelection({
  fsPath,
  preview,
  include,
  exclude,
  disabled,
  setInclude,
  setExclude,
}: {
  fsPath: string;
  preview: DeployPreview;
  include: string[];
  exclude: string[];
  disabled: boolean;
  setInclude: (v: string[]) => void;
  setExclude: (v: string[]) => void;
}) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const [dirFiles, setDirFiles] = useState<WalkEntry[] | null>(null);
  const [dirTruncated, setDirTruncated] = useState(false);
  const [walkBusy, setWalkBusy] = useState(false);
  const [walkError, setWalkError] = useState<string | null>(null);

  const pageBase = basename(fsPath);
  const includeKeys = useMemo(() => new Set(include.map(relKey)), [include]);
  const publishedKeys = useMemo(
    () =>
      new Set([
        ...preview.entrypoints.map((e) => relKey(e.path)),
        ...preview.assets.map((a) => relKey(a.path)),
      ]),
    [preview],
  );

  // × on a row → add to exclude. Exclude is applied last server-side and drops a
  // file by key whether it was auto-detected OR manually included, so this always
  // takes the file out of the bundle; "Restore" (remove from exclude) brings an
  // auto/included file back. include is left untouched so a restore is lossless.
  const removeRow = (path: string) => {
    if (!exclude.some((e) => relKey(e) === relKey(path))) setExclude([...exclude, path]);
  };
  const restore = (path: string) => setExclude(exclude.filter((e) => relKey(e) !== relKey(path)));
  // Adding files (picker / add-all): append to include and clear any exclusion of
  // the same paths, so a previously-dropped file comes back.
  const addFiles = (paths: string[]) => {
    const fresh = paths.filter((p) => !includeKeys.has(relKey(p)));
    if (fresh.length) setInclude([...include, ...fresh]);
    const addedKeys = new Set(paths.map(relKey));
    if (paths.some((p) => exclude.some((e) => relKey(e) === relKey(p))))
      setExclude(exclude.filter((e) => !addedKeys.has(relKey(e))));
  };
  const reset = () => {
    setInclude([]);
    setExclude([]);
  };

  const loadDir = async (): Promise<WalkEntry[]> => {
    setWalkBusy(true);
    setWalkError(null);
    try {
      const r = await walkDir(dirname(fsPath));
      const files = r.entries.filter((e) => !e.is_dir);
      setDirFiles(files);
      setDirTruncated(r.truncated);
      return files;
    } catch (e) {
      setWalkError((e as Error).message);
      return [];
    } finally {
      setWalkBusy(false);
    }
  };

  const openPicker = () => {
    setPickerOpen(true);
    if (dirFiles === null) void loadDir();
  };
  const addAllInFolder = async () => {
    const files = dirFiles ?? (await loadDir());
    addFiles(
      files
        .map((f) => f.rel)
        .filter((rel) => rel !== pageBase && !publishedKeys.has(relKey(rel))),
    );
  };

  // Files on disk that aren't already published and aren't excluded — the picker's
  // candidates. Excluded files live in their own "Excluded" list (with Restore).
  const excludeKeys = useMemo(() => new Set(exclude.map(relKey)), [exclude]);
  const available = (dirFiles ?? []).filter(
    (f) =>
      f.rel !== pageBase && !publishedKeys.has(relKey(f.rel)) && !excludeKeys.has(relKey(f.rel)),
  );

  if (preview.errors.length > 0) {
    // Blocking problems: no editable list — show the fix-it list. (The selection
    // controls stay hidden until the page exports cleanly.)
    return (
      <div className="deploy-error">
        This page can't be deployed yet:
        {preview.errors.map((e, i) => (
          <div key={i}>• {e}</div>
        ))}
      </div>
    );
  }

  const rows = [
    ...preview.entrypoints.map((e) => ({
      path: e.path,
      label: relKey(e.path),
      title: `fused.runPython(${JSON.stringify(e.path)}) → route “${e.name}”`,
      kind: "run" as const,
    })),
    ...preview.assets.map((a) => ({
      path: a.path,
      label: relKey(a.path),
      title: includeKeys.has(relKey(a.path))
        ? `included file — ${a.path}`
        : `asset “${a.name}” (fused.rawUrl/readFile) — ${a.path}`,
      kind: includeKeys.has(relKey(a.path)) ? ("added" as const) : ("asset" as const),
    })),
  ];

  return (
    <div className="deploy-preview">
      <div className="deploy-preview-head">
        <span className="deploy-muted">Will publish:</span>
        <div className="deploy-preview-actions">
          <button type="button" onClick={openPicker} disabled={disabled}>
            Add files…
          </button>
          <button type="button" onClick={() => void addAllInFolder()} disabled={disabled || walkBusy}>
            {walkBusy ? "Scanning…" : "Add all in folder"}
          </button>
          {(include.length > 0 || exclude.length > 0) && (
            <button type="button" onClick={reset} disabled={disabled}>
              Reset to default
            </button>
          )}
        </div>
      </div>

      <ul className="deploy-file-list">
        <li className="deploy-file page">
          <code title={preview.page}>{relKey(preview.page)}</code>
          <span className="deploy-file-tag">page</span>
        </li>
        {rows.map((r) => (
          <li key={r.kind + r.path} className="deploy-file">
            <code title={r.title}>{r.label}</code>
            {r.kind === "run" && <span className="deploy-file-tag">run</span>}
            {r.kind === "added" && <span className="deploy-file-tag added">added</span>}
            <button
              type="button"
              className="deploy-file-remove"
              title="Remove from the bundle"
              onClick={() => removeRow(r.path)}
              disabled={disabled}
            >
              ✕
            </button>
          </li>
        ))}
        {rows.length === 0 && (
          <li className="deploy-muted">(the page only — no runPython/rawUrl targets)</li>
        )}
      </ul>

      {exclude.length > 0 && (
        <div className="deploy-excluded">
          <span className="deploy-muted">Excluded (won't be bundled):</span>
          <ul className="deploy-file-list">
            {exclude.map((p) => (
              <li key={p} className="deploy-file excluded">
                <code title={p}>{relKey(p)}</code>
                <button
                  type="button"
                  className="deploy-file-restore"
                  title="Add back to the bundle"
                  onClick={() => restore(p)}
                  disabled={disabled}
                >
                  Restore
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {pickerOpen && (
        <div className="deploy-picker">
          <div className="deploy-picker-head">
            <span className="deploy-muted">Add files from this page's folder</span>
            <button type="button" onClick={() => setPickerOpen(false)}>
              Done
            </button>
          </div>
          {walkBusy && <div className="deploy-muted">Scanning folder…</div>}
          {walkError && <div className="deploy-error">{walkError}</div>}
          {dirTruncated && (
            <div className="deploy-note deploy-muted">
              This folder is large — some files were omitted from the scan.
            </div>
          )}
          {dirFiles !== null && !walkBusy && available.length === 0 && (
            <div className="deploy-muted">
              No other files to add — everything in the folder is already listed.
            </div>
          )}
          {available.length > 0 && (
            <ul className="deploy-picker-list">
              {available.map((f) => (
                <li key={f.rel}>
                  <button type="button" onClick={() => addFiles([f.rel])} disabled={disabled}>
                    + <code>{f.rel}</code>
                    <span className="deploy-muted">{formatSize(f.size)}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {preview.warnings.length > 0 && (
        <div className="deploy-warnings">
          {preview.warnings.map((w, i) => (
            <div key={i} className="deploy-warning">
              ⚠ {w}
            </div>
          ))}
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
  // The user's file selection, layered on the auto-detected set: `include` adds
  // extra files (as assets), `exclude` drops files. Seeded on open from the
  // stored deployment record (so it reloads the last-published selection) and
  // sent back on Deploy. Both empty = the auto-detected default.
  const [include, setInclude] = useState<string[]>([]);
  const [exclude, setExclude] = useState<string[]>([]);

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
  // Preview (the "will publish" set) is a function of the page's on-disk state
  // AND the current include/exclude — so it lives in its own seq-guarded fetch,
  // re-run whenever fsPath or the selection changes (effect below) and on a
  // background reconcile (to pick up on-disk edits). A fetch failure degrades to
  // an export blocker so the form still renders with Deploy disabled.
  const previewSeq = useRef(0);
  const refreshPreview = async (inc: string[], exc: string[]) => {
    const seq = ++previewSeq.current;
    const prev = await getDeployPreview(fsPath, inc, exc).catch(
      (e): DeployPreview => ({
        page: basename(fsPath),
        entrypoints: [],
        assets: [],
        errors: [(e as Error).message],
        warnings: [],
      }),
    );
    if (seq === previewSeq.current && alive.current) setPreview(prev);
  };

  const load = async (background = false) => {
    const seq = ++loadSeq.current;
    if (!background) {
      setLoadError(null);
      setConfig(null);
    }
    try {
      const [cfg, status] = await Promise.all([
        getDeployConfig(),
        getDeployStatus(fsPath, true),
      ]);
      if (seq !== loadSeq.current) return;
      setLoadError(null);
      setConfig(cfg);
      applyDeployment(status.deployment);
      setReconciled(status.reconciled);
      setLive(status.live ?? null);
      // Seed the selection from the stored record on a fresh open (never on a
      // background refresh — that would clobber the user's in-progress edits).
      // Setting the state triggers the preview effect; a background load instead
      // refreshes the preview explicitly (its inputs didn't change but the page
      // on disk may have).
      if (!background) {
        setInclude(status.deployment?.include ?? []);
        setExclude(status.deployment?.exclude ?? []);
      } else {
        void refreshPreview(include, exclude);
      }
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

  // Re-resolve the "will publish" preview whenever the page or the selection
  // changes (covers the initial seed from load, and every include/exclude edit).
  useEffect(() => {
    void refreshPreview(include, exclude);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsPath, include, exclude]);

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
      const record = await deployPage(fsPath, env.name, include, exclude);
      applyDeployment(record);
      if (!alive.current) return;
      setReconciled(true);
      setLive("active");
      // Re-seed from what was actually persisted, so the list reflects the
      // stored record (e.g. server-side dedup) rather than the raw local lists.
      setInclude(record.include ?? include);
      setExclude(record.exclude ?? exclude);
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

        {preview && (
          <FileSelection
            fsPath={fsPath}
            preview={preview}
            include={include}
            exclude={exclude}
            disabled={busy !== null}
            setInclude={setInclude}
            setExclude={setExclude}
          />
        )}
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
