// File preview. Dispatch is exactly two-way (ARCHITECTURE §6):
//   1. stat.templates non-empty -> render active mode in iframe (+_file on iframe URL)
//   2. else                      -> fallback metadata card
// No file-type checks live in the shell — html arrives through stat.templates
// like everything else, via the "_render" sentinel (SPEC PT-12).
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  getDeployStatus,
  rawUrl,
  resolveConditions,
  renameEntry,
  copyEntry,
  revealPath,
  deleteEntry,
} from "@platform/lib/api";
import type { Deployment, StatResult, TemplateEntry } from "@platform/lib/api";
import { navigate, navigateUrl, urlForFsPath, replaceSearch } from "@platform/lib/router";
import { formatSize, formatMtimeFull, basename } from "@platform/lib/format";
import { useRefreshOnReturn } from "@platform/lib/hooks";
import { useDeployEnabled } from "@platform/lib/prefs";
import {
  dirname,
  join,
  freeDuplicatePath,
  copyToClipboard,
  notePathDeleted,
  remapClipboardPath,
  trashEntry,
  buildOpenWithItems,
  friendlyFsError,
} from "@apps/explorer/lib/fs-actions";
import { useAppButton } from "@apps/explorer/lib/app-button";
import { acquireOverlay, releaseOverlay } from "@platform/lib/ui-overlay";
import { setClipboard } from "@apps/explorer/lib/fs-clipboard";
import { pushToast } from "@platform/lib/toast";
import { templateModeIcon, modeTitle, KNOWN_SENTINEL_MODES } from "@apps/explorer/ModeSwitcher";
import {
  isModePending,
  visibleModes,
  defaultMode,
  effectiveActive,
} from "@platform/lib/mode-visibility";
import { ModeMenu, OverflowMenu } from "@apps/explorer/BarMenu";
import { subscribeTopbarSlot, topbarSlot } from "@apps/explorer/topbar-slot";
import { subscribePaneActionSlot, paneActionSlot } from "@apps/explorer/pane-action-slot";
import ContextMenu, { type MenuEntry, type MenuItem } from "@platform/ui/ContextMenu";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { PromptDialog, ConfirmDialog, nameError } from "@apps/explorer/FsDialogs";
import DeployModal from "@platform/cloud/DeployModal";
import Listing from "@apps/explorer/Listing";
import { useSplitIsWide } from "@apps/explorer/listing/pane";

interface HeaderProps {
  fsPath: string;
  stat: StatResult;
  children?: ReactNode;
  // Rendered right after the name, in the same group (e.g. the directory
  // listing's "Open as app" button) — nothing renders there by default.
  afterName?: ReactNode;
  // Right-click on the header chrome opens the file context menu for the open
  // file (views hosting a real preview wire this; transient resolving/loading
  // headers leave it undefined).
  onContextMenu?: (e: React.MouseEvent) => void;
}

function Header({ fsPath, stat, children, afterName, onContextMenu }: HeaderProps) {
  return (
    <div className="preview-header" onContextMenu={onContextMenu}>
      <div className="preview-title">
        <h1 title={fsPath}>{stat.name}</h1>
        {afterName}
      </div>
      <div className="preview-actions">{children}</div>
    </div>
  );
}

// Explorer variant: the second header bar is gone (the name is redundant with
// the breadcrumb), so the view's actions render into the breadcrumb bar's
// `#topbar-mode-slot` (Breadcrumb.tsx) via a portal. The slot node comes from
// a store rather than a getElementById at mount: over a folder the crumb bar
// itself portals down into the listing's left column, which rebuilds the slot
// — and a node captured once would be a detached div from then on
// (topbar-slot.ts).
function TopbarActions({ children }: { children: ReactNode }) {
  const slot = useSyncExternalStore(subscribeTopbarSlot, topbarSlot, () => null);
  return slot ? createPortal(children, slot) : null;
}

