// Deploy modal (SPEC §19): publish the current page to a hosted environment
// through the fused CLI, from the preview header's Deploy button.
//
// Everything real happens server-side (fused_render/deploy.py): the page is
// re-exported to a temp bundle and handed to `fused share create/repoint`; the
// modal is a thin client over /api/deploy*. Its states, in order of checking:
//   1. loading  — config + (reconciled) status fetch in flight
//   2. fused CLI missing — install panel (one-click when the server can pip
//      install the pinned [fused] extra, else the manual hint)
//   3. no hosted envs — sign in / route to the account tab's setup panel
//      (AWS env creation stays a named terminal flow, SPEC AC-9)
//   4. the form — env picker (default: the managed fused-backend env),
//      current deployment card (URL + copy/open), an optional "Link name"
//      (fresh deploys only — picks the URL's token instead of the default
//      opaque one), Deploy/Redeploy, Revoke.
// The env-wide share list (every mount on an env, with revoke) lives on the
// Fused account tab's Deployments section (SPEC AC-11), not here — this
// modal is scoped to the current page.
import { useEffect, useMemo, useRef, useState } from "react";
import {
  clearCacheDeployment,
  deployPage,
  getDeployConfig,
  getDeployPreview,
  getDeployStatus,
  installFused,
  revokeDeployment,
  walkDir,
} from "../lib/api";
import type {
  CacheClearResult,
  DeployConfig,
  DeployPreview,
  Deployment,
  WalkEntry,
} from "../lib/api";
import { useFusedLogin } from "../lib/account";
import DeploymentErrors from "./DeploymentErrors";
import { basename, dirname, formatSize } from "../lib/format";
import { useRefreshOnReturn } from "../lib/hooks";
import { navigateUrl } from "../lib/router";
import { Modal } from "./modal/Modal";
import { ErrorBanner } from "./ErrorBanner";
import { Select, TextInput } from "./field/fields";

