// File preview. Dispatch is exactly two-way (ARCHITECTURE §6):
//   1. stat.templates non-empty -> render active mode in iframe (+_file on iframe URL)
//   2. else                      -> fallback metadata card
// No file-type checks live in the shell — html arrives through stat.templates
// like everything else, via the "_render" sentinel (SPEC PT-12).
import { useEffect, useRef, useState, type ReactNode } from "react";
import { getDeployStatus, getPrefs, rawUrl, resolveConditions } from "../lib/api";
import type { Deployment, StatResult, TemplateEntry } from "../lib/api";
import { formatSize, formatMtime } from "../lib/format";
import ModeSwitcher, { templateModeIcon, modeTitle, KNOWN_SENTINEL_MODES } from "../components/ModeSwitcher";
import DeployModal from "../components/DeployModal";
import Listing from "./Listing";

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
// or unknown/stale value falls back to the default silently. The default is
// the first UNCONDITIONAL entry (CT-12: a gated template is never the default
// while a normal one exists) — only an all-conditional list falls back to its
// first (by then verdict-allowed) entry.
function defaultTemplate(templates: TemplateEntry[]): TemplateEntry {
  return templates.find((t) => !t.conditional) || templates[0];
}

function activeTemplate(templates: TemplateEntry[]): TemplateEntry {
  const requested = new URLSearchParams(location.search).get("_mode");
  return templates.find((t) => t.mode === requested) || defaultTemplate(templates);
}