// Where the open FOLDER's primary action goes. The preview pane's header when
// there is one (pane-action-slot.ts): the title bar is crowded — crumbs,
// search box, `···` — and a labelled pill among them squeezes the path down to
// nothing, while the header across the divider has room to spare.
//
// Null when there is no pane. Below the split's width threshold it does not
// render at all, and the button has to keep appearing SOMEWHERE — a narrow
// window must not silently cost a folder its primary action — so the caller
// falls back to the bar it came from.
function usePaneActionSlot(): HTMLElement | null {
  return useSyncExternalStore(subscribePaneActionSlot, paneActionSlot, () => null);
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
        pushToast({ msg: `Duplicated as ${basename(dst)}`, tone: "info" });
      } catch (e) {
        pushToast({ msg: friendlyFsError(e, { verb: "duplicate", name: stat.name }), tone: "error" });
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
            notePathDeleted(fsPath);
            navigate(parent, { isDir: true }); // the open file is gone — leave for the parent listing
          },
          (e: Error) => pushToast({ msg: friendlyFsError(e, { verb: "delete", name: stat.name }), tone: "error" })
        );
      },
    });

  const doTrash = () => {
    trashEntry(fsPath, stat.is_dir).then((r) => {
      if (r.status === "trashed") {
        notePathDeleted(fsPath);
        navigate(parent, { isDir: true });
      } else if (r.status === "unsupported") {
        startDelete();
      } else {
        pushToast({ msg: friendlyFsError(r.message, { verb: "move to Bin", name: stat.name }), tone: "error" });
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
          pushToast({ msg: err, tone: "error" });
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
          (e: Error) => pushToast({ msg: friendlyFsError(e, { verb: "rename", name: stat.name }), tone: "error" })
        );
      },
    });

  const doCopyPath = () => {
    copyToClipboard(fsPath).then((ok) => {
      if (ok) pushToast({ msg: "Path copied", tone: "info" });
    });
  };

  const doReveal = () => {
    revealPath(fsPath).catch((e) =>
      pushToast({ msg: friendlyFsError(e, { verb: "reveal", name: stat.name }), tone: "error" })
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
    { label: "Cut", icon: MenuIcons.cut, onClick: () => setClipboard({ paths: [fsPath], op: "cut" }) },
    { label: "Copy", icon: MenuIcons.copy, onClick: () => setClipboard({ paths: [fsPath], op: "copy" }) },
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
    </>
  );

  return { onContextMenu, overlays };
}

// `_mode` (shell URL) selects among stat.templates by name (SPEC PT-9): absent
// or unknown/stale value falls back to the default silently. The default is
// the first UNCONDITIONAL entry (CT-12: a gated template is never the default
// while a normal one exists) — only an all-conditional list falls back to its
// first (by then verdict-allowed) entry.
// Both rules live in lib/mode-visibility so every mode surface resolves the
// same way; `templates` here is already the visible list, so a gate-denied
// `_mode` lands on the default exactly like an unknown one.
function defaultTemplate(templates: TemplateEntry[]): TemplateEntry {
  return defaultMode(templates) as TemplateEntry;
}

function activeTemplate(templates: TemplateEntry[]): TemplateEntry {
  const requested = new URLSearchParams(location.search).get("_mode");
  return effectiveActive(templates, requested) as TemplateEntry;
}

// Deferred condition.py verdicts (CT-12). Stat only MARKS gated templates
// (`conditional: true`) so it stays fast on remote mounts; the actual gates
// run here, in the background, while the first unconditional template is
// already rendering. Returns null while resolving, then {mode: allowed}.
// A failed request resolves to {} — no verdicts at all; lib/mode-visibility
// keeps verdict-less gated entries visible rather than emptying the menu.
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

// --- Held-frame mode swap (A1) ----------------------------------------------
// How long the incoming preview frame takes to fade in over the outgoing one.
// Must match `--dur-med` in shell.css (the CSS owns the actual transition; this
// only decides when the outgoing frame may be unmounted).
const FRAME_FADE_MS = 150;
// Upper bound on holding the outgoing frame. A document that never fires `load`
// (a /render 500, a wedged template daemon) must not strand the user on the
// previous mode's content forever — past this the swap completes regardless.
const FRAME_SWAP_TIMEOUT_MS = 4000;

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

