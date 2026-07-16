// File preview. Dispatch is exactly two-way (ARCHITECTURE §6):
//   1. stat.templates non-empty -> render active mode in iframe (+_file on iframe URL)
//   2. else                      -> fallback metadata card
// No file-type checks live in the shell — html arrives through stat.templates
// like everything else, via the "_render" sentinel (SPEC PT-12).
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import {
  getDeployStatus,
  getPrefs,
  rawUrl,
  resolveConditions,
  renameEntry,
  copyEntry,
  revealPath,
  deleteEntry,
} from "../lib/api";
import type { Deployment, StatResult, TemplateEntry } from "../lib/api";
import { navigate, navigateUrl, urlForFsPath } from "../lib/router";
import { formatSize, formatMtime, basename } from "../lib/format";
import { useRefreshOnReturn } from "../lib/hooks";
import {
  dirname,
  join,
  freeDuplicatePath,
  copyToClipboard,
  clearClipboardIfDeleted,
  remapClipboardPath,
  trashEntry,
  buildOpenWithItems,
  friendlyFsError,
} from "../lib/fs-actions";
import { acquireOverlay, releaseOverlay } from "../lib/ui-overlay";
import { setClipboard } from "../lib/fs-clipboard";
import ModeSwitcher, { templateModeIcon, modeTitle, KNOWN_SENTINEL_MODES } from "../components/ModeSwitcher";
import ContextMenu, { type MenuEntry, type MenuItem } from "../components/ContextMenu";
import { MenuIcons } from "../components/MenuIcons";
import { PromptDialog, ConfirmDialog, nameError } from "../components/FsDialogs";
import Toast, { type ToastTone } from "../components/Toast";
import DeployModal from "../components/DeployModal";
import Listing from "./Listing";

interface HeaderProps {
  fsPath: string;
  stat: StatResult;
  children?: ReactNode;
  // Right-click on the header chrome opens the file context menu for the open
  // file (views hosting a real preview wire this; transient resolving/loading
  // headers leave it undefined).
  onContextMenu?: (e: React.MouseEvent) => void;
}

function Header({ fsPath, stat, children, onContextMenu }: HeaderProps) {
  return (
    <div className="preview-header" onContextMenu={onContextMenu}>
      <h1 title={fsPath}>{stat.name}</h1>
      <div className="preview-actions">{children}</div>
    </div>
  );
}

// One open modal for the preview file menu: a Rename prompt or a Delete confirm
// (the trash-unsupported fallback). Mirrors Listing's DialogState, kept local
// so the two views don't couple through a shared dialog type.
type PreviewDialog =
  | { kind: "prompt"; title: string; initial: string; confirmLabel: string; selectStem?: boolean; onConfirm: (value: string) => void }
  | { kind: "confirm"; title: string; message: ReactNode; confirmLabel: string; danger?: boolean; onConfirm: () => void };