// Deferred condition.py verdicts (CT-12). Stat only MARKS gated templates
// (`conditional: true`) so it stays fast on remote mounts; the actual gates
// run here, in the background, while the first unconditional template is
// already rendering. Returns null while resolving, then {mode: allowed}.
// A failed request resolves to {} — every gated entry then reads as denied,
// the same fail-closed posture as a broken gate server-side.
function useConditions(fsPath: string, templates: TemplateEntry[]): Record<string, boolean> | null {
  const anyConditional = templates.some((t) => t.conditional);
  const [verdicts, setVerdicts] = useState<Record<string, boolean> | null>(anyConditional ? null : {});
  useEffect(() => {
    if (!anyConditional) {
      setVerdicts({});
      return;
    }
    let alive = true;
    setVerdicts(null);
    resolveConditions(fsPath)
      .then((r) => {
        if (alive) setVerdicts(r.conditions);
      })
      .catch(() => {
        if (alive) setVerdicts({});
      });
    return () => {
      alive = false;
    };
  }, [fsPath, anyConditional]);
  return verdicts;
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

function TemplatePreview({
  fsPath,
  stat,
  templates,
  conditions,
}: {
  fsPath: string;
  stat: StatResult;
  templates: TemplateEntry[];
  conditions: Record<string, boolean> | null;
}) {
  // Caller only renders this when `templates` (already sentinel-filtered by
  // Preview's dispatch, SPEC PT-12) is non-empty. Entries whose condition.py
  // verdict is still in flight (CT-12) are present but PENDING — shown in the
  // switcher as a disabled spinner, not selectable, never the default.
  const isPending = (t: TemplateEntry) => !!t.conditional && conditions === null;
  const defaultEntry = defaultTemplate(templates);
  const [mode, setModeState] = useState<string>(() => activeTemplate(templates).mode);
  const entry = templates.find((t) => t.mode === mode) || defaultEntry;
  // A verdict landing can DROP the current mode (URL-requested conditional
  // that resolved false): fall back to the default, same silent posture as an
  // unknown `_mode`.
  useEffect(() => {
    if (!templates.some((t) => t.mode === mode)) setModeState(defaultEntry.mode);
  }, [templates, mode, defaultEntry.mode]);
  const deployEnabled = useDeployEnabled();
  // `_listing` sentinel (D81): the shell's built-in directory listing, mounted
  // in place of the preview iframe — no iframe, no `_file`. Every directory
  // renders through this same header + body chrome (even a plain folder's
  // single `_listing` mode), so the preview header is uniform across files and
  // dirs.
  const isListing = entry.mode === "_listing";

  // One switch at a time: the flush below is async, and a second click landing
  // mid-flight could resolve in either order, desyncing iframe key / local
  // state / shell `_mode`. Clicks during a pending switch are dropped.
  const switching = useRef(false);
  const setMode = async (next: string) => {
    if (next === mode || switching.current) return;
    // Unresolved gate: not selectable (the switcher disables it too).
    const target = templates.find((t) => t.mode === next);
    if (target && isPending(target)) return;
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
    if (next === defaultEntry.mode) params.delete("_mode");
    else params.set("_mode", next);
    const search = params.toString();
    history.replaceState(null, "", location.pathname + (search ? "?" + search : ""));
    setModeState(next);
  };

  // "_render" sentinel (PT-12): render the target file itself, no _file param.
  // Ordinary entries: target file rides on the iframe's own URL as _file —
  // the shell URL's pathname already names the file, so no duplication there.
  // `_remote=1` forwards stat's remote flag (bytes come from a mount) so a
  // page can prefer ranged HTTP reads (/api/fs/raw) over local file I/O.
  // `_listing` builds no src — it renders a shell component, not an iframe.
  const remote = stat.remote ? "&_remote=1" : "";
  const src = isListing
    ? null
    : entry.mode === "_render"
      ? `/render?path=${encodeURIComponent(fsPath)}`
      : `/render?path=${encodeURIComponent(entry.path as string)}&_file=${encodeURIComponent(fsPath)}${remote}`;

  // Embed hides the whole preview-header, hence the switcher (shell.css). A
  // directory whose mode list carries `_listing` alongside another mode (a
  // .zarr store, or a custom view + listing) surfaces a corner chip to toggle
  // between the listing and that other view (D81 — replaces the old
  // `?listing=1` "Browse contents"). The listing's counterpart is the default
  // mode, UNLESS the default IS the listing (`["_listing", "gallery"]`) — then
  // the first non-listing mode, so an embed whose default is the listing still
  // has a path to the secondary view. Shown only when a non-listing mode exists.
  const otherEntry = templates.find((t) => t.mode !== "_listing");
  const counterpart = defaultEntry.mode !== "_listing" ? defaultEntry.mode : otherEntry?.mode;
  const toggleListing =
    otherEntry && templates.some((t) => t.mode === "_listing")
      ? () => setMode(isListing ? (counterpart as string) : "_listing")
      : null;

  return (
    <>
      <Header fsPath={fsPath} stat={stat}>
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
          entries={templates.map((t) => ({ mode: t.mode, icon: templateModeIcon(t), pending: isPending(t) }))}
          active={entry.mode}
          onSelect={setMode}
        />
      </Header>
      <div className="preview-body">
        {isPending(entry) ? (
          /* URL-requested a gated mode whose verdict is still in flight: hold
             the body until it lands (the iframe must not render a template on
             a file its gate may deny). */
          <div className="preview-resolving">
            <span className="mode-icon-spinner" />
            Checking if this view applies…
          </div>
        ) : isListing ? (
          <Listing fsPath={fsPath} />
        ) : (
          /* key: switching mode replaces the iframe (fresh document per switch). */
          <iframe key={mode} src={src as string} />
        )}
        {toggleListing && (
          <button type="button" className="preview-browse-chip" onClick={toggleListing}>
            {!isListing
              ? "Browse contents"
              : counterpart === defaultEntry.mode
                ? "Back"
                : modeTitle(counterpart as string)}
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
  // a recognized sentinel (`_render`, `_listing`) is dropped. Filtering here
  // keeps the non-empty dispatch check honest (an all-unknown list falls back
  // instead of crashing TemplatePreview).
  const templates = stat.templates.filter((t) => t.path !== null || KNOWN_SENTINEL_MODES.has(t.mode));
  // Deferred gates (CT-12): resolve condition.py verdicts in the background.
  // The first unconditional template renders immediately — only an
  // ALL-conditional list has nothing safe to show and waits here.
  const conditions = useConditions(fsPath, templates);
  const resolving = conditions === null;
  // While resolving, gated entries stay visible (as pending); once verdicts
  // land, denied ones drop.
  const visible = templates.filter((t) => !t.conditional || resolving || conditions[t.mode] === true);
  if (resolving && templates.length > 0 && templates.every((t) => t.conditional)) {
    return (
      <>
        <Header fsPath={fsPath} stat={stat} />
        <div className="preview-body">
          <div className="preview-resolving">
            <span className="mode-icon-spinner" />
            Checking which views apply…
          </div>
        </div>
      </>
    );
  }
  if (visible.length > 0)
    return <TemplatePreview fsPath={fsPath} stat={stat} templates={visible} conditions={conditions} />;
  return <FallbackPreview fsPath={fsPath} stat={stat} />;
}