function TemplatePreview({
  fsPath,
  stat,
  templates,
  conditions,
  onRenderedTitle,
  hideHeader,
  actionsInTopbar,
  appChrome,
}: {
  fsPath: string;
  stat: StatResult;
  templates: TemplateEntry[];
  conditions: Record<string, boolean> | null;
  onRenderedTitle?: (title: string | null) => void;
  hideHeader?: boolean;
  // True in the app-builder variant (allowModes pinned): adds the "Open in
  // explorer" header action.
  appChrome?: boolean;
  actionsInTopbar?: boolean;
}) {
  // Caller only renders this when `templates` (already sentinel-filtered by
  // Preview's dispatch, SPEC PT-12) is non-empty. Entries whose condition.py
  // verdict is still in flight (CT-12) are present but PENDING — shown in the
  // switcher as a disabled spinner, not selectable, never the default.
  const isPending = (t: TemplateEntry) => isModePending(t, conditions);
  const defaultEntry = defaultTemplate(templates);
  // `mode` is what the user (or the URL) ASKED for; `entry` is what this paint
  // can actually render. They differ for exactly one render whenever a verdict
  // lands and DROPS the requested mode (a URL-requested conditional that
  // resolved false) — the reconciling effect below cannot run until after that
  // paint. So everything downstream keys off `entry.mode`, never off `mode`:
  // reading the stale request meant the held-frame swap spent that paint with
  // no frame at all (a blank pane), then mounted a frame for the dropped mode
  // whose `srcFor` is null, and only unwound it once the state caught up.
  const [mode, setModeState] = useState<string>(() => activeTemplate(templates).mode);
  const entry = templates.find((t) => t.mode === mode) || defaultEntry;
  const activeMode = entry.mode;
  // Reconcile the request with what actually rendered. Purely bookkeeping now
  // (the switcher's selection, and the guard in setMode) — no rendering waits
  // on it.
  useEffect(() => {
    if (mode !== activeMode) setModeState(activeMode);
  }, [mode, activeMode]);
  const deployEnabled = useDeployEnabled();
  // `_listing` sentinel (D81): the shell's built-in directory listing, mounted
  // in place of the preview iframe — no iframe, no `_file`. Every directory
  // renders through this same header + body chrome (even a plain folder's
  // single `_listing` mode), so the preview header is uniform across files and
  // dirs.
  const isListing = entry.mode === "_listing";
  // Whether the listing's right preview pane is showing. The pane has no
  // on/off state to read any more (no toggle, no `preview` param, no saved
  // key): it appears when the split container is wide enough, so the only way
  // to answer the question is to ask the same measurement the listing asks —
  // hence the same hook, pointed at THIS body, which is the box the listing's
  // own split container fills. Gated on `isListing`: only a directory renders
  // a listing, and only a listing has a pane.
  //
  // Used for the one thing the pane displaces: .preview-browse-chip, whose
  // corner is INSIDE the pane when there is one (see its comment below) — an
  // embed-only control now, but an embedded listing can have a pane too. The
  // top-bar mode control is not displaced but removed — for an explorer folder
  // it is gone whether the pane is open or not; see headerActions. And the
  // folder's "Open as app" is not conditioned on the pane at all any more (see
  // openAsAppBtn).
  const bodyRef = useRef<HTMLDivElement>(null);
  const bodyIsWide = useSplitIsWide(bodyRef);
  const listingPaneOpen = isListing && bodyIsWide;
  // Path of the directory's lone top-level HTML file, reported by Listing
  // (null when there isn't exactly one) — drives the "Open as app" button
  // between the directory name and the mode switcher.
  const [singleAppPath, setSingleAppPath] = useState<string | null>(null);

  // Tab title (App's StatView owns the actual document.title write, and it
  // also feeds the default bookmark name and the Recents row — see
  // Breadcrumb.tsx / recents.ts): only a "_render" entry is the file's OWN
  // html, so only it can carry an authored <title> worth showing over the
  // filename — a template's title is a fixed generic string ("CSV preview")
  // that's strictly worse than the filename StatView falls back to. So a
  // known title must OUTLIVE a mode switch away from "_render": switching
  // modes is local state on this same TemplatePreview instance (`mode`),
  // not a remount, and the filename is often undescriptive ("index.html") —
  // clearing a real title back to that on every switch would be a strict
  // downgrade. Only reset on true unmount (TemplatePreview swapped out
  // entirely — the resolving spinner, FallbackPreview, a re-stat that
  // errors), so a title never outlives the file whose page set it; the
  // "_render" branch overwrites it (to a fresh value, or null if genuinely
  // absent) once its iframe loads. Same-origin iframe (D3/D4 — /render
  // always serves same-origin), so a direct contentDocument read is safe and
  // needs no postMessage round trip.
  const titleObserverRef = useRef<MutationObserver | null>(null);
  useEffect(() => {
    return () => {
      titleObserverRef.current?.disconnect();
      titleObserverRef.current = null;
      onRenderedTitle?.(null);
    };
  }, [onRenderedTitle]);
  const onRenderFrameLoad = (e: React.SyntheticEvent<HTMLIFrameElement>, frameMode: string) => {
    if (frameMode !== "_render" || entry.mode !== frameMode) return;
    // Guards a slow "_render" iframe's load firing AFTER a switch away from
    // it. Two independent guards, both needed: the frame may still be
    // CONNECTED (the held-frame swap keeps the outgoing frame mounted while the
    // incoming one fades in), so the mode comparison above is what rejects a
    // late load from a frame that is no longer the active one; isConnected
    // still covers a frame React has already detached, checked at call time
    // rather than closure-capture time.
    const frame = e.currentTarget;
    if (!frame.isConnected) return;
    const doc = frame.contentDocument;
    const report = () => {
      if (!frame.isConnected) return;
      onRenderedTitle?.(doc?.title.trim() || null);
    };
    report();
    // The authored title can change after load (e.g. a page updates
    // document.title once async data arrives) — watch <head> (not just the
    // <title> node) so both a text edit on an existing <title> and a
    // <title> element added after load are caught; the isConnected guard in
    // `report` covers the same stale-after-unmount race as above.
    titleObserverRef.current?.disconnect();
    if (doc?.head) {
      const observer = new MutationObserver(report);
      observer.observe(doc.head, { childList: true, subtree: true, characterData: true });
      titleObserverRef.current = observer;
    }
  };

  // One switch at a time: the flush below is async, and a second click landing
  // mid-flight could resolve in either order, desyncing iframe key / local
  // state / shell `_mode`. Clicks during a pending switch are dropped.
  const switching = useRef(false);
  // The mode a click is currently switching TO, or null. The flush in
  // doSetMode can block for up to 10s on __fusedFlushEdits, and clicks landing
  // in the meantime are dropped — with nothing on screen that read as a dead
  // button, so the switcher shows a spinner on this entry until the iframe swap
  // begins (A4).
  const [switchingTo, setSwitchingTo] = useState<string | null>(null);
  const setMode = async (next: string) => {
    if (next === activeMode || switching.current) return;
    // Unresolved gate: not selectable (the switcher disables it too).
    const target = templates.find((t) => t.mode === next);
    if (target && isPending(target)) return;
    switching.current = true;
    setSwitchingTo(next);
    try {
      await doSetMode(next);
    } finally {
      switching.current = false;
      setSwitchingTo(null);
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
    // `.is-shown` picks the ACTIVE frame: the held-frame swap can leave an
    // outgoing frame mounted alongside it, and flushing that one's (already
    // detached) editor buffer would be a no-op that silently loses edits.
    const frame = document.querySelector<HTMLIFrameElement>(".preview-body iframe.is-shown");
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
    replaceSearch(location.pathname + (search ? "?" + search : ""));
    setModeState(next);
  };

  // "_render" sentinel (PT-12): render the target file itself, no _file param.
  // Ordinary entries: target file rides on the iframe's own URL as _file —
  // the shell URL's pathname already names the file, so no duplication there.
  // `_remote=1` forwards stat's remote flag (bytes come from a mount) so a
  // page can prefer ranged HTTP reads (/api/fs/raw) over local file I/O.
  // `_listing` builds no src — it renders a shell component, not an iframe.
  const remote = stat.remote ? "&_remote=1" : "";
  const srcFor = (m: string): string | null => {
    if (m === "_listing") return null;
    if (m === "_render") return `/render?path=${encodeURIComponent(fsPath)}`;
    const t = templates.find((x) => x.mode === m);
    return t ? `/render?path=${encodeURIComponent(t.path as string)}&_file=${encodeURIComponent(fsPath)}${remote}` : null;
  };

  // Held-frame swap. Switching mode used to destroy the iframe and mount the
  // next one bare (`key={mode}`), so the user watched a blank pane for as long
  // as the new document took to load. Now the OUTGOING frame stays mounted and
  // visible while the incoming one mounts at opacity 0 and fades in on its own
  // load event; the outgoing one is unmounted once the fade is over.
  //
  // `frames` is append-only in insertion order and is NEVER reordered: React
  // re-parents a moved child, and re-parenting an iframe reloads its document
  // (the same discipline Tabs.tsx keeps for its keep-alive frames). A→B→A
  // therefore keeps [A, B] rather than swapping to [B, A]. Stacking is done
  // with z-index (shell.css), not DOM order.
  const [frames, setFrames] = useState<string[]>(() => (isListing ? [] : [activeMode]));
  // Which frame is visible. Lags `mode` for the length of a swap; the initial
  // frame is shown immediately (it fades from --bg, not from white, so there is
  // nothing to hold back for).
  const [shown, setShown] = useState<string>(activeMode);
  // Modes whose frame has fired `load` at least once and is STILL mounted. A
  // frame the append-only list kept alive will never fire `load` again, so
  // switching back to it (A→B→A inside the swap window) has no event to complete
  // the swap with — without this the 4s fallback below was the only thing that
  // ever made it visible again, i.e. the user sat on mode B for four seconds
  // after asking for A. Entries are dropped when their frame is retired: a later
  // mount of the same mode is a NEW document that has to load again.
  const loadedFrames = useRef<Set<string>>(new Set());
  const framePending = isListing || isPending(entry);
  useLayoutEffect(() => {
    // Layout effect: the incoming frame must be in the DOM before the paint
    // that starts its fade, or the transition has no `from` value to run from.
    if (framePending) {
      setFrames([]);
      setShown(activeMode);
      loadedFrames.current.clear(); // every frame unmounts with them
      return;
    }
    setFrames((f) => (f.includes(activeMode) ? f : [...f, activeMode]));
    // Already mounted AND already loaded: complete the swap now rather than
    // waiting for a load event that cannot come. The `.is-shown` flip still
    // cross-fades through the CSS transition, so this is the same swap, just
    // without the wait. Frames that are mounted but not yet loaded keep the
    // load/timeout path below.
    //
    // Nothing mounted at all (the gate-pending branch above just cleared the
    // list, or a verdict dropped the requested mode) is the same situation as
    // the initial mount: there is no outgoing content to hold, so the incoming
    // frame is shown straight away and fades up from --bg instead of waiting
    // out its load behind an empty pane.
    if (loadedFrames.current.has(activeMode) || frames.length === 0) setShown(activeMode);
    // `frames` is read only to spot the empty case; adding it to the deps would
    // re-run this on every append and re-show a frame mid-swap.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeMode, framePending]);
  // A frame whose document never fires `load` must not strand the user on the
  // previous mode's content: past FRAME_SWAP_TIMEOUT_MS the swap completes
  // regardless of what the incoming frame did.
  useEffect(() => {
    if (shown === activeMode || framePending) return;
    const id = window.setTimeout(() => setShown(activeMode), FRAME_SWAP_TIMEOUT_MS);
    return () => window.clearTimeout(id);
  }, [shown, activeMode, framePending]);
  // Retire the frames the swap left behind, once the incoming one has faded in.
  useEffect(() => {
    if (frames.length <= 1 || shown !== activeMode) return;
    const id = window.setTimeout(() => {
      setFrames([activeMode]);
      // Their documents are gone with them, so they are no longer "loaded".
      for (const m of [...loadedFrames.current]) if (m !== activeMode) loadedFrames.current.delete(m);
    }, FRAME_FADE_MS);
    return () => window.clearTimeout(id);
  }, [frames, shown, activeMode]);

  // Embed hides the whole preview-header, hence the switcher (shell.css). A
  // directory whose mode list carries `_listing` alongside another mode (a
  // .zarr store, or a custom view + listing) surfaces a corner chip to toggle
  // between the listing and that other view (D81 — replaces the old
  // `?listing=1` "Browse contents"). The listing's counterpart is the default
  // mode, UNLESS the default IS the listing (`["_listing", "gallery"]`) — then
  // the first non-listing mode, so an embed whose default is the listing still
  // has a path to the secondary view. Shown only when a non-listing mode exists.
  //
  // There is no opt-out param. `?modechip=false` used to be one, for a single
  // caller: the chat template's left pane framed this embed for a folder with no
  // app entry, and a directory's counterpart mode is that chat (D237), so the
  // chip read "Chat" and sat in the top-right corner of the chat's own preview
  // column — one click from a second agent nested inside the first one's pane.
  // D239 removed that pane, so the param lost its only producer, and a branch no
  // caller can take is a branch nothing can test. If another template ever frames
  // an embed of its counterpart's own target, the opt-out comes back with that
  // caller.
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

  // The folder's relationship to the app system, and the button that follows
  // from it — label, click and destination alike (lib/app-button). The rule
  // used to live here, inline, which is exactly why the preview PANE's version
  // of this button was a different, weaker one: it could only ever say "Open as
  // app", including for folders where that could not work. Both surfaces now
  // render this one.
  const appBtn = useAppButton(isListing ? fsPath : null, singleAppPath);

  // The folder's primary action, built once and rendered in exactly one place
  // — which of the two bars depends on whether there is a pane.
  //
  // It spent a while riding down into the preview pane's header whenever the
  // pane was open, on the theory that the pane's own row already had an empty
  // primary slot for it. That slot belonged to the pane's SELF target (nothing
  // selected) — and a qualifying folder is by definition non-empty (it holds a
  // top-level HTML file), so FS-16's auto-select claims the selection the
  // moment the folder opens. The self row almost never showed, and the button
  // had effectively disappeared from the default view of exactly the folders it
  // exists for. It moved to the title bar for that reason.
  //
  // It is back in the pane header, but on a different footing: the slot is in
  // `strip` now, so it is there in EVERY pane state rather than one that is
  // almost never reached. The title bar meanwhile stopped having room — the
  // search row moved into it, and the pill was pushing the folder's own name
  // out of the crumbs. The bar is still the fallback when the window is too
  // narrow for a pane (paneActionSlot is null then).
  const paneSlot = usePaneActionSlot();
  const openAsAppBtn = appBtn ? (
    <button type="button" className="open-as-app-btn" onClick={appBtn.onClick}>
      {appBtn.label}
    </button>
  ) : null;

  const headerActions = (
    <>
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
      {/* One mode control per view, and for an explorer FOLDER it is the
          preview pane's, not this one. The pane header carries a ModeMenu of
          its own beside the previewed row (ListingPreviewPane), so a folder
          browsed in the explorer had two switchers in view at once — one
          top-right, one a few hundred pixels below it — and telling which
          governed which half is not something a user should have to work out.
          The pane's is the one that stays: it sits with the thing it changes.
          Files keep this control (they have no pane), and the app view/page
          (`appChrome`) keeps everything it has — its folder is the whole
          subject of the route, not a listing beside a preview.
          Getting back out of one of the non-listing modes is the BROWSER'S
          BACK button, and deliberately nothing else (owner call). A folder
          only ever enters those modes by navigating — a typed `?_mode=`, a
          bookmark, Open With — so the navigation that got the user there is
          the thing that undoes it, and it is already at the top of the window.
          This view carried a floating "Browse contents" chip for that state
          for one release; over a template that draws its own header row it sat
          on the content and read as a stray tooltip rather than as a way out.
          A control that has to be explained is worse than the standard one
          every user already has.
          ACCEPTED TRADEOFF, and this part IS the product decision: nothing
          switches a folder INTO one of those modes from the explorer any more.
          The pane's menu writes `_panelMode` — what the PANE previews — not
          `_mode`, and the chip only ever offers the listing⇄counterpart pair.
          So a folder's git/history/graph views are entered by `?_mode=` (a
          URL, a bookmark, the file menu's Open With) and left by the chip. The
          user chose that over two switchers in one view: for a folder, the
          pane IS the explorer, and its peers are opt-in tools rather than ways
          of looking at the listing. */}
      {!(stat.is_dir && !appChrome) && (
        <ModeMenu
          entries={templates.map((t) => ({
            mode: t.mode,
            icon: templateModeIcon(t),
            pending: isPending(t),
          }))}
          active={entry.mode}
          /* Spinner from the click until the incoming frame has actually taken
             over — the flush wait AND the new document's load are both time the
             user is waiting on that button. */
          busy={switchingTo ?? (shown !== activeMode ? activeMode : null)}
          onSelect={setMode}
        />
      )}
      {/* Rightmost, per the bars' grammar: the low-frequency one-shots live in
          the overflow, beside "Open in Finder" and "Copy path" in the title
          bar's own `···`. "Open in explorer" — the counterpart of the
          explorer's "Open as app", jumping from the app experience back to the
          folder where the full template surface lives — held the bar's most
          prominent slot for an action nobody uses twice a session. */}
      {appChrome && stat.is_dir && (
        <OverflowMenu
          items={[
            { label: "Open in explorer", onClick: () => navigate(fsPath, { isDir: true }) },
          ]}
        />
      )}
    </>
  );

  // One or the other, never both.
  const appBtnInPane = paneSlot ? openAsAppBtn : null;
  const appBtnInBar = paneSlot ? null : openAsAppBtn;

  return (
    <>
      {appBtnInPane && createPortal(appBtnInPane, paneSlot as HTMLElement)}
      {actionsInTopbar ? (
        <TopbarActions>
          {appBtnInBar}
          {headerActions}
        </TopbarActions>
      ) : (
        !hideHeader && (
          <Header
            fsPath={fsPath}
            stat={stat}
            onContextMenu={fileMenu.onContextMenu}
            afterName={appBtnInBar}
          >
            {headerActions}
          </Header>
        )
      )}
      <div className="preview-body" ref={bodyRef}>
        {isPending(entry) ? (
          /* URL-requested a gated mode whose verdict is still in flight: hold
             the body until it lands (the iframe must not render a template on
             a file its gate may deny). */
          <div className="preview-resolving">
            <span className="mode-icon-spinner" />
            Checking if this view applies…
          </div>
        ) : isListing ? (
          <Listing
            fsPath={fsPath}
            /* Same condition as the header's: `actionsInTopbar` IS "this view
               is the explorer's, and the crumb bar is its bar" — so this
               listing is the one that claims the bar's layout zone. */
            barChrome={actionsInTopbar}
            onSingleApp={setSingleAppPath}
          />
        ) : (
          /* One frame per mounted mode (see the held-frame swap above). Each
             key is its own mode, so a frame is created once and never
             re-created by a switch away and back within the swap window. */
          <div className="preview-frames">
            {frames.map((m) => (
              <iframe
                key={m}
                className={"preview-frame" + (m === shown ? " is-shown" : "")}
                src={srcFor(m) as string}
                onLoad={(e) => {
                  // Completes the swap: the incoming document has painted, so
                  // it can take over from the frame being held. Recorded so a
                  // switch BACK to this still-mounted frame can complete
                  // without a second load event (see loadedFrames).
                  loadedFrames.current.add(m);
                  if (m === activeMode) setShown(m);
                  onRenderFrameLoad(e, m);
                }}
              />
            ))}
          </div>
        )}
        {/* EMBED ONLY, by the CSS (see .preview-browse-chip): it is the embed's
            whole mode affordance, because the embed hides .preview-header and
            with it the switcher (PT-13/D65).

            It briefly had a second, explorer-side reveal — `is-exit`, for a
            folder showing a non-listing mode — on the grounds that PT-13b's
            missing top-bar switcher left that state with no way back to the
            listing. Removed by owner call: floating over the template's own
            content it read as a stray tooltip rather than as chrome (a
            full-width `history` view wore it on its HISTORY header), and
            the way back out of a mode you navigated into is the browser's Back
            button, which costs the view nothing to provide.

            Not while the listing's preview pane is open: the chip pins to this
            element's top-right corner, and with the pane on, that corner is
            INSIDE the pane — the chip lands in the pane's header row, where it
            reads as pane chrome. It is not (it switches the FOLDER's mode, not
            the previewed file's), so a bare mode name like "Claude" sitting
            there is a mystery button. The guard only ever bites in `_listing`
            mode, since the pane exists only there. */}
        {toggleListing && !listingPaneOpen && (
          <button
            type="button"
            className="preview-browse-chip"
            onClick={toggleListing}
          >
            {!isListing
              ? "Browse contents"
              : counterpart === defaultEntry.mode
                ? "Back"
                : modeTitle(counterpart as string)}
          </button>
        )}
        {/* Embed hides .preview-header (see afterName above), so the "Open as
            app" affordance also rides as a corner chip pinned over the
            listing, revealed only in embed — same pattern as
            preview-browse-chip. Opposite corner so the two can coexist (a
            directory can have both a browsable counterpart mode AND a lone
            HTML file).

            Not when there is a PANE, though: embed hides the two headers but
            not the pane's strip, so the portaled button is on screen there and
            the chip would be the same action twice, one of them lying on top
            of the listing. Keyed on the slot rather than on `listingPaneOpen`
            because the slot is the button's actual whereabouts — the two agree
            almost always, and when they disagree it is the slot that is right.
            .preview-browse-chip makes the same call one condition up. */}
        {appBtn && !paneSlot && (
          <button type="button" className="open-as-app-chip" onClick={appBtn.onClick}>
            {appBtn.label}
          </button>
        )}
      </div>
      {fileMenu.overlays}
    </>
  );
}

function FallbackPreview({ fsPath, stat, actionsInTopbar }: { fsPath: string; stat: StatResult; actionsInTopbar?: boolean }) {
  // No renderable views back this file (that's why it's the fallback), so Open
  // With resolves to the empty "No views available" list without a re-stat.
  const loadOpenWith = () => Promise.resolve(buildOpenWithItems([], () => {}));
  const fileMenu = usePreviewFileMenu(fsPath, stat, loadOpenWith);
  return (
    <>
      {!actionsInTopbar && <Header fsPath={fsPath} stat={stat} onContextMenu={fileMenu.onContextMenu} />}
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
            <dd>{formatMtimeFull(stat.mtime)}</dd>
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
  // Reports the "_render" iframe's own authored <title>, so callers wanting a
  // better tab title than the filename can use it (App's StatView). Undefined
  // for every dispatch branch that isn't the "_render"-carrying TemplatePreview.
  onRenderedTitle?: (title: string | null) => void;
  // Sub-app mode allowlist: when set, only these modes from stat.templates are
  // offered (the app-builder pins its views to the app modes, App.tsx
  // APP_MODES). The server keeps resolving the full list; this is a UI
  // restriction only —
  // `_mode` semantics on the URL are unchanged.
  allowModes?: string[];
  // Chrome-free render (the /learn page): no preview header, no mode switcher —
  // the content fills the body directly.
  hideHeader?: boolean;
  // Explorer variant: no preview header bar; the mode switcher/deploy actions
  // portal into the breadcrumb bar's #topbar-mode-slot instead.
  actionsInTopbar?: boolean;
}

export default function Preview({ fsPath, stat, onRenderedTitle, allowModes, hideHeader, actionsInTopbar }: PreviewProps) {
  // Defensive filter (SPEC PT-12): an entry with path===null whose mode isn't
  // a recognized sentinel (`_render`, `_listing`) is dropped. Filtering here
  // keeps the non-empty dispatch check honest (an all-unknown list falls back
  // instead of crashing TemplatePreview).
  const templates = stat.templates.filter(
    (t) =>
      (t.path !== null || KNOWN_SENTINEL_MODES.has(t.mode)) &&
      (!allowModes || allowModes.includes(t.mode))
  );
  // Deferred gates (CT-12): resolve condition.py verdicts in the background.
  // The first unconditional template renders immediately — only an
  // ALL-conditional list has nothing safe to show and waits here.
  const conditions = useConditions(fsPath, templates);
  const resolving = conditions === null;
  // Shared visibility policy (lib/mode-visibility): gated entries are pending
  // while resolving, stay when no verdict ever arrived, and drop on an
  // explicit denial — including when the URL asked for one, which then falls
  // back to the default (activeTemplate) or, if nothing survives, to
  // FallbackPreview below.
  const visible = visibleModes(templates, conditions);
  if (resolving && templates.length > 0 && templates.every((t) => t.conditional)) {
    return (
      <>
        {!actionsInTopbar && <Header fsPath={fsPath} stat={stat} />}
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
    return (
      <TemplatePreview
        fsPath={fsPath}
        stat={stat}
        templates={visible}
        conditions={conditions}
        onRenderedTitle={onRenderedTitle}
        hideHeader={hideHeader}
        actionsInTopbar={actionsInTopbar}
        appChrome={!!allowModes}
      />
    );
  return <FallbackPreview fsPath={fsPath} stat={stat} actionsInTopbar={actionsInTopbar} />;
}