// The file context menu for the CURRENTLY OPEN preview file. Owns its own
// menu/dialog/toast state and, unlike Listing (which refetches + re-anchors its
// selection), reacts to mutations by NAVIGATING: a rename moves to the renamed
// path (preserving the current query, i.e. `_mode`/params), a trash/delete
// moves to the parent folder listing — so neither leaves a dead URL. Action
// bodies come from lib/fs-actions, shared with Listing. `loadOpenWith` is
// supplied by the caller since the two preview variants resolve modes
// differently (TemplatePreview already knows its templates; FallbackPreview
// re-stats).
function usePreviewFileMenu(
  fsPath: string,
  stat: StatResult,
  loadOpenWith: () => Promise<MenuItem[]>
) {
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuEntry[] } | null>(null);
  const [dialog, setDialog] = useState<PreviewDialog | null>(null);
  const [toast, setToast] = useState<{ msg: string; tone: ToastTone } | null>(null);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(t);
  }, [toast]);

  // Publish this header menu's overlay state to the shared registry (lib/
  // ui-overlay). A directory opened in Preview embeds a Listing whose own
  // document-level keyboard handlers would otherwise fire (Cmd+Backspace,
  // Cmd+X, …) on a row behind this preview menu/dialog — the embedded Listing
  // can't see this view's local state, so the shared count is what makes it
  // back off. Release on close and on unmount so no held count leaks.
  const overlayOpen = menu !== null || dialog !== null;
  // Layout effect: registers before paint, so a keydown on the very tick the
  // menu opens already sees isOverlayOpen() (a plain effect leaves one frame
  // where the embedded listing's shortcuts still fire).
  useLayoutEffect(() => {
    if (!overlayOpen) return;
    acquireOverlay();
    return () => releaseOverlay();
  }, [overlayOpen]);

  const parent = dirname(fsPath);

  // In-flight guard (same as Listing's): a rapid double-invoke would race both
  // calls to the same free "… copy" name and 409 the second.
  const duplicateInFlight = useRef(false);
  const doDuplicate = () => {
    if (duplicateInFlight.current) return;
    duplicateInFlight.current = true;
    (async () => {
      try {
        const dst = await freeDuplicatePath(parent, stat.name, stat.is_dir);
        await copyEntry(fsPath, dst);
        setToast({ msg: `Duplicated as ${basename(dst)}`, tone: "info" });
      } catch (e) {
        setToast({ msg: friendlyFsError(e, { verb: "duplicate", name: stat.name }), tone: "error" });
      } finally {
        duplicateInFlight.current = false;
      }
    })();
  };

  // Hard delete (irreversible) — only reached when the server can't trash.
  const startDelete = () =>
    setDialog({
      kind: "confirm",
      title: "Delete",
      message: stat.is_dir
        ? `Delete the folder "${stat.name}" and everything inside it? This can't be undone.`
        : `Delete "${stat.name}"? This can't be undone.`,
      confirmLabel: "Delete",
      danger: true,
      onConfirm: () => {
        deleteEntry(fsPath, stat.is_dir).then(
          () => {
            clearClipboardIfDeleted(fsPath);
            navigate(parent); // the open file is gone — leave for the parent listing
          },
          (e: Error) => setToast({ msg: friendlyFsError(e, { verb: "delete", name: stat.name }), tone: "error" })
        );
      },
    });

  const doTrash = () => {
    trashEntry(fsPath, stat.is_dir).then((r) => {
      if (r.status === "trashed") {
        clearClipboardIfDeleted(fsPath);
        navigate(parent);
      } else if (r.status === "unsupported") {
        startDelete();
      } else {
        setToast({ msg: friendlyFsError(r.message, { verb: "move to Bin", name: stat.name }), tone: "error" });
      }
    });
  };

  const startRename = () =>
    setDialog({
      kind: "prompt",
      title: "Rename",
      initial: stat.name,
      confirmLabel: "Rename",
      selectStem: true,
      onConfirm: (name) => {
        if (name === stat.name) return;
        const err = nameError(name);
        if (err) {
          setToast({ msg: err, tone: "error" });
          return;
        }
        const dst = join(parent, name);
        renameEntry(fsPath, dst).then(
          () => {
            // The clipboard may still be pointing at the old path (or inside
            // it, if this was a renamed folder holding the cut/copied entry)
            // — repoint it so a later Paste doesn't target a gone source.
            remapClipboardPath(fsPath, dst);
            // Navigate to the renamed file, preserving the current query
            // (`_mode`/params) so the same view stays open on the new path.
            navigateUrl(urlForFsPath(dst, location.search));
          },
          (e: Error) => setToast({ msg: friendlyFsError(e, { verb: "rename", name: stat.name }), tone: "error" })
        );
      },
    });

  const doCopyPath = () => {
    copyToClipboard(fsPath).then((ok) => {
      if (ok) setToast({ msg: "Path copied", tone: "info" });
    });
  };

  const doReveal = () => {
    revealPath(fsPath).catch((e) =>
      setToast({ msg: friendlyFsError(e, { verb: "reveal", name: stat.name }), tone: "error" })
    );
  };

  // Menu for the open file, macOS Finder order. No Open (already viewing it),
  // no Paste/New/Refresh/Download (nothing to paste INTO from a single file).
  const buildMenu = (): MenuEntry[] => [
    { label: "Open With", icon: MenuIcons.openWith, submenu: loadOpenWith },
    "separator",
    { label: "Move to Bin", icon: MenuIcons.trash, onClick: doTrash },
    "separator",
    { label: "Rename…", icon: MenuIcons.rename, onClick: startRename },
    { label: "Duplicate", icon: MenuIcons.duplicate, onClick: doDuplicate },
    "separator",
    { label: "Cut", icon: MenuIcons.cut, onClick: () => setClipboard({ path: fsPath, op: "cut" }) },
    { label: "Copy", icon: MenuIcons.copy, onClick: () => setClipboard({ path: fsPath, op: "copy" }) },
    "separator",
    { label: "Copy Path", icon: MenuIcons.copyPath, onClick: doCopyPath },
    { label: "Reveal in Finder", icon: MenuIcons.reveal, onClick: doReveal },
  ];

  const onContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    setMenu({ x: e.clientX, y: e.clientY, items: buildMenu() });
  };

  const overlays = (
    <>
      {menu && <ContextMenu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />}
      {dialog?.kind === "prompt" && (
        <PromptDialog
          title={dialog.title}
          initialValue={dialog.initial}
          confirmLabel={dialog.confirmLabel}
          selectStem={dialog.selectStem}
          onConfirm={(v) => {
            const { onConfirm } = dialog;
            setDialog(null);
            onConfirm(v);
          }}
          onCancel={() => setDialog(null)}
        />
      )}
      {dialog?.kind === "confirm" && (
        <ConfirmDialog
          title={dialog.title}
          message={dialog.message}
          confirmLabel={dialog.confirmLabel}
          danger={dialog.danger}
          onConfirm={() => {
            const { onConfirm } = dialog;
            setDialog(null);
            onConfirm();
          }}
          onCancel={() => setDialog(null)}
        />
      )}
      {toast && <Toast msg={toast.msg} tone={toast.tone} onClose={() => setToast(null)} />}
    </>
  );

  return { onContextMenu, overlays };
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
  // handling). Re-read on focus/visibility regain (useRefreshOnReturn): a
  // cheap local JSON read, the bookmarks-poll freshness posture (D77)
  // without a timer.
  const aliveDot = useRef(true);
  useEffect(() => () => {
    aliveDot.current = false;
  }, []);
  const refreshDot = () => {
    getDeployStatus(fsPath, false)
      .then((r) => {
        if (aliveDot.current) setDeployment(r.deployment);
      })
      .catch(() => {});
  };
  useEffect(refreshDot, [fsPath]); // initial read (and per-file)
  useRefreshOnReturn(refreshDot);

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
  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);
  const refresh = () => {
    getPrefs()
      .then((p) => {
        if (alive.current) setEnabled(p.deploy.enabled);
      })
      .catch(() => {});
  };
  useEffect(refresh, []); // initial read
  useRefreshOnReturn(refresh);
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

  // Preview already knows its resolved templates, so Open With switches mode
  // IN PLACE (setMode does the editor-flush + `_mode` replaceState) rather than
  // re-navigating to the same path — no re-stat, no iframe teardown/rebuild
  // beyond the mode change the switcher would make anyway.
  const loadOpenWith = () =>
    Promise.resolve(buildOpenWithItems(templates, (m) => void setMode(m)));
  const fileMenu = usePreviewFileMenu(fsPath, stat, loadOpenWith);

  return (
    <>
      <Header fsPath={fsPath} stat={stat} onContextMenu={fileMenu.onContextMenu}>
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
      {fileMenu.overlays}
    </>
  );
}

function FallbackPreview({ fsPath, stat }: { fsPath: string; stat: StatResult }) {
  // No renderable views back this file (that's why it's the fallback), so Open
  // With resolves to the empty "No views available" list without a re-stat.
  const loadOpenWith = () => Promise.resolve(buildOpenWithItems([], () => {}));
  const fileMenu = usePreviewFileMenu(fsPath, stat, loadOpenWith);
  return (
    <>
      <Header fsPath={fsPath} stat={stat} onContextMenu={fileMenu.onContextMenu} />
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
      {fileMenu.overlays}
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
