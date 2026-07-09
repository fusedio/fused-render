// File preview. Dispatch is exactly two-way (ARCHITECTURE §6):
//   1. stat.templates non-empty -> render active mode in iframe (+_file on iframe URL)
//   2. else                      -> fallback metadata card
// No file-type checks live in the shell — html arrives through stat.templates
// like everything else, via the "_render" sentinel (SPEC PT-12).
import { useEffect, useRef, useState, type ReactNode } from "react";
import { getDeployStatus, getPrefs, rawUrl } from "../lib/api";
import type { Deployment, StatResult, TemplateEntry } from "../lib/api";
import { formatSize, formatMtime } from "../lib/format";
import { navigateUrl } from "../lib/router";
import ModeSwitcher, { templateModeIcon } from "../components/ModeSwitcher";
import DeployModal from "../components/DeployModal";

// Directory previews (a `.zarr` store maps to a directory template, D65) keep
// a way into the raw members: navigate to the same path with `?listing=1`,
// which App's dispatch honors to force the plain listing view. The pathname
// (which already carries the /view/ or /embed/ prefix) is preserved.
function browseContents() {
  navigateUrl(location.pathname + "?listing=1");
}

interface HeaderProps {
  fsPath: string;
  stat: StatResult;
  children?: ReactNode;
}

function Header({ fsPath, stat, children }: HeaderProps) {
  return (
    <div className="preview-header">
      <h1 title={fsPath}>{stat.name}</h1>
      <div className="preview-actions">{children}</div>
    </div>
  );
}

// `_mode` (shell URL) selects among stat.templates by name (SPEC PT-9): absent
// or unknown/stale value falls back to the default (templates[0]) silently.
function activeTemplate(templates: TemplateEntry[]): TemplateEntry {
  const requested = new URLSearchParams(location.search).get("_mode");
  return templates.find((t) => t.mode === requested) || templates[0];
}

// --- Deploy button (SPEC §19) -----------------------------------------------
// Header action for deployable pages: any file whose mode list carries the
// "_render" sentinel (i.e. a renderable page — the exact set /api/export
// accepts). Shows a live dot when the local deployment pointer reads active;
// the pointer is a cheap local read (no CLI shell-out) — the modal is what
// reconciles against `share list`. A user who rebinds .html away from
// "_render" loses the button too, consistently with losing the rendered view.

const DEPLOY_ICON = (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 19V5" />
    <path d="M5 12l7-7 7 7" />
  </svg>
);