// A path's bundle key: what dedup/exclude match on. Mirrors the server's
// _asset_key (export.py) for the common case — strip a leading "./"; the exact
// literal is preserved elsewhere for display/tooltips.
const relKey = (p: string) => p.replace(/^\.\//, "");

// Caching duration presets (fused's cache_max_age format — a non-negative integer +
// s/m/h/d unit; see fused/agent_core/caching.py's parse_cache_max_age, which itself
// has no upper bound). The real ceiling is the env's `results/` cache-bucket
// lifecycle rule, which hard-expires cached objects and is a GC backstop meant to
// stay comfortably ABOVE any TTL in use (fused repo's spec/caching/storage.md;
// application repo's openfused_server/app/serving.py rejects a `cache_max_age`
// beyond it outright). That rule is fixed at 30 days
// (RESULTS_CACHE_LIFECYCLE_DAYS, application/openfused_server/app/managed/
// provisioning.py) for a managed environment, and matches for a self-hosted AWS
// backend (`openfused-gc-results`, fused/src/fused/agent_core/backends/aws/manage.py).
// 30d itself is deliberately NOT offered here — it would leave zero margin against
// that backstop; 14d keeps a comfortable half-window of slack. "0s" is off and not
// itself an option here — the checkbox controls that axis.
const CACHE_DURATION_PRESETS: { value: string; label: string }[] = [
  { value: "1m", label: "1 minute" },
  { value: "5m", label: "5 minutes" },
  { value: "15m", label: "15 minutes" },
  { value: "1h", label: "1 hour" },
  { value: "6h", label: "6 hours" },
  { value: "1d", label: "1 day" },
  { value: "7d", label: "7 days" },
  { value: "14d", label: "14 days" },
];
const DEFAULT_CACHE_DURATION = "1h";

// A chosen link name's allowed shape — mirrors the fused CLI's own mount-token
// rule (fused repo's agent_core/mounts.py validate_token): lowercase letters,
// digits, `-`/`_`, starting with a letter or digit. Checked client-side so a
// bad name is caught before the round trip; the CLI is still the authority
// (an already-taken name only it can know about surfaces from the deploy call).
const TOKEN_RE = /^[a-z0-9][a-z0-9_-]*$/;

// The preset list, plus the current value as its own option when it isn't a preset
// (e.g. a duration set via `share create --cache-max-age` outside this dialog) — so
// the <select> always shows the TRUE value rather than silently falling back to
// the first preset while a redeploy would still send the real, unlisted one.
function cacheDurationOptions(current: string) {
  if (CACHE_DURATION_PRESETS.some((o) => o.value === current)) return CACHE_DURATION_PRESETS;
  return [{ value: current, label: current }, ...CACHE_DURATION_PRESETS];
}

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
  const [collapsed, setCollapsed] = useState(false);
  const [dirFiles, setDirFiles] = useState<WalkEntry[] | null>(null);
  const [dirTruncated, setDirTruncated] = useState(false);
  const [walkBusy, setWalkBusy] = useState(false);
  const [walkError, setWalkError] = useState<string | null>(null);

  const pageBase = basename(fsPath);
  const includeKeys = useMemo(() => new Set(include.map(relKey)), [include]);
  const excludeKeys = useMemo(() => new Set(exclude.map(relKey)), [exclude]);
  // The page's default publish set (runPython/rawUrl/readFile literals), from the
  // server — the authority on whether a file is auto-detected vs a manual include.
  const autoKeys = useMemo(() => new Set(preview.auto.map(relKey)), [preview.auto]);

  // × on a row. Branch on whether the file is auto-detected (in the default set):
  //  - auto → move to `exclude` (suppresses it even if it's ALSO in `include`, and
  //    surfaces it under "Excluded" with a Restore); drop any stale include entry
  //    so the lists stay clean. Excluding is the only way to remove an auto file,
  //    since the page still references it.
  //  - purely manual (in include, not auto) → just drop it from `include`; it was
  //    never a default, so no "Excluded" tombstone.
  const removeRow = (path: string) => {
    const key = relKey(path);
    if (autoKeys.has(key)) {
      if (includeKeys.has(key)) setInclude(include.filter((i) => relKey(i) !== key));
      if (!excludeKeys.has(key)) setExclude([...exclude, path]);
    } else {
      setInclude(include.filter((i) => relKey(i) !== key));
    }
  };
  const restore = (path: string) => setExclude(exclude.filter((e) => relKey(e) !== relKey(path)));
  // Adding files (picker / add-all): append to `include` only. It never touches
  // `exclude` — a deliberate exclusion must not be silently cleared by an add — so
  // re-including an excluded file goes through Restore. Callers only ever pass
  // candidates that are neither auto, already included, nor excluded (see
  // `available`), so there's nothing to un-exclude here anyway.
  const addFiles = (paths: string[]) => {
    const fresh = paths.filter((p) => !includeKeys.has(relKey(p)));
    if (fresh.length) setInclude([...include, ...fresh]);
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

  // A file on disk is a candidate to add when it isn't the page, isn't already in
  // the default set, isn't already manually included, and isn't excluded (excluded
  // files live in the "Excluded" list with Restore). Shared by the picker and
  // "Add all in folder", so the two never re-add or un-exclude the same files.
  const isCandidate = (rel: string) => {
    const key = relKey(rel);
    return (
      rel !== pageBase && !autoKeys.has(key) && !includeKeys.has(key) && !excludeKeys.has(key)
    );
  };

  const openPicker = () => {
    setPickerOpen(true);
    if (dirFiles === null) void loadDir();
  };
  const addAllInFolder = async () => {
    const files = dirFiles ?? (await loadDir());
    addFiles(files.map((f) => f.rel).filter(isCandidate));
  };

  const available = (dirFiles ?? []).filter((f) => isCandidate(f.rel));

  // Advisory (non-blocking) notes — shown whether or not there are blockers, since
  // the backend can return both at once (SPEC DP-2a: warnings appear alongside).
  const warningsBlock =
    preview.warnings.length > 0 ? (
      <div className="deploy-warnings">
        {preview.warnings.map((w, i) => (
          <div key={i} className="deploy-warning">
            ⚠ {w}
          </div>
        ))}
      </div>
    ) : null;

  if (preview.errors.length > 0) {
    // Blocking problems: show the fix-it list. The full editable preview is
    // unavailable (the scan failed), but a bad selection can BE the cause — a
    // persisted `include` for a file that no longer exists fails the preview
    // even when the page is fine. So still offer a recovery path: list the
    // manually-included files with a remove, plus Reset — otherwise the modal
    // traps the user with no way to clear the offending selection. (Only
    // `include` can error; `exclude` just filters, so it's not shown here.)
    return (
      <>
        <ErrorBanner>
          This page can't be deployed yet:
          {preview.errors.map((e, i) => (
            <div key={i}>• {e}</div>
          ))}
        </ErrorBanner>
        {include.length > 0 && (
          <div className="deploy-files">
            <div className="deploy-files-body">
              <span className="deploy-muted">
                Files you added (remove one that no longer exists, or reset):
              </span>
              <ul className="deploy-file-list">
                {include.map((p) => (
                  <li key={p} className="deploy-file">
                    <code title={p}>{relKey(p)}</code>
                    <span className="deploy-file-tag added">added</span>
                    <button
                      type="button"
                      className="deploy-file-action deploy-file-remove"
                      title="Remove from the bundle"
                      onClick={() => removeRow(p)}
                      disabled={disabled}
                    >
                      ✕
                    </button>
                  </li>
                ))}
              </ul>
              <div className="deploy-preview-actions">
                <button type="button" onClick={reset} disabled={disabled}>
                  Reset to default
                </button>
              </div>
            </div>
          </div>
        )}
        {warningsBlock}
      </>
    );
  }

  // A publish-list row. `tag` is the pill's CSS modifier class, `tagText` its
  // (case-preserved) label — decoupled so the pill can read "rawUrl" while the
  // class stays lowercase.
  type Row = { path: string; label: string; title: string; tag: string; tagText: string };
  const rows: Row[] = [
    ...preview.entrypoints.map(
      (e): Row => ({
        path: e.path,
        label: relKey(e.path),
        title: `fused.runPython(${JSON.stringify(e.path)}) → route “${e.name}”`,
        tag: "run",
        tagText: "run",
      }),
    ),
    ...preview.assets.map((a): Row => {
      // Every asset is served read-only on the hosted `_asset` route — the surface
      // fused.rawUrl()/readFile() fetch from. The pill mentions rawUrl/readFile
      // exposure and names HOW the file got bundled (a.source, from the server):
      //   reference → the page fetches it via a literal fused.rawUrl()/readFile()
      //   manifest  → declared in the page's fused-bundle manifest to back a
      //               *computed* rawUrl/readFile path (so it auto-shows here)
      //   include   → added by hand (Add files / Add all in folder)
      const served = `served read-only at _asset/${a.name} — the surface fused.rawUrl()/readFile() fetch from`;
      if (a.source === "include") {
        return {
          path: a.path,
          label: relKey(a.path),
          title: `Added file — ${served} — ${a.path}`,
          tag: "added",
          tagText: "added",
        };
      }
      if (a.source === "manifest") {
        return {
          path: a.path,
          label: relKey(a.path),
          title: `Declared in the page's fused-bundle manifest to back a computed fused.rawUrl()/readFile() path — ${served} — ${a.path}`,
          tag: "rawurl",
          tagText: "bundle",
        };
      }
      return {
        path: a.path,
        label: relKey(a.path),
        title: `Fetched by the page via fused.rawUrl()/readFile() — ${served} — ${a.path}`,
        tag: "rawurl",
        tagText: "rawUrl",
      };
    }),
  ];

  const publishCount = rows.length + 1; // + the page itself
  const summary =
    `${publishCount} file${publishCount === 1 ? "" : "s"}` +
    (exclude.length ? ` · ${exclude.length} excluded` : "");

  return (
    <div className="deploy-files">
      <button
        type="button"
        className="deploy-files-head"
        aria-expanded={!collapsed}
        onClick={() => setCollapsed((c) => !c)}
      >
        <span className="deploy-files-chevron" aria-hidden="true">
          {collapsed ? "▸" : "▾"}
        </span>
        <span className="deploy-files-title">Files to publish</span>
        <span className="deploy-files-count">{summary}</span>
      </button>

      {!collapsed && (
        <div className="deploy-files-body">
          <div className="deploy-preview-actions">
            <button type="button" onClick={openPicker} disabled={disabled}>
              Add files…
            </button>
            <button
              type="button"
              onClick={() => void addAllInFolder()}
              disabled={disabled || walkBusy}
            >
              {walkBusy ? "Scanning…" : "Add all in folder"}
            </button>
            {(include.length > 0 || exclude.length > 0) && (
              <button type="button" onClick={reset} disabled={disabled}>
                Reset to default
              </button>
            )}
          </div>

          <ul className="deploy-file-list">
            <li className="deploy-file">
              <code title={preview.page}>{relKey(preview.page)}</code>
              <span className="deploy-file-tag">page</span>
              <span className="deploy-file-action" />
            </li>
            {rows.map((r) => (
              <li key={r.tag + r.path} className="deploy-file">
                <code title={r.title}>{r.label}</code>
                <span className={"deploy-file-tag " + r.tag}>{r.tagText}</span>
                <button
                  type="button"
                  className="deploy-file-action deploy-file-remove"
                  title="Remove from the bundle"
                  onClick={() => removeRow(r.path)}
                  disabled={disabled}
                >
                  ✕
                </button>
              </li>
            ))}
            {rows.length === 0 && (
              <li className="deploy-muted deploy-file-empty">
                (the page only — no runPython/rawUrl targets)
              </li>
            )}
          </ul>

          {exclude.length > 0 && (
            <div className="deploy-excluded">
              <span className="deploy-muted">Excluded (won't be bundled):</span>
              <ul className="deploy-file-list">
                {exclude.map((p) => (
                  <li key={p} className="deploy-file excluded">
                    <code title={p}>{relKey(p)}</code>
                    <span className="deploy-file-tag none" />
                    <button
                      type="button"
                      className="deploy-file-action deploy-file-restore"
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
              {walkError && <ErrorBanner>{walkError}</ErrorBanner>}
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
                        <span className="deploy-picker-add" aria-hidden="true">
                          +
                        </span>
                        <code>{f.rel}</code>
                        <span className="deploy-muted">{formatSize(f.size)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}

      {warningsBlock}
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
  const [busy, setBusy] = useState<"deploy" | "revoke" | "install" | "clear-cache" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // The user's file selection, layered on the auto-detected set: `include` adds
  // extra files (as assets), `exclude` drops files. Seeded on open from the
  // stored deployment record (so it reloads the last-published selection) and
  // sent back on Deploy. Both empty = the auto-detected default.
  const [include, setInclude] = useState<string[]>([]);
  const [exclude, setExclude] = useState<string[]>([]);
  // The caching choice: "0s" (off, the default) or a duration like "5m"/"1h" —
  // fused's cache_max_age. Seeded on open from the stored record (like include/
  // exclude) and sent back on every Deploy; there is no "leave it as it was".
  const [cacheMaxAge, setCacheMaxAge] = useState<string>("0s");
  // How the NEXT create's link is named — an explicit either/or, not "blank =
  // random" (which read as an unfinished field). "random" (the default, safe
  // choice) mints the opaque unguessable token; "named" uses `customToken` as
  // an explicit, deliberately guessable URL segment. Reset on a fresh open (see
  // `load`); re-seeded from the current mount when the user clicks "Change
  // link" on a live deployment (see `enterChangeLink`).
  const [tokenMode, setTokenMode] = useState<"random" | "named">("random");
  const [customToken, setCustomToken] = useState("");
  // A live mount's token is fixed (repoint/recreate keep it), so the Link
  // picker is normally hidden once deployed — replaced by a read-only summary.
  // "Change link" flips this on to re-reveal the picker; deploying then takes
  // the force_new path (mint a NEW token, best-effort revoke the old one), the
  // only way to actually change a mount's URL. Reset on a fresh open and after
  // a successful deploy.
  const [changingLink, setChangingLink] = useState(false);
  // The result of the last "Clear cache" click (deleted/scope), shown as a status
  // line until the next load/action clears it.
  const [clearCacheResult, setClearCacheResult] = useState<CacheClearResult | null>(null);
  // Progressive disclosure: both start collapsed — a summary line is enough
  // until the user asks for more (caching's edit controls; the diagnostics
  // panel, which is also the mount switch for whether DeploymentErrors is
  // mounted at all, so it never fetches until opened).
  const [cachingOpen, setCachingOpen] = useState(false);
  const [errorsOpen, setErrorsOpen] = useState(false);
  // The Link section collapses like Caching — a one-line summary of the current
  // setting is enough until the user wants to change it.
  const [linkOpen, setLinkOpen] = useState(false);
  // True while a preview fetch is in flight — the shown "Files to publish" list may
  // not yet reflect the latest include/exclude edit, so Deploy is held until it
  // catches up (keeps the click WYSIWYG: never deploy a set the list doesn't show).
  const [previewPending, setPreviewPending] = useState(true);
  // False until `load` has seeded include/exclude from the deployment record on
  // open. The preview effect is gated on this so we NEVER issue a preview for the
  // initial empty selection (which would race the seeded request and could commit
  // the default bundle after state already holds the persisted selection). The
  // first — and only correct — preview request fires once the selection is known.
  const [selectionReady, setSelectionReady] = useState(false);
  // Latest-ref mirrors of the selection. A background reconcile (load(true)) is
  // async: it must refresh the preview with the selection current at COMPLETION,
  // not the value captured when its closure was created — otherwise a focus/
  // visibility reconcile finishing after an edit overwrites the list (and, being
  // the newest fetch, wins the seq race) with a preview for the stale selection.
  const includeRef = useRef(include);
  includeRef.current = include;
  const excludeRef = useRef(exclude);
  excludeRef.current = exclude;

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
    if (alive.current) setPreviewPending(true);
    const prev = await getDeployPreview(fsPath, inc, exc).catch(
      (e): DeployPreview => ({
        page: basename(fsPath),
        entrypoints: [],
        assets: [],
        auto: [],
        errors: [(e as Error).message],
        warnings: [],
      }),
    );
    // Only the latest request settles the view: a superseded fetch leaves both
    // `preview` and `previewPending` for the newer one to resolve, so Deploy
    // stays held until what's shown matches the current selection.
    if (seq === previewSeq.current && alive.current) {
      setPreview(prev);
      setPreviewPending(false);
    }
  };

  const load = async (background = false) => {
    const seq = ++loadSeq.current;
    if (!background) {
      setLoadError(null);
      setConfig(null);
      // Re-gate the preview until this load seeds the selection, so an fsPath
      // switch can't issue/commit a preview for the old-or-empty selection.
      setSelectionReady(false);
      // Drop the previous page's preview too (not just config): a fresh open —
      // including an fsPath switch with the modal still mounted — must not leave
      // last page's "Files to publish" list on screen while the new fetch is in
      // flight. FileSelection only renders when BOTH config and preview are set,
      // so clearing preview keeps it hidden until the new page's preview lands —
      // no stale rows, and no × / restore edit that could target the wrong page.
      setPreview(null);
      // A stale "N cleared" note from the previous page must not linger.
      setClearCacheResult(null);
      // A fresh open (including an fsPath switch) starts collapsed — an
      // expanded state from the previous page shouldn't carry over onto one
      // that hasn't been asked about yet.
      setCachingOpen(false);
      setErrorsOpen(false);
      setLinkOpen(false);
      setChangingLink(false);
      setTokenMode("random");
      setCustomToken("");
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
        setCacheMaxAge(status.deployment?.cache_max_age ?? "0s");
        // Selection is now known — open the gate; this (with the seeded include/
        // exclude) triggers the effect to fetch the first, correct preview.
        setSelectionReady(true);
      } else {
        // Refs, not the closure's include/exclude: this runs after the await, so
        // read the selection as it stands now (an edit may have landed meanwhile).
        void refreshPreview(includeRef.current, excludeRef.current);
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

  // In-app sign-in for the managed backend (SPEC §27, AC-9):
  // replaces the old "run `fused cloud login` in a terminal" guidance. The
  // warning flips off immediately on the poll's own confirmation — the
  // background reload just refreshes the rest, and its failure self-heals on
  // the next focus refresh instead of stranding a signed-in user behind it.
  const signin = useFusedLogin(() => {
    setConfig((prev) => (prev ? { ...prev, fused_logged_in: true } : prev));
    void load(true);
  });

  // Re-resolve the "will publish" preview whenever the selection changes — but
  // only once `load` has seeded it (selectionReady), so the initial empty
  // selection never issues a preview that could race the seeded one.
  useEffect(() => {
    if (!selectionReady) return;
    void refreshPreview(include, exclude);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsPath, include, exclude, selectionReady]);

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
  // load from racing the mount load; freshness comes from the refs.
  useRefreshOnReturn(() => {
    if (busyRef.current === null) void loadRef.current(true);
  });

  const envs = config?.envs ?? [];
  const env = useMemo(
    () => envs.find((e) => e.name === selectedEnv) ?? null,
    [envs, selectedEnv],
  );

  // A redeploy targets an existing, still-known mount (repoint / recreate
  // --same-token) — both keep the mount's ORIGINAL token, so a chosen link
  // name only ever applies to a fresh `share create`: no deployment yet, a
  // different env, or the recorded mount gone from `share list` entirely.
  const samePointerEnv = deployment !== null && deployment.env === selectedEnv;
  const mountAbsent = live === "absent";
  const isRedeploy = samePointerEnv && !mountAbsent;
  // The link-name picker's gate is deliberately NARROWER than isRedeploy:
  // `live` is null both when reconcile never ran and when it ran but the env
  // was unreachable (deployStatus's "couldn't be confirmed" branch below) —
  // in that case we do NOT actually know yet whether the next Deploy click
  // will repoint or fall through to a fresh create (deploy_page re-checks
  // `share list` live, at click time, independent of this stale read). So
  // only a CONFIRMED still-live mount (active or revoked) hides the picker;
  // an unconfirmed one leaves it up, and a name chosen there is simply
  // ignored server-side if the click turns out to repoint after all
  // (deploy_page only applies custom_token on its create branches).
  const confirmedRedeployToken = samePointerEnv && (live === "active" || live === "revoked");
  // The link picker (radios) is shown for a fresh create — no confirmed live
  // mount whose token a redeploy would reuse — OR when the user has explicitly
  // asked to Change a live mount's link (which deploys force_new). Otherwise a
  // confirmed mount shows a read-only summary of its current link instead.
  const pickingLink = !confirmedRedeployToken || changingLink;
  const namedTokenActive = pickingLink && tokenMode === "named";
  const trimmedToken = customToken.trim();
  // Two distinct not-ready states for a named link, kept apart so the UI can
  // treat them differently: a malformed name (non-empty but wrong shape) is a
  // hard red error; a not-yet-typed name is a quiet prompt, so picking the
  // "Custom name" radio doesn't flash red before the user has typed anything.
  // Both block Deploy.
  const tokenFormatError =
    namedTokenActive && trimmedToken !== "" && !TOKEN_RE.test(trimmedToken)
      ? "Use lowercase letters, numbers, - and _ only, starting with a letter or number."
      : null;
  const tokenIncomplete = namedTokenActive && trimmedToken === "";
  // Change-link is a force_new: `share create --token <name>` runs BEFORE the
  // old mount is revoked (create-then-revoke is the safe order — a failed
  // create must never take the live page down), so reusing the CURRENT name
  // would collide on create and fail. Block it up front with a clear prompt
  // rather than let the "Deploy new link" click 400 on a token clash — keeping
  // the same name isn't a change anyway.
  const tokenUnchanged =
    changingLink && namedTokenActive && trimmedToken !== "" && trimmedToken === deployment?.token;
  const tokenBlocksDeploy = tokenFormatError !== null || tokenIncomplete || tokenUnchanged;

  // Each handler applies its result (onChange always propagates to the header
  // dot), then guards the modal's OWN setState on `alive` — the dialog may
  // have been closed mid-action (#12).
  const onDeploy = async (forceNew = false) => {
    if (!env) return;
    setBusy("deploy");
    setActionError(null);
    setClearCacheResult(null); // a stale "N cleared" note must not survive a redeploy
    try {
      // A chosen name only rides along on the "named" path (and only for a
      // fresh create — see confirmedRedeployToken); Deploy is disabled while
      // that name is missing/malformed, so trimmedToken is a valid name here.
      const record = await deployPage(
        fsPath,
        env.name,
        include,
        exclude,
        cacheMaxAge,
        forceNew,
        namedTokenActive ? trimmedToken : undefined,
      );
      applyDeployment(record);
      if (!alive.current) return;
      setReconciled(true);
      setLive("active");
      // The change-link flow is done once the new mount is live — drop back to
      // the read-only summary (now reflecting the just-minted token).
      setChangingLink(false);
      // Re-seed from what was actually persisted, so the list reflects the
      // stored record (e.g. server-side dedup) rather than the raw local lists.
      setInclude(record.include ?? include);
      setExclude(record.exclude ?? exclude);
      setCacheMaxAge(record.cache_max_age ?? cacheMaxAge);
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

  // Forces cached results to be recomputed on next request, without touching the
  // mount's status/URL/caching setting (deploy.py's clear_cache_deployment) — for
  // "I changed the underlying data, not the code" (a redeploy dedupes to the same
  // content-address and would otherwise keep serving the old cached result until
  // cache_max_age expires).
  const onClearCache = async () => {
    setBusy("clear-cache");
    setActionError(null);
    setClearCacheResult(null);
    try {
      const result = await clearCacheDeployment(fsPath);
      if (alive.current) setClearCacheResult(result);
    } catch (e) {
      if (alive.current) setActionError((e as Error).message);
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

  // Enter/leave the change-link flow (see `changingLink`). Entering seeds the
  // picker from the current mount so the user starts where they are; leaving
  // restores the default (a fresh create's default is an unguessable link).
  const enterChangeLink = () => {
    setChangingLink(true);
    setLinkOpen(true);
    if (deployment?.named) {
      setTokenMode("named");
      setCustomToken(deployment.token);
    } else {
      setTokenMode("random");
      setCustomToken("");
    }
  };
  const cancelChangeLink = () => {
    setChangingLink(false);
    setTokenMode("random");
    setCustomToken("");
  };

  // Deploy/Redeploy: the button label stays stable ("Deploy" / "Redeploy" /
  // "Deploying…"); the URL nuance (same link, restored link, fresh link,
  // unconfirmed) moves to a single status line below, driven by `live` — the
  // mount's VERIFIED `share list` classification. The change-link flow is the
  // one case that overrides both — it force_news a new URL and takes the old
  // one down, so it says so rather than "keeps the same URL".
  const deployLabel =
    busy === "deploy"
      ? "Deploying…"
      : changingLink
        ? "Deploy new link"
        : isRedeploy
          ? "Redeploy"
          : "Deploy";
  const deployStatus = changingLink
    ? "Deploying mints a new link and takes the current one down."
    : !samePointerEnv
      ? null
      : live === "active"
        ? "Redeploying keeps the same URL."
        : live === "revoked"
          ? "Redeploying restores the previous URL."
          : mountAbsent
            ? "The recorded deployment no longer exists — deploying mints a new URL."
            : "Environment unreachable — the current deployment couldn't be confirmed.";

  // The Link section's collapsed one-liner (like Caching's "off"/"on, 5m"):
  // the pending choice while picking, else the live mount's current setting.
  const linkSummary = pickingLink
    ? tokenMode === "named"
      ? trimmedToken
        ? `custom: ${trimmedToken}`
        : "custom name"
      : "unguessable"
    : deployment?.named
      ? `custom: ${deployment.token}`
      : "unguessable";

  const body = () => {
    if (loadError) {
      return (
        <>
          <ErrorBanner>{loadError}</ErrorBanner>
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
              className="btn btn-primary"
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
          {actionError && <ErrorBanner>{actionError}</ErrorBanner>}
        </div>
      );
    }

    if (envs.length === 0) {
      // The setup flow itself lives on the Fused account tab (M18b) — this
      // block routes there, handling the sign-in prerequisite in place.
      return (
        <div className="deploy-section">
          <p>
            No hosted environments are configured — deploying needs a managed{" "}
            <code>fused</code> environment or an <code>aws</code> environment with a
            provisioned serving plane.
          </p>
          {!config.fused_logged_in ? (
            <>
              <p className="deploy-muted">
                Setting up the managed environment starts with a one-time browser sign-in
                to Fused.
              </p>
              {signin.connecting ? (
                <div className="deploy-form-row">
                  <span className="deploy-muted">
                    Waiting for the browser sign-in… finish signing in in the tab that just
                    opened.
                  </span>
                  <button type="button" onClick={() => void signin.cancel()}>
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void signin.begin()}
                >
                  Sign in to Fused
                </button>
              )}
              {signin.error && <ErrorBanner>{signin.error}</ErrorBanner>}
            </>
          ) : (
            <div className="deploy-form-row">
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => navigateUrl("/view/_prefs?tab=account")}
              >
                Set up hosted environment
              </button>
              <span className="deploy-muted">opens the Fused account tab in Preferences</span>
            </div>
          )}
          {/* Unconditional: an AWS-only user who is signed out must still be
              told how to create their env without an irrelevant managed-cloud
              sign-in. */}
          <p className="deploy-muted">
            Self-hosted AWS environments are created in a terminal with{" "}
            <code>{config.setup_cli} env create</code>. Environments are read from{" "}
            <code>{config.envs_file}</code>.
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

        {/* Owner-only diagnostics for this deployed page: the recent captured
            failures behind its opaque 500s (fused share errors). Collapsed by
            default — DeploymentErrors is only mounted (and so only fetches)
            once opened, mirroring the account Deployments list's per-row
            toggle; viewers of the page never see any of this either way. The
            section always renders — for an as-yet-undeployed ("unshared") page
            it shows disabled, with a hint, so the modal's chrome is consistent
            rather than a control that pops into existence on first deploy. */}
        {(() => {
          const hasDeployment = !!(deployment?.env && deployment?.token);
          return (
            <div className="deploy-files">
              <button
                type="button"
                className="deploy-files-head"
                aria-expanded={hasDeployment ? errorsOpen : undefined}
                disabled={!hasDeployment}
                onClick={() => setErrorsOpen((o) => !o)}
                title={hasDeployment ? undefined : "Available once this page is deployed"}
              >
                <span className="deploy-files-chevron" aria-hidden="true">
                  {hasDeployment && errorsOpen ? "▾" : "▸"}
                </span>
                <span className="deploy-files-title">Recent errors</span>
                {!hasDeployment && (
                  <span className="deploy-files-count">available after you deploy</span>
                )}
              </button>
              {hasDeployment && errorsOpen && (
                <DeploymentErrors env={deployment!.env} token={deployment!.token} />
              )}
            </div>
          );
        })()}

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

        <div className="deploy-files">
          <button
            type="button"
            className="deploy-files-head"
            aria-expanded={cachingOpen}
            onClick={() => setCachingOpen((o) => !o)}
          >
            <span className="deploy-files-chevron" aria-hidden="true">
              {cachingOpen ? "▾" : "▸"}
            </span>
            <span className="deploy-files-title">Caching</span>
            <span className="deploy-files-count">
              {cacheMaxAge === "0s" ? "off" : `on, ${cacheMaxAge}`}
            </span>
          </button>
          {cachingOpen && (
            <div className="deploy-files-body">
              <div className="deploy-form-row">
                <label className="deploy-cache-toggle">
                  <input
                    type="checkbox"
                    checked={cacheMaxAge !== "0s"}
                    disabled={busy !== null}
                    onChange={(e) =>
                      setCacheMaxAge(e.target.checked ? DEFAULT_CACHE_DURATION : "0s")
                    }
                  />
                  Cache page results
                </label>
                {cacheMaxAge !== "0s" && (
                  <Select
                    aria-label="Cache duration"
                    value={cacheMaxAge}
                    onChange={(e) => setCacheMaxAge(e.target.value)}
                    disabled={busy !== null}
                  >
                    {cacheDurationOptions(cacheMaxAge).map((d) => (
                      <option key={d.value} value={d.value}>
                        for {d.label}
                      </option>
                    ))}
                  </Select>
                )}
                {deployment?.status === "active" && (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={onClearCache}
                    disabled={busy !== null}
                    title="Force cached results to be recomputed on the next request, without redeploying or changing the URL"
                  >
                    {busy === "clear-cache" ? "Clearing cache…" : "Clear cache"}
                  </button>
                )}
              </div>
            </div>
          )}
          {clearCacheResult && (
            <div className="deploy-muted deploy-files-body">
              {clearCacheResult.deleted > 0
                ? `Cleared ${clearCacheResult.deleted} cached result${clearCacheResult.deleted === 1 ? "" : "s"} — the next request recomputes.`
                : "Nothing was cached — nothing to clear."}
            </div>
          )}
        </div>
        {/* Link section (collapsible, like Caching). Two body modes:
              - picking (a fresh create, or an explicit "Change link" on a live
                mount): the random-vs-named radios — an explicit choice, not a
                "blank = random" field, since naming a public link is a
                deliberate, security-relevant act;
              - confirmed live mount: a read-only summary of the current link
                plus a "Change link" action, because repoint/recreate keep the
                token — only a force_new deploy (what Change link triggers) can
                actually change the URL.
            The public/no-auth note lives BELOW this block (always visible, even
            when collapsed), since the radios/summary describe guessability but
            not that the link is unauthenticated. */}
        <div className="deploy-files">
          <button
            type="button"
            className="deploy-files-head"
            aria-expanded={linkOpen}
            onClick={() => setLinkOpen((o) => !o)}
          >
            <span className="deploy-files-chevron" aria-hidden="true">
              {linkOpen ? "▾" : "▸"}
            </span>
            <span className="deploy-files-title">Link</span>
            <span className="deploy-files-count">{linkSummary}</span>
          </button>
          {linkOpen && (
            <div className="deploy-files-body">
              {pickingLink ? (
                <>
                  <fieldset className="deploy-token-modes">
                    <legend>{changingLink ? "New link" : "Link"}</legend>
                    <label className="deploy-token-mode">
                      <input
                        type="radio"
                        name="deploy-token-mode"
                        checked={tokenMode === "random"}
                        disabled={busy !== null}
                        onChange={() => setTokenMode("random")}
                      />
                      <span>
                        <b>Unguessable link</b>
                        <span className="deploy-muted">
                          {" "}
                          — a random URL nobody can guess (default)
                        </span>
                      </span>
                    </label>
                    <label className="deploy-token-mode">
                      <input
                        type="radio"
                        name="deploy-token-mode"
                        checked={tokenMode === "named"}
                        disabled={busy !== null}
                        onChange={() => setTokenMode("named")}
                      />
                      <span>
                        <b>Custom name</b>
                        <span className="deploy-muted">
                          {" "}
                          — you pick the URL; anyone who knows or guesses it can open it
                        </span>
                      </span>
                    </label>
                  </fieldset>
                  {namedTokenActive && (
                    <div className="deploy-form-row">
                      <label htmlFor="deploy-token-input">Name</label>
                      <TextInput
                        id="deploy-token-input"
                        type="text"
                        placeholder="my-dashboard"
                        value={customToken}
                        disabled={busy !== null}
                        autoFocus
                        onChange={(e) => setCustomToken(e.target.value)}
                      />
                    </div>
                  )}
                  {namedTokenActive && (tokenFormatError || tokenIncomplete || tokenUnchanged) && (
                    <div
                      className={
                        "deploy-token-hint deploy-muted" + (tokenFormatError ? " err" : "")
                      }
                    >
                      {tokenFormatError ??
                        (tokenUnchanged
                          ? "That's already this link's name — pick a different one to change it, or Cancel."
                          : "Enter a name for the link, or switch to an unguessable link.")}
                    </div>
                  )}
                  {changingLink && (
                    <div className="deploy-form-row">
                      <button type="button" onClick={cancelChangeLink} disabled={busy !== null}>
                        Cancel
                      </button>
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className="deploy-muted">
                    {deployment?.named ? (
                      <>
                        This link uses a <b>custom name</b> you chose.
                      </>
                    ) : (
                      <>
                        This link is an <b>unguessable</b> random URL.
                      </>
                    )}
                  </div>
                  <div className="deploy-form-row">
                    <button
                      type="button"
                      className="btn btn-secondary"
                      onClick={enterChangeLink}
                      disabled={busy !== null}
                      title="Mint a new link (the current one is taken down)"
                    >
                      Change link…
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
        <div className="deploy-muted deploy-about">
          Deploys are <b>public</b> — no sign-in required, so anyone with the link can open it,
          whether it's the unguessable default or a name you chose.
        </div>
        <div className="deploy-form-row">
          <label htmlFor="deploy-env-select">Deploy to</label>
          <Select
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
          </Select>
          <button
            type="button"
            className="btn btn-primary"
            // In the change-link flow this is the force_new deploy (mint a new
            // token, revoke the old); otherwise a normal create/redeploy.
            onClick={() => onDeploy(changingLink)}
            // Hold Deploy until the shown "Files to publish" list matches the current
            // selection: preview === null (still resolving, or cleared on a page
            // switch) or previewPending (a refresh is in flight after an edit) both
            // mean the list on screen may not reflect what a click would ship, so
            // we don't deploy blind; preview.errors are hard export blockers.
            disabled={
              busy !== null ||
              env === null ||
              preview === null ||
              previewPending ||
              preview.errors.length > 0 ||
              tokenBlocksDeploy
            }
            title={
              preview === null || previewPending
                ? "Preparing the publish preview…"
                : preview.errors.length > 0
                  ? "Fix the export problems listed above first"
                  : tokenFormatError
                    ? tokenFormatError
                    : tokenIncomplete
                      ? "Enter a name for the link, or switch to an unguessable link"
                      : tokenUnchanged
                        ? "Pick a different name to change the link, or Cancel"
                        : undefined
            }
          >
            {busy === "deploy" && <span className="deploy-spinner" />}
            {deployLabel}
          </button>
          {deployment?.status === "active" && (
            <button
              type="button"
              className="btn btn-danger"
              onClick={onRevoke}
              disabled={busy !== null}
              title="Take the URL down (the link stops working until you deploy again)"
            >
              {busy === "revoke" ? "Revoking…" : "Revoke"}
            </button>
          )}
        </div>
        {/* One status line for the same-env case — the URL nuance that used to
            live in the button label / a stack of notes. */}
        {deployStatus && <div className="deploy-muted">{deployStatus}</div>}
        {/* One context-derived note for the cross-env cases (recorded env still
            configured vs. removed) — mutually exclusive, collapsed into one. */}
        {deployment && selectedEnv !== null && deployment.env !== selectedEnv && (
          <div className="deploy-note">
            {envs.some((e) => e.name === deployment.env) ? (
              <>
                This page is already deployed on <b>{deployment.env}</b> — deploying to{" "}
                <b>{selectedEnv}</b> mints an independent new link and this dialog will track that
                one instead (the old mount stays live until revoked from the CLI).
              </>
            ) : (
              <>
                This page was deployed to <b>{deployment.env}</b>, which is no longer a configured
                environment. Deploying here starts a new mount on <b>{selectedEnv}</b>; the old one
                is unmanaged from this dialog.
              </>
            )}
          </div>
        )}
        {env?.backend === "fused" && !config.fused_logged_in && (
          <div className="deploy-note">
            <div>
              You aren't signed in to Fused — deploying to <b>{env.name}</b> needs a one-time
              browser sign-in.
            </div>
            {signin.connecting ? (
              <div className="deploy-form-row">
                <span className="deploy-muted">
                  Waiting for the browser sign-in… finish signing in in the tab that just opened.
                </span>
                <button type="button" onClick={() => void signin.cancel()}>
                  Cancel
                </button>
              </div>
            ) : (
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void signin.begin()}
                disabled={busy !== null}
              >
                Sign in to Fused
              </button>
            )}
            {signin.error && <ErrorBanner>{signin.error}</ErrorBanner>}
          </div>
        )}
        {/* The old "About public links" disclosure was removed — its content
            now lives inline beneath the Link section above (always visible),
            so the bottom-of-modal toggle is redundant. */}
        {actionError && <ErrorBanner>{actionError}</ErrorBanner>}
      </>
    );
  };

  return (
    // busy is intentionally NOT passed to the Modal's close gate: the dialog
    // stays closeable mid-action (#12) — the action continues server-side and
    // onChange keeps the header dot correct, so a slow/hung CLI child can never
    // trap the user. closeTitle reflects that.
    <Modal
      title={`Deploy ${basename(fsPath)}`}
      onClose={onClose}
      closeTitle={busy !== null ? "Close (the action keeps running)" : "Close"}
    >
      {body()}
    </Modal>
  );
}