function DeployButton({ fsPath }: { fsPath: string }) {
  const [open, setOpen] = useState(false);
  const [deployment, setDeployment] = useState<Deployment | null>(null);

  // Local pointer only (reconcile=false): opening a preview must never spawn
  // the fused CLI. Errors are ignored — the button then just shows no dot.
  // The pointer can change without this view remounting — a revoke from the
  // Preferences page in ANOTHER tab, or any out-of-band /api/deploy/revoke
  // (same-tab navigation remounts the view via the nav epoch, so it needs no
  // handling). Re-read on focus/visibility regain: a cheap local JSON read,
  // the bookmarks-poll freshness posture (D77) without a timer.
  useEffect(() => {
    let alive = true;
    const refresh = () => {
      getDeployStatus(fsPath, false)
        .then((r) => {
          if (alive) setDeployment(r.deployment);
        })
        .catch(() => {});
    };
    refresh();
    const onVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [fsPath]);

  const live = deployment?.status === "active";
  return (
    <>
      <button
        type="button"
        className={"deploy-btn" + (live ? " live" : "")}
        title={live ? "Deployed — open the Deploy dialog to manage" : "Deploy this page to a hosted URL"}
        onClick={() => setOpen(true)}
      >
        {DEPLOY_ICON}
        Deploy
        {live && <span className="deploy-live-dot" />}
      </button>
      {open && (
        <DeployModal fsPath={fsPath} onClose={() => setOpen(false)} onChange={setDeployment} />
      )}
    </>
  );
}

// Whether the Deploy affordance is enabled (Preferences → Deployments; SPEC
// §20). Deploy is opt-in, so the button stays hidden until the pref reads on —
// default false while loading means it never flashes on for a user who left it
// off. Re-read on focus/visibility so toggling it in the Preferences tab shows
// through without a reload (same cheap-local-read posture as the deploy dot).
function useDeployEnabled(): boolean {
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let alive = true;
    const refresh = () => {
      getPrefs()
        .then((p) => {
          if (alive) setEnabled(p.deploy.enabled);
        })
        .catch(() => {});
    };
    refresh();
    const onVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  return enabled;
}

function TemplatePreview({ fsPath, stat, templates }: { fsPath: string; stat: StatResult; templates: TemplateEntry[] }) {
  // Caller only renders this when `templates` (already sentinel-filtered by
  // Preview's dispatch, SPEC PT-12) is non-empty.
  const [mode, setModeState] = useState<string>(() => activeTemplate(templates).mode);
  const entry = templates.find((t) => t.mode === mode) || templates[0];
  const deployEnabled = useDeployEnabled();

  // One switch at a time: the flush below is async, and a second click landing
  // mid-flight could resolve in either order, desyncing iframe key / local
  // state / shell `_mode`. Clicks during a pending switch are dropped.
  const switching = useRef(false);
  const setMode = async (next: string) => {
    if (next === mode || switching.current) return;
    switching.current = true;
    try {
      await doSetMode(next);
    } finally {
      switching.current = false;
    }
  };

  const doSetMode = async (next: string) => {
    // The flush below is async: if the user navigates to ANOTHER file while
    // it's in flight, writing `_mode` against the then-current location would
    // stamp the switch onto the wrong file's URL. Capture where the switch
    // started and abort if the location moved.
    const startedAt = location.pathname;
    // Switching modes REMOUNTS the preview iframe (React key change) — an
    // editor buffer with edits newer than the last autosave would be silently
    // discarded. Same-origin, so ask the iframe to flush first (the code
    // template exposes __fusedFlushEdits); refuse the switch when the buffer
    // can't be made safe (save failure / unresolved conflict — the template's
    // own banner explains). The 10s bound only catches a truly hung write so
    // the switcher can't wedge forever; timing out aborts the switch, never
    // the save.
    const frame = document.querySelector<HTMLIFrameElement>(".preview-body iframe");
    const flush = frame?.contentWindow && (frame.contentWindow as any).__fusedFlushEdits;
    if (typeof flush === "function") {
      try {
        const res = await Promise.race([
          flush(),
          new Promise((r) => setTimeout(() => r({ ok: false }), 10000)),
        ]);
        if (res && (res as { ok: boolean }).ok === false) return;
      } catch {
        return;
      }
    }
    if (location.pathname !== startedAt) return; // navigated away mid-flush
    const params = new URLSearchParams(location.search);
    // Selecting the default mode DELETES _mode (clean URLs); any other mode sets it.
    if (next === templates[0].mode) params.delete("_mode");
    else params.set("_mode", next);
    const search = params.toString();
    history.replaceState(null, "", location.pathname + (search ? "?" + search : ""));
    setModeState(next);
  };

  // "_render" sentinel (PT-12): render the target file itself, no _file param.
  // Ordinary entries: target file rides on the iframe's own URL as _file —
  // the shell URL's pathname already names the file, so no duplication there.
  const src =
    entry.mode === "_render"
      ? `/render?path=${encodeURIComponent(fsPath)}`
      : `/render?path=${encodeURIComponent(entry.path as string)}&_file=${encodeURIComponent(fsPath)}`;

  return (
    <>
      <Header fsPath={fsPath} stat={stat}>
        {/* Directory template (e.g. a .zarr store): a "Browse contents" action
            drops into the raw member listing (D65). */}
        {stat.is_dir && (
          <button type="button" onClick={browseContents}>
            Browse contents
          </button>
        )}
        {/* Deployable = the mode list carries the "_render" sentinel AND the
            file is .html/.htm — the exporter's actual contract. The extension
            check matters because a registry rebind can put "_render" on any
            type (D73), but /api/export and /api/deploy/preview accept only
            .html/.htm — the button must not open a modal that can't deploy.
            Directories never deploy (no _render binding exists for one today;
            the guard keeps that true even if a registry ever says otherwise).
            Gated on the opt-in Deploy pref (Preferences → Deployments): hidden
            entirely unless the user has turned Deploy on. */}
        {!stat.is_dir &&
          deployEnabled &&
          templates.some((t) => t.mode === "_render") &&
          /\.html?$/i.test(fsPath) && <DeployButton fsPath={fsPath} />}
        <ModeSwitcher
          entries={templates.map((t) => ({ mode: t.mode, icon: templateModeIcon(t) }))}
          active={entry.mode}
          onSelect={setMode}
        />
      </Header>
      <div className="preview-body">
        {/* key: switching mode replaces the iframe (fresh document per switch). */}
        <iframe key={mode} src={src} />
        {/* Embed mode hides the whole preview-header (shell.css), so a directory
            template also surfaces "Browse contents" as a corner chip pinned over
            the iframe — CSS reveals it only in embed. File previews render no
            chip, so embed chrome stays empty for them. */}
        {stat.is_dir && (
          <button type="button" className="preview-browse-chip" onClick={browseContents}>
            Browse contents
          </button>
        )}
      </div>
    </>
  );
}

function FallbackPreview({ fsPath, stat }: { fsPath: string; stat: StatResult }) {
  return (
    <>
      <Header fsPath={fsPath} stat={stat} />
      <div className="preview-body">
        <div className="metadata-card">
          <dl>
            <dt>Name</dt>
            <dd>{stat.name}</dd>
            <dt>Path</dt>
            <dd>{fsPath}</dd>
            <dt>Size</dt>
            <dd>{formatSize(stat.size)}</dd>
            <dt>Modified</dt>
            <dd>{formatMtime(stat.mtime)}</dd>
          </dl>
          <a href={rawUrl(fsPath)} download={stat.name}>
            Download
          </a>
        </div>
      </div>
    </>
  );
}

interface PreviewProps {
  fsPath: string;
  stat: StatResult;
}

export default function Preview({ fsPath, stat }: PreviewProps) {
  // Defensive filter (SPEC PT-12): an entry with path===null whose mode isn't
  // a recognized sentinel is dropped — only "_render" exists today. Filtering
  // here keeps the non-empty dispatch check honest (an all-unknown list falls
  // back instead of crashing TemplatePreview).
  const templates = stat.templates.filter((t) => t.path !== null || t.mode === "_render");
  if (templates.length > 0) return <TemplatePreview fsPath={fsPath} stat={stat} templates={templates} />;
  return <FallbackPreview fsPath={fsPath} stat={stat} />;
}
