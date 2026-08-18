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
  getRegistryEntryForPath,
  resetRegistryBinding,
  repairTemplateRegistry,
} from "@platform/lib/api";
import type { Deployment, StatResult, TemplateEntry, RegistryEntryForPath } from "@platform/lib/api";
import { navigate, navigateUrl, urlForFsPath, replaceSearch, IS_EMBED, IS_PREVIEW } from "@platform/lib/router";
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
  claudeTerminalCommand,
} from "@apps/explorer/lib/fs-actions";
import { fileBarMenu } from "@apps/explorer/lib/bar-menus";
import { enterPanel } from "@apps/explorer/lib/split-actions";
import { publishTopbarMenu } from "@apps/explorer/topbar-menu";
import { acquireOverlay, releaseOverlay } from "@platform/lib/ui-overlay";
import { setClipboard } from "@apps/explorer/lib/fs-clipboard";
import { recordFsOp } from "@apps/explorer/lib/fs-undo";
import { pushToast } from "@platform/lib/toast";
import { runCommunity, touchCommunityApp, communityCacheSlug } from "@platform/lib/community";
import { templateModeIcon, modeTitle, KNOWN_SENTINEL_MODES } from "@apps/explorer/ModeSwitcher";
import {
  isModePending,
  isSidebarMode,
  partitionModes,
  visibleModes,
  defaultMode,
  effectiveActive,
} from "@platform/lib/mode-visibility";
import { useDirMode } from "@apps/explorer/lib/dir-mode";
import {
  sideSplit,
  parseSide,
  resolveSide,
  sideParam,
  writeQueryParam,
  sideToggleTarget,
  reconcileSideSearch,
  type SideRequest,
} from "@apps/explorer/lib/preview-side";
import {
  activeRev,
  revFromHook,
  revSrc,
  shortSha,
  type RevSelection,
} from "@apps/explorer/lib/preview-rev";
import { ModeMenu } from "@apps/explorer/BarMenu";
import { SideToggleButton } from "@apps/explorer/SideChrome";
import PreviewSidebar from "@apps/explorer/PreviewSidebar";
import { subscribePreviewSideSlot, previewSideSlot } from "@apps/explorer/preview-side-slot";
import { subscribeTopbarSlot, topbarSlot } from "@apps/explorer/topbar-slot";
import ContextMenu, { type MenuEntry, type MenuItem } from "@platform/ui/ContextMenu";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { PromptDialog, ConfirmDialog, nameError } from "@apps/explorer/FsDialogs";
import DeployModal from "@platform/cloud/DeployModal";
import Listing from "@apps/explorer/Listing";

// The window global the injected runtime calls to hand this shell the commit the
// git sidebar just selected (static/runtime.js `noteRevSelected`, reached from the
// template as `window._fusedSelectRev`). Declared here, beside the assignment that
// installs it, exactly as main.tsx declares `_fusedFsChanged` beside its own — the
// other half of the same ancestor-global contract with that runtime.
declare global {
  interface Window {
    _fusedRevSelected?: (sha: unknown) => void;
  }
}

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

// The preview sidebar's slot, up at StatView level (preview-side-slot.ts). The
// sidebar is a PAGE-LEVEL column — a sibling of the crumb bar and the content
// TOGETHER, not something inside the body under the bar — so the bar ends at the
// divider and the sidebar's own header is the top of its column. This view is
// what knows whether there is a sidebar, so it renders the content and StatView
// renders the box: same arrangement as TopbarActions above, other way round.
function usePreviewSideSlot(): HTMLElement | null {
  return useSyncExternalStore(subscribePreviewSideSlot, previewSideSlot, () => null);
}

// "Clone" in the preview header of a showcase app: copy the app (current
// state, edits included) into the workspace (Fused/local/<slug>, community.py's
// `install`) and navigate to the cloned copy — the same open convention the
// /apps community grid uses. The showcase tree itself is fully editable; the
// clone is how you keep a copy that catalog refreshes never touch.
function CloneCommunityButton({ slug }: { slug: string }) {
  const [busy, setBusy] = useState(false);
  const doClone = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await runCommunity<{ status?: string; message?: string; path?: string }>({
        action: "install",
        slug,
      });
      // `already-installed` also carries the path — an app cloned elsewhere
      // still opens the user's copy rather than erroring.
      if (!r.path) throw new Error(r.message || "clone failed");
      touchCommunityApp(slug);
      navigate(r.path, { isDir: true });
    } catch (e) {
      pushToast({ msg: (e as Error).message || "clone failed", tone: "error" });
      setBusy(false);
    }
    // Success navigates away and unmounts this button; no busy reset needed.
  };
  return (
    <button
      type="button"
      className="bar-ctl bar-ctl-bordered"
      title={"Clone this app into Fused/local/" + slug + " and open your copy"}
      onClick={doClone}
      disabled={busy}
    >
      {busy && <span className="mode-icon-spinner" />}
      {busy ? "Cloning…" : "Clone"}
    </button>
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
  loadOpenWith: () => Promise<MenuItem[]>,
  // "This preview owns the window's crumb bar" — the same flag that portals its
  // mode control into it. While it holds, a right-click anywhere on that bar
  // opens THIS file's bar menu (topbar-menu.ts + lib/bar-menus).
  actionsInTopbar?: boolean
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
        pushToast({ msg: friendlyFsError(r.message, { verb: "delete", name: stat.name }), tone: "error" });
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
            // Undoable, exactly as the listing's own rename is (lib/fs-undo).
            // The stack is module-level and the chord is served by whichever
            // Listing is mounted, so a rename recorded HERE and undone from the
            // folder view afterwards is the normal case, not an edge one — and a
            // rename that skipped this left the stack's top entry describing some
            // older move, so Cmd+Z said "Undid the move" and yanked an unrelated
            // file out of a folder while this rename stayed unreachable.
            recordFsOp({ kind: "rename", pairs: [{ from: fsPath, to: dst }] });
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
    { label: "Delete", icon: MenuIcons.trash, onClick: doTrash },
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

  // Copy the command that starts Claude Code on this file's folder — the same
  // clipboard hand-off the listing's row menu makes, not a launch.
  const doOpenInClaude = () => {
    copyToClipboard(claudeTerminalCommand(fsPath, stat.is_dir, parent)).then((ok) => {
      if (ok) pushToast({ msg: "Command copied — paste it in your terminal", tone: "info" });
    });
  };

  // The CRUMB BAR's menu for this file — deliberately not `buildMenu` above (see
  // lib/bar-menus for what it leaves out and why). The splits are offered on the
  // same condition TemplatePreview uses for its own split affordances: a single
  // file, in the shell window, not inside a pane that already is a split.
  const barMenuItems = (): MenuEntry[] =>
    fileBarMenu({
      onRename: startRename,
      onOpenInClaude: doOpenInClaude,
      onCopyPath: doCopyPath,
      onReveal: doReveal,
      onSplit:
        !stat.is_dir && !IS_EMBED ? (dir) => enterPanel(fsPath, dir) : undefined,
    });

  // Publish it for as long as this preview owns the bar. Through a ref for the
  // reason useFileOps does the same: the builder closes over `fsPath`/`stat`, so
  // a captured function goes stale on the next file, and re-publishing per change
  // would churn the registry (topbar-menu.ts). A DIRECTORY opened here renders an
  // embedded <Listing> that claims the bar and publishes its own folder menu —
  // this one stands down rather than racing it.
  const openBarMenuRef = useRef<(x: number, y: number) => void>(() => {});
  openBarMenuRef.current = (x, y) => setMenu({ x, y, items: barMenuItems() });
  const ownsBar = !!actionsInTopbar && !stat.is_dir;
  useEffect(() => {
    if (!ownsBar) return;
    return publishTopbarMenu((x, y) => openBarMenuRef.current(x, y));
  }, [ownsBar]);

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

// The shell-level revision indicator: what a content pane wears while it is
// showing a PAST commit instead of the live file.
//
// It exists because the pane itself cannot say so. A revision pane is the ordinary
// template rendering ordinary bytes — the code editor looks exactly like the code
// editor — so without this the only difference between "your file" and "your file
// as it was in March" is a save that quietly refuses. So: which commit, said in the
// same 7-character form the sidebar's rows and `git log --oneline` use, and one
// obvious way back.
//
// Chrome, not a new visual language: a `.bar-ctl`-sized pill in the same bar the
// mode control and the sidebar toggle sit in (preview.css), reading as a state
// badge rather than as an action, with the way out being an ordinary bar button.
//
// THE HONEST BIT, and the reason the caveat is in the title rather than nowhere:
// `fused.runPython` readers and the "_render" mode still read the LIVE file
// (static/runtime.js, above runPython — phase 2b), so a parquet/xlsx pane, or an
// .html file previewed as a page, can show current content under this badge. The
// alternative — declining the revision for those modes — would need the shell to
// know which modes get their bytes from Python, which it cannot know for a
// user-registered template, and would leave the user with a Git commit list whose
// clicks silently did nothing on some files.
function RevisionPill({ sha, onLive }: { sha: string; onLive: () => void }) {
  const short = shortSha(sha);
  return (
    <span
      className="preview-rev"
      title={
        `Showing this file as of commit ${short} — read-only. ` +
        "Views that read the file through a Python reader (or run it as a page) " +
        "may still show its current content."
      }
    >
      <span className="preview-rev-label" aria-hidden="true">
        {/* An eye — "you are looking at an old version", not a rewind/clock,
            which would read as "restore to here" for a badge that changes
            nothing on disk. Same paths as the git view's row toggle
            (templates/git/template.html PREVIEW_GLYPH); drawn in the same 16px
            currentColor stroke every glyph in these bars uses (SideChrome). */}
        <svg
          viewBox="0 0 24 24"
          width="14"
          height="14"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M3 12c2.4-3.6 5.4-5.4 9-5.4s6.6 1.8 9 5.4" />
          <path d="M21 12c-2.4 3.6-5.4 5.4-9 5.4s-6.6-1.8-9-5.4" />
          <path d="M12 9a3 3 0 1 0 0 6 3 3 0 1 0 0-6" />
        </svg>
      </span>
      {/* Both facts VISIBLE, not tooltipped: which commit, and that the pane
          cannot be edited. A badge saying only `abc1234` leaves "why did my save
          refuse?" to a hover nobody performs. */}
      <code className="preview-rev-sha">{short}</code>
      <span className="preview-rev-note">read-only</span>
      <button type="button" className="bar-ctl" onClick={onLive}>
        {/* "Live", not "Close": the pane is not being dismissed, it is being
            returned to the file as it is now. */}
        Live
      </button>
    </span>
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
}: {
  fsPath: string;
  stat: StatResult;
  templates: TemplateEntry[];
  conditions: Record<string, boolean> | null;
  onRenderedTitle?: (title: string | null) => void;
  hideHeader?: boolean;
  actionsInTopbar?: boolean;
}) {
  // Caller only renders this when `templates` (already sentinel-filtered by
  // Preview's dispatch, SPEC PT-12) is non-empty. Entries whose condition.py
  // verdict is still in flight (CT-12) are present but PENDING — shown in the
  // switcher as a disabled spinner, not selectable, never the default.
  const isPending = (t: TemplateEntry) => isModePending(t, conditions);

  // --- the content/sidebar split (`_side`) ----------------------------------
  // ONE surface splits: a single FILE opened on the explorer route in its own
  // window. Everything else keeps `claude` as an ordinary content mode,
  // and deliberately:
  //   * a DIRECTORY's chat is the folder-scoped one and has no file preview to
  //     sit beside; its mode list is governed from the listing's pane instead
  //     (see headerActions);
  //   * `IS_EMBED` is every pane of panel/tab mode — those panes ARE a split the
  //     user built, sized by them, with their own bar (PaneModeMenu) writing
  //     `_mode`. A pane that grew a second split of its own would be answering a
  //     layout question the user already answered;
  // Showcase clone app: fully editable, no mode restrictions — the slug only
  // decides whether the Clone button renders in the header.
  const communitySlug = communityCacheSlug(fsPath);
  const splitCapable = !!actionsInTopbar && !stat.is_dir && !IS_EMBED;
  const parts = partitionModes(templates);

  // --- the BORROWED companion: `git`, from this file's parent folder ----------
  // A working tree belongs to the FOLDER (templates/git/condition.py), so the
  // registry keeps `git` on the universal "/" key alone and this file's own
  // template list will never carry one. "What has changed in here" is worth just
  // as much while reading a file, so the sidebar asks the PARENT DIRECTORY for
  // its entry through the ordinary stat + condition machinery every mode surface
  // uses (lib/dir-mode — which is also where the caching lives, so walking a
  // folder file by file costs one probe rather than one per file). A parent
  // outside a repository, or on a mount, denies the gate and there is simply no
  // Git pill.
  //
  // Unless the file HAS one of its own: a user registry may bind `git` to a file
  // extension, and then the entry is the file's, aimed at the file, and there is
  // nothing to borrow — offering both would draw the same mode twice.
  const parentDir = dirname(fsPath);
  const ownGit = parts.sidebar.some((e) => e.mode === "git");
  const parentGit = useDirMode(splitCapable && !ownGit ? parentDir : null, "git");
  const borrowedGit = ownGit ? null : parentGit.entry;
  const borrowedPending = !ownGit && parentGit.pending;
  // Registry order for the file's own companions, then SIDEBAR_MODES order over
  // the assembled list — Claude / Git, whatever the registry ranked
  // (see orderSidebarModes). `on` vs `offered` is the pending placeholder's whole
  // story and lib/preview-side is where it is written down: while the borrowed
  // probe is in flight the entry may be LISTED (so a `?_side=git` deep link is not
  // stripped before the verdict) but decides nothing — it cannot turn the split
  // on for a file that has no companion of its own, cannot become the toggle's
  // target, and cannot leave a `_side` behind if the verdict is no.
  //
  // `bound` is the icon supply for the DISABLED rows, and it is the one place
  // this component deliberately reaches past `templates` to the raw stat: a
  // companion whose gate said no was filtered out upstream (Preview's
  // `visibleModes`), and with it went the icon the switcher still has to draw —
  // an unavailable Claude is the Claude glyph dimmed, not a boxed "C". The
  // parent's `git` binding comes the same way from lib/dir-mode, which keeps it
  // through a denial for exactly this. Icons only: `path` never crosses over
  // (lib/preview-side), so none of these can become something to frame.
  const split = sideSplit({
    splitCapable,
    content: parts.content,
    own: parts.sidebar,
    borrowed: borrowedGit,
    borrowedPending,
    // This file's own gates, for `defaultSide` alone: an absent `_side` must not
    // open a companion whose condition.py has not answered — `claude` HAS one, so
    // that is every file for as long as /api/fs/conditions takes, and on a
    // mount-backed file the answer is no (lib/preview-side's `defaultSide`).
    conditionsPending: conditions === null,
    bound: [
      ...partitionModes(stat.templates).sidebar,
      ...(parentGit.bound ? [parentGit.bound] : []),
    ],
  });
  const sideOn = split.on;
  // What the CONTENT pane may show, and what the SIDEBAR may show. Unsplit
  // surfaces put everything in the content list, which is what keeps their
  // behaviour byte-identical to before. Keyed on `offered` rather than on `on`:
  // the two differ only for a file with no companions of its own, where both
  // branches are the same list anyway, and the sidebar half has to keep listing
  // the pending entry for the deep link's sake.
  const contentModes = split.offered ? parts.content : templates;
  const sidebarModes = split.offered ? split.all : [];
  // What the sidebar's switcher DRAWS, which is a longer list than the one above:
  // all three companions, the ones this file cannot show disabled and explaining
  // themselves (lib/preview-side). Kept apart from `sidebarModes` on purpose —
  // every decision below (`sideEntry`, `activeSide`, the toggle, the reconcile)
  // reads the short list, so a disabled row can be rendered without becoming
  // something the URL or the split can land on.
  const sidebarMenu = split.offered ? split.menu : [];
  // Pending, for a SIDEBAR entry. The borrowed `git` entry is gated on the
  // PARENT's verdicts, resolved by lib/dir-mode — not on any of this file's, so
  // it cannot go through `isPending` (which reads `conditions`, this file's map,
  // and would call a borrowed entry settled the moment the file's own gates
  // landed). Everything else is an ordinary entry of this file's.
  const isSidePending = (t: TemplateEntry) =>
    t.mode === "git" && !ownGit ? borrowedPending : isPending(t);

  const defaultEntry = defaultTemplate(contentModes);
  // `mode` is what the user (or the URL) ASKED for; `entry` is what this paint
  // can actually render. They differ for exactly one render whenever a verdict
  // lands and DROPS the requested mode (a URL-requested conditional that
  // resolved false) — the reconciling effect below cannot run until after that
  // paint. So everything downstream keys off `entry.mode`, never off `mode`:
  // reading the stale request meant the held-frame swap spent that paint with
  // no frame at all (a blank pane), then mounted a frame for the dropped mode
  // whose `srcFor` is null, and only unwound it once the state caught up.
  const [mode, setModeState] = useState<string>(() => activeTemplate(contentModes).mode);
  const entry = contentModes.find((t) => t.mode === mode) || defaultEntry;
  const activeMode = entry.mode;
  // Reconcile the request with what actually rendered. Purely bookkeeping now
  // (the switcher's selection, and the guard in setMode) — no rendering waits
  // on it.
  useEffect(() => {
    if (mode !== activeMode) setModeState(activeMode);
  }, [mode, activeMode]);

  // --- `_side`: which companion the sidebar shows, ABSENT = OPEN (D326) ------
  // Read from the URL at mount as a REQUEST — open/shut plus the companion named,
  // if any — then owned as state and written back through replaceSearch, since the
  // sidebar is a view of this same file and not a navigation. An absent `_side`
  // asks for "open at whatever this file offers first", exactly as it does on a
  // folder (lib/preview-side's header has the whole argument, and why the old
  // absent-means-closed rule had to go); `_side=off` is how a shut sidebar says so.
  //
  // Nothing about it is persisted anywhere. It rides the URL, so it survives the
  // shell's pushState navigation within this file, and a refresh — or an open of a
  // different file, which starts from a bare URL — lands on the default again.
  const [sideReq, setSideReq] = useState<SideRequest>(() => parseSide(location.search));
  // Same request/paint distinction as `mode` above, and here it is RESOLVED rather
  // than reconciled: a verdict that denies the open companion cannot leave this
  // paint framing it, because `activeSide` is recomputed from the lists every
  // render and an unhonourable request falls to the default (lib/preview-side).
  const activeSide = resolveSide(sideReq, split);
  const sideEntry = activeSide ? sidebarModes.find((e) => e.mode === activeSide) ?? null : null;
  // Which companion a bare "open the sidebar" reopens: the last one the user had
  // open on this file, so closing and reopening is not a reset. STATE, not a ref,
  // because the toggle button RENDERS from it — it wears the icon of the mode it
  // would open, so a closed sidebar that last showed Git shows the Git
  // glyph, and a ref read during render is a value React does not promise is
  // current.
  const [lastSide, setLastSide] = useState<string | null>(null);
  useEffect(() => {
    if (activeSide) setLastSide(activeSide);
  }, [activeSide]);
  // What the toggle acts on, and so what it looks like (lib/preview-side). Over
  // the SETTLED companions only: a placeholder whose probe may yet say "no
  // repository here" must not put a button in the bar for the length of that
  // probe and take it away again, and must not outrank a companion this file
  // definitely has.
  const sideTargets = sideOn ? split.settled : [];
  const sideTarget = sideToggleTarget(sideTargets, activeSide, lastSide);
  const sideTargetEntry = sideTargets.find((e) => e.mode === sideTarget) ?? null;

  // --- the CONTENT pane's revision (`_rev`) ---------------------------------
  // A commit clicked in the git sidebar makes the content pane render this file as
  // of that commit. The sha arrives from the sidebar's frame through the runtime's
  // ancestor-window hop — a global on this window, the same idiom
  // `_fusedFsChanged` uses (static/runtime.js), and deliberately NOT a param: see
  // lib/preview-rev for the three places a `_rev` in the address bar would leak to.
  //
  // State, held as {sha, path}, and read ONLY through `activeRev` — which is what
  // makes the clearing rules invariants rather than effects. Nothing here clears
  // anything: a revision chosen for another file, or one left over from a sidebar
  // that has since closed or switched companion, simply does not resolve. See the
  // header of lib/preview-rev.
  const [revSel, setRevSel] = useState<RevSelection | null>(null);
  useEffect(() => {
    // Only the splitting surface installs the hook: it is the one surface with a
    // git sidebar to select in, and two instances racing for one window global
    // (a panel of panes) would have the last mount win the callback for all of
    // them. Re-installed per file so the report is stamped with the file that was
    // open when it arrived.
    if (!splitCapable) return;
    window._fusedRevSelected = (sha: unknown) => setRevSel(revFromHook(sha, fsPath));
    return () => {
      delete window._fusedRevSelected;
    };
  }, [splitCapable, fsPath]);
  const rev = activeRev(revSel, activeSide, fsPath);
  // HOUSEKEEPING, not the guarantee. `activeRev` above already refuses a stale
  // selection on the paint that makes it stale, so nothing depends on this effect
  // running — but a selection kept in memory after its sidebar closed would come
  // BACK the moment the same sidebar reopened, for the few milliseconds before the
  // template's own frame loads and announces "live". Dropping it here means the
  // reopened pane starts live and stays live until a row is clicked again.
  useEffect(() => {
    if (revSel && (activeSide !== "git" || revSel.path !== fsPath)) setRevSel(null);
  }, [revSel, activeSide, fsPath]);

  // The box the sidebar goes in — StatView's, one level up from #content, so the
  // column stands beside the crumb bar rather than under it.
  const sideSlot = usePreviewSideSlot();

  // The one writer. `null` CLOSES, and closing is a value (`_side=off`) rather
  // than a deleted param now that absence means open — while choosing the
  // companion a bare URL would have opened deletes the param instead, so the
  // ordinary state keeps the clean URL (`sideParam`, lib/preview-side).
  const setSide = (next: string | null) => {
    // Written textually (`writeQueryParam`) so a click on the sidebar cannot
    // re-encode a template's own params on its way past them — LSN-2's verbatim
    // rule, and this runs on the first close of every auto-opened sidebar.
    const search = writeQueryParam(
      location.search.replace(/^\?/, ""),
      "_side",
      sideParam(next, split.defaultSide)
    );
    replaceSearch(location.pathname + (search ? "?" + search : ""));
    setSideReq({ open: next !== null, mode: next });
  };
  const toggleSide = () => {
    if (activeSide) setSide(null);
    else if (sideTarget) setSide(sideTarget);
  };

  // Keep the URL honest about what is actually open, for the cases the user's
  // own clicks don't cover: the legacy `_mode=claude` migration above, and a
  // `_side` that named a mode this file doesn't offer (a carried-over param, or
  // a gate that has just denied it). Both are REPLACED, never pushed — neither
  // is a place the Back button should have to visit. The rules, including which
  // of them a still-pending borrowed entry suspends, are in lib/preview-side.
  //
  // Guarded on `splitCapable` and not on the split being ON, which is the whole
  // point: a borrowed `git` that resolves to DENIED takes the split off with it,
  // and a `_side=git` left in the URL there is a param naming a state nothing on
  // this file can honour. (It used to be worse than that — the session sidecar
  // recorded the query and replayed it on the next bare open — which `_side` is now
  // stripped at both ends to prevent, lib/session and server/session.py.)
  const sideKeys = sidebarModes.map((e) => e.mode).join(",");
  useEffect(() => {
    const search = reconcileSideSearch(location.search, {
      splitCapable,
      offered: split.offered,
      open: sideReq.open,
      activeSide,
      defaultSide: split.defaultSide,
    });
    if (search === null) return; // already agrees
    replaceSearch(location.pathname + (search ? "?" + search : ""));
    // `sideKeys` is in the deps because a landing verdict is what makes a
    // previously-fine `_side` stale.
  }, [splitCapable, split.offered, split.defaultSide, sideReq.open, activeSide, sideKeys]);
  const deployEnabled = useDeployEnabled();
  // `_listing` sentinel (D81): the shell's built-in directory listing, mounted
  // in place of the preview iframe — no iframe, no `_file`. Every directory
  // renders through this same header + body chrome (even a plain folder's
  // single `_listing` mode), so the preview header is uniform across files and
  // dirs.
  const isListing = entry.mode === "_listing";
  // Whether the listing's right preview pane is showing — and it is now the same
  // question as "is this a listing at all". The pane has no on/off state to read
  // (no toggle, no `preview` param, no saved key) and, since D282, no width gate
  // either: a Listing that can have a pane has one. This used to measure THIS body
  // with the same ResizeObserver the listing used, so the two could not disagree
  // about whether 700px had been reached; with the threshold deleted there is
  // nothing to measure and nothing to agree on.
  //
  // Used for the one thing the pane displaces: .preview-browse-chip, whose
  // corner is INSIDE the pane when there is one (see its comment below) — an
  // embed-only control now, but an embedded listing can have a pane too. **So the
  // chip no longer appears over a listing at any width**, where a narrow embed
  // used to get one; a `.zarr` folder's embed reaches its map through the chip
  // only while showing the MAP (the `isListing` half), not while showing the
  // listing. The top-bar mode control is not displaced but removed — for an
  // explorer folder it is gone whether the pane is open or not; see headerActions.
  const listingPaneOpen = isListing;
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
    const target = contentModes.find((t) => t.mode === next);
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
  //
  // `_rev` rides here and NOWHERE ELSE (lib/preview-rev): a revision is a property
  // of what this frame is showing, not of where the user is, so it lives on the
  // iframe src exactly as `_file` and `chat_only=1` do. The runtime reads it off
  // the frame's own query and resolves readFile/rawUrl/stat through
  // /api/git/show instead of the live filesystem — which is why no template
  // changes a line for this.
  //
  // It goes onto the "_render" frame too, and that one is a KNOWN partial: /render
  // serves the file's own bytes from disk, so an .html file previewed as itself
  // shows the live page under a revision heading. Same family as the runPython gap
  // (static/runtime.js, above runPython) and deferred with it — the pill below is
  // what keeps it honest. The param is still worth carrying there: any read the
  // page makes through `fused.*` does resolve to the revision, and the write gate
  // applies.
  const remote = stat.remote ? "&_remote=1" : "";
  // A shell loaded as a card thumbnail (IS_PREVIEW) forwards the flag onto
  // every render it triggers, so peeking at an app's entry page is not
  // recorded as opening the app (D301 records on GET /render by default).
  const preview = IS_PREVIEW ? "&_preview=1" : "";
  const srcFor = (m: string): string | null => {
    if (m === "_listing") return null;
    if (m === "_render")
      return revSrc(`/render?path=${encodeURIComponent(fsPath)}${preview}`, rev);
    const t = templates.find((x) => x.mode === m);
    return t
      ? revSrc(
          `/render?path=${encodeURIComponent(t.path as string)}&_file=${encodeURIComponent(fsPath)}${remote}${preview}`,
          rev
        )
      : null;
  };

  // The SIDEBAR's iframe URL. Built here rather than through `srcFor` above,
  // because the two differ on both of the things that URL says:
  //
  //   WHICH ENTRY. The borrowed `git` entry is not in `templates` (it is the
  //   parent folder's — see above), so a lookup there would miss it. The lookup
  //   is `sidebarModes`, which is the list the column is actually showing.
  //
  //   WHAT `_file` NAMES. For the companions of this file, this file. For a
  //   borrowed `git`, the PARENT DIRECTORY — the template is unchanged and asks
  //   git about whatever `_file` names, so aiming it at the folder is the whole
  //   of the borrowing. `_remote` does not travel with it either: that flag says
  //   where THIS FILE's bytes come from, and the git gate refuses a mount-backed
  //   directory outright, so a borrowed target is never remote.
  //
  // Plus the one thing the sidebar has to tell a template about its host —
  // `chat_only=1` for the chat. That template's own layout is a split whose left
  // half is ITS copy of this file's preview (templates/claude/template.html),
  // which in the sidebar would be the same file previewed twice in one window,
  // the inner one a few hundred pixels wide. The param makes it take that half
  // away and run the chat column full width; the template does it through its
  // existing no-pane path (enterNoPane), the same designed absence a folder with
  // no app entry gets.
  //
  // Null while the mode's gate is unresolved — a pending borrowed entry has no
  // template path yet — and the column holds a spinner.
  const sideSrcFor = (m: string): string | null => {
    const t = sidebarModes.find((e) => e.mode === m);
    if (!t || t.path === null) return null;
    const borrowed = m === "git" && !ownGit;
    const target = borrowed ? parentDir : fsPath;
    const rem = borrowed ? "" : remote;
    const chatOnly = m === "claude" ? "&chat_only=1" : "";
    return (
      `/render?path=${encodeURIComponent(t.path)}` +
      `&_file=${encodeURIComponent(target)}${rem}${chatOnly}${preview}`
    );
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
  const otherEntry = contentModes.find((t) => t.mode !== "_listing");
  const counterpart = defaultEntry.mode !== "_listing" ? defaultEntry.mode : otherEntry?.mode;
  const toggleListing =
    otherEntry && contentModes.some((t) => t.mode === "_listing")
      ? () => setMode(isListing ? (counterpart as string) : "_listing")
      : null;

  // Preview already knows its resolved templates, so Open With switches mode
  // IN PLACE (setMode does the editor-flush + `_mode` replaceState) rather than
  // re-navigating to the same path — no re-stat, no iframe teardown/rebuild
  // beyond the mode change the switcher would make anyway.
  //
  // It lists EVERY mode, sidebar companions included — "Open With → Claude" is a
  // request for the chat, and where the chat lives is this view's business, not
  // the menu's. On a splitting surface that request opens the sidebar instead of
  // replacing the content pane, which is the same answer the mode partition
  // gives everywhere else.
  const openMode = (m: string) => {
    if (sideOn && isSidebarMode(m)) setSide(m);
    else void setMode(m);
  };
  const loadOpenWith = () => Promise.resolve(buildOpenWithItems(templates, openMode));
  const fileMenu = usePreviewFileMenu(fsPath, stat, loadOpenWith, actionsInTopbar);

  const headerActions = (
    <>
      {/* FIRST in the bar, left of the mode control: it describes what the pane is
          SHOWING, and the controls that follow act on it. Rendered only while a
          revision actually resolves (lib/preview-rev's `activeRev`), so it cannot
          outlive the pane it describes — and "Live" clears the same state the
          sidebar sets, which is why it does not touch the URL either.
          The sidebar is not touched by it: its own pane still shows the commit's
          DIFF, which is a true statement about that column and the subject its row
          highlight names. The one cost is that re-selecting the SAME row then
          toggles it off first (the template treats a click on the selected row as
          "deselect"), so getting the revision back takes a second click. */}
      {rev && <RevisionPill sha={rev} onLive={() => setRevSel(null)} />}
      {/* Deployable = the mode list carries the "_render" sentinel AND the
          file is .html/.htm — the exporter's actual contract. The extension
          check matters because a registry rebind can put "_render" on any
          type (D73), but /api/export and /api/deploy/preview accept only
          .html/.htm — the button must not open a modal that can't deploy.
          Directories never deploy (no _render binding exists for one today;
          the guard keeps that true even if a registry ever says otherwise).
          Gated on the opt-in Deploy pref (Preferences → Deployments): hidden
          entirely unless the user has turned Deploy on. */}
      {/* Showcase app: Clone copies it into Fused/local so catalog refreshes
          never touch your copy. */}
      {communitySlug && !stat.is_dir && <CloneCommunityButton slug={communitySlug} />}
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
          Files keep this control (they have no pane). The app view had a third
          answer here — it kept this control, because under its own route the
          folder was the whole subject rather than a listing beside a preview —
          and that route is gone, so a folder is a folder wherever you reached
          it from.
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
          switches a folder's own `_mode` from the explorer any more. The pane's
          menu writes `_side` — which of the PANE's three the pane is showing
          (Preview / Claude / Git, listing/pane-side.ts) — and the chip only ever
          offers the listing⇄counterpart pair. So a folder's other `_mode`
          views (`graph`, say) are entered by URL (a bookmark, the file menu's Open
          With) and left by the chip. The user chose that over two switchers in one
          view: for a folder, the pane IS the explorer, and its peers are opt-in
          tools rather than ways of looking at the listing.
          Two of those peers came BACK as pane modes rather than as `_mode` views,
          and that is the same call rather than a reversal: the pane's Claude and
          Git sit beside the listing instead of replacing it, so they are
          companions to browsing the folder — which is exactly the argument the
          file sidebar makes one level down. */}
      {!stat.is_dir && (
        <ModeMenu
          /* Content modes only where the split is on: the companions
             (`claude`, `git`) are the SIDEBAR's list, and offering them
             here as well would be one control writing two different halves of
             the screen. See the partition above. */
          entries={contentModes.map((t) => ({
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
      {/* The sidebar's OPENER, immediately right of the mode control it
          partitions with — the shared control (SideChrome), which is where the
          "one affordance, two places, chosen by state" split between this button
          and the column's own close chevron is written down. It renders only
          while the column is SHUT, and it wears the COMPANION'S OWN ICON, so a
          closed sidebar that last showed Git shows the Git glyph.

          Absent entirely when this file has no companion at all (no `claude`, no
          `git` in the parent, or a gate denied them): a control for
          nothing is worse than no control. */}
      {sideTargetEntry && !activeSide && (
        <SideToggleButton
          what={modeTitle(sideTargetEntry.mode)}
          icon={templateModeIcon(sideTargetEntry)}
          onClick={toggleSide}
        />
      )}
      {/* The app view's overflow lived here — one "Open in explorer" entry,
          jumping from the app's own route back to the folder. The route went
          with D262 and the app view itself with D264. */}
    </>
  );

  return (
    <>
      {actionsInTopbar ? (
        <TopbarActions>{headerActions}</TopbarActions>
      ) : (
        !hideHeader && (
          <Header
            fsPath={fsPath}
            stat={stat}
            onContextMenu={fileMenu.onContextMenu}
          >
            {headerActions}
          </Header>
        )
      )}
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
          <Listing
            fsPath={fsPath}
            /* Same condition as the header's: `actionsInTopbar` IS "this view
               is the explorer's, and the crumb bar is its bar" — so this
               listing is the one that claims the bar's layout zone. */
            barChrome={actionsInTopbar}
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
                /* The shell's ONE contribution to annotation, and deliberately
                   the whole of it: the claude sidebar looks this attribute up
                   through `parent.document` and treats the frame it marks as the
                   document its notes point at — see
                   fused_render/templates/claude/template.html (the annotate
                   target seam). Nothing here knows what annotation is, and the
                   template stays host-agnostic: no mark, no annotate switch.

                   The contract is "exactly one, and it is the content the reader
                   is looking at". So it rides `shown` and not `activeMode`: the
                   swap above keeps BOTH frames mounted while the incoming
                   document loads, and only the shown one is on screen (the other
                   is transparent and un-clickable), so marking the active mode
                   mid-swap would aim the pins at a frame nobody can see. `shown`
                   catches up the moment that frame paints.

                   `splitCapable` is what keeps it to the single-file explorer
                   preview: a folder renders <Listing> and never reaches this
                   branch, and a panel/tab embed has no sidebar to answer the
                   mark. When no content pane shows at all — a listing, a pending
                   gate, the fallback card — no frame renders and the mark is
                   simply absent, which is exactly how the template is told
                   there is nothing to annotate. */
                data-fused-annotate-target={
                  splitCapable && m === shown ? "" : undefined
                }
                /* The REVISION capability, and a second mark rather than a
                   second reading of the one above: they are stamped under the
                   same condition today and they do not mean the same thing —
                   one says "this frame is what notes point at", the other says
                   "a revision can be driven into this frame". A sidebar reading
                   the annotate mark to decide whether to offer a commit preview
                   would be inferring one capability from another, and the day
                   either condition moves it would silently be wrong.

                   Same contract shape as the annotate mark, for the same reason
                   and read the same way (the git template polls
                   `parent.document` for it): PRESENT ONLY WHERE THE CAPABILITY
                   REALLY EXISTS. `splitCapable` is what makes this the single-
                   file explorer preview — the one surface with both a content
                   pane and a git sidebar to select in — and `m === shown` keeps
                   it on the frame the reader is actually looking at, since the
                   held-frame swap can leave two mounted. A folder's listing
                   preview pane renders no frame at all and so stamps nothing,
                   which is exactly how the git template running in THAT pane
                   learns it has nothing to drive. */
                data-fused-rev-target={
                  splitCapable && m === shown ? "" : undefined
                }
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
            full-width timeline view once wore it on its own header), and
            the way back out of a mode you navigated into is the browser's Back
            button, which costs the view nothing to provide.

            Not while the listing's preview pane is open: the chip pins to this
            element's top-right corner, and with the pane on, that corner is
            INSIDE the pane — the chip lands in the pane's header row, where it
            reads as pane chrome. It is not (it switches the FOLDER's mode, not
            the previewed file's), so a bare mode name like "Claude" sitting
            there is a mystery button.

            **That guard now bites in `_listing` mode ALWAYS**, because since D282
            a listing always has a pane — `listingPaneOpen` is exactly `isListing`.
            So the chip only ever renders over a NON-listing mode, and its label is
            unconditionally "Browse contents"; the `isListing` label branches
            ("Back", the counterpart's own name) were unreachable and are deleted
            rather than left as a suggestion that they can happen.

            **The direction that is now unreachable is listing → the other mode.**
            In an embed the whole `.preview-header` and its switcher are hidden, so
            this chip was the only control there: a `.zarr` folder embedded at any
            width can go map → listing and then has nothing to click back with. It
            is a dead end, not a degradation, and it is left standing on purpose —
            the fix is either a chip that does not sit under the pane's corner or a
            pane the embed does not get, and re-gating either on a WIDTH is what
            D282 removed. Recorded in D282 for the owner to rule on. */}
        {toggleListing && !listingPaneOpen && (
          <button
            type="button"
            className="preview-browse-chip"
            onClick={toggleListing}
          >
            Browse contents
          </button>
        )}
      </div>
      {/* The `_side` split's right-hand column, portaled UP to StatView's split
          container (usePreviewSideSlot). It is a sibling of the whole left column
          — crumb bar included — which is what makes the bar stop at the divider
          and the sidebar's header line up with it at the top of the window.
          The portal is also what keeps the CONTENT iframe alive across an
          open/close: nothing above `.preview-body` is restructured, so React never
          re-parents the frame, and re-parenting an iframe reloads its document
          (the same rule the held-frame swap keeps for reordering). */}
      {activeSide &&
        sideSlot &&
        createPortal(
          <PreviewSidebar
            entries={sidebarMenu.map((t) => ({
              mode: t.mode,
              icon: templateModeIcon(t),
              pending: isSidePending(t),
              disabledReason: t.disabledReason,
            }))}
            active={activeSide}
            src={sideEntry && isSidePending(sideEntry) ? null : sideSrcFor(activeSide)}
            onSelect={setSide}
            onClose={() => setSide(null)}
          />,
          sideSlot
        )}
      {fileMenu.overlays}
    </>
  );
}

// The "get me out of this" panel FallbackPreview shows when the reason
// nothing renders is a fixable Template Registry state — a disabling user
// override, or a corrupt user registry.json — rather than a genuinely
// unbound file type (nothing to fix from here, so nothing is shown). Both
// fixes are one click and stay entirely inside the app: no file to find, no
// JSON to hand-edit.
function RegistryFixNotice({ fsPath, isDir, onReload }: { fsPath: string; isDir: boolean; onReload?: () => void }) {
  const [entry, setEntry] = useState<RegistryEntryForPath | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setEntry(null);
    setActionError(null);
    getRegistryEntryForPath(fsPath, isDir).then(
      (r) => alive && setEntry(r),
      () => alive && setEntry({ key: null })
    );
    return () => {
      alive = false;
    };
  }, [fsPath, isDir]);

  if (entry === null) return null; // first paint is instant; this enriches in the background

  // The ONE registry state that truly empties a file's rendered template
  // list is an explicit null/[] override on the key that would otherwise
  // govern it — an unresolvable NAME alone self-heals, because
  // _templates_for falls back to the core list when a user value resolves to
  // nothing at all, so it never reaches this fallback card in the first
  // place. Narrowed into its own variable (rather than a boolean flag) so
  // every field below reads off the checked value, not back off `entry`.
  const resetTarget = entry.key !== null && entry.overridesCore && entry.disabled ? entry : null;
  const registryError = entry.registryError;
  const coreRegistryError = entry.coreRegistryError;
  if (!resetTarget && !registryError && !coreRegistryError) return null;

  // `isFixed` lets a no-op success (repair's `{repaired: false}` — the file
  // already parsed fine, nothing to do) skip the "Fixed" claim instead of
  // reloading and toasting over a state that never changed. Reset has no
  // such no-op shape given how resetTarget is gated above, so it takes the
  // default (every resolution counts as fixed).
  const run = <T,>(action: () => Promise<T>, isFixed: (result: T) => boolean = () => true) => {
    setBusy(true);
    setActionError(null);
    action().then(
      (result) => {
        setBusy(false);
        if (isFixed(result)) {
          pushToast({ msg: "Fixed — reloading this file's preview…", tone: "info" });
          onReload?.();
        } else {
          setActionError("Nothing to repair — the registry file already reads fine.");
        }
      },
      (err: Error) => {
        setBusy(false);
        setActionError(err.message || String(err));
      }
    );
  };

  return (
    <div className="metadata-card registry-fix-notice">
      {registryError && (
        <>
          <p>
            Your Template Registry file couldn't be read: <code>{registryError}</code>
          </p>
          <p className="registry-fix-hint">
            Any custom preview bindings in it are being ignored until this is fixed.
          </p>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy}
            onClick={() => run(repairTemplateRegistry, (r) => r.repaired)}
          >
            Repair Template Registry
          </button>
        </>
      )}
      {coreRegistryError && (
        // No button: this is fused-render's own PACKAGED registry, not
        // anything a request handler may rewrite — it's immutable data healed
        // only by ensure_core_templates' startup check, so the honest fix
        // really is "restart the app", not a click here.
        <p>
          Fused Render's built-in Template Registry couldn't be read: <code>{coreRegistryError}</code>. Restarting
          the app usually fixes this.
        </p>
      )}
      {resetTarget && (
        <>
          <p>
            Previews for <code>{resetTarget.key}</code> files are turned off in your Template Registry.
          </p>
          {resetTarget.coreTemplates && resetTarget.coreTemplates.length > 0 && (
            <p className="registry-fix-hint">
              Restoring the default will bring back: {resetTarget.coreTemplates.join(", ")}.
            </p>
          )}
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy}
            onClick={() => run(() => resetRegistryBinding(resetTarget.key))}
          >
            Restore default previews for {resetTarget.key}
          </button>
        </>
      )}
      {actionError && <p className="registry-fix-error">{actionError}</p>}
    </div>
  );
}

function FallbackPreview({
  fsPath,
  stat,
  actionsInTopbar,
  onReload,
}: {
  fsPath: string;
  stat: StatResult;
  actionsInTopbar?: boolean;
  onReload?: () => void;
}) {
  // No renderable views back this file (that's why it's the fallback), so Open
  // With resolves to the empty "No views available" list without a re-stat.
  const loadOpenWith = () => Promise.resolve(buildOpenWithItems([], () => {}));
  const fileMenu = usePreviewFileMenu(fsPath, stat, loadOpenWith, actionsInTopbar);
  return (
    <>
      {!actionsInTopbar && <Header fsPath={fsPath} stat={stat} onContextMenu={fileMenu.onContextMenu} />}
      <div className="preview-body">
        <div className="metadata-stack">
          <RegistryFixNotice fsPath={fsPath} isDir={stat.is_dir} onReload={onReload} />
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
  // Chrome-free render (the /learn page): no preview header, no mode switcher —
  // the content fills the body directly.
  hideHeader?: boolean;
  // Explorer variant: no preview header bar; the mode switcher/deploy actions
  // portal into the breadcrumb bar's #topbar-mode-slot instead.
  actionsInTopbar?: boolean;
  // Bumps StatView's reloadKey to re-fetch /api/fs/stat in place (App.tsx).
  // FallbackPreview's RegistryFixNotice calls this after a fix succeeds, so a
  // file that starts rendering again (e.g. "_render" is back) does so without
  // a manual refresh. Undefined for callers that don't wire up StatView's
  // reload (e.g. the learn page), where FallbackPreview simply omits the
  // "reloading…" step.
  onReload?: () => void;
}

export default function Preview({ fsPath, stat, onRenderedTitle, hideHeader, actionsInTopbar, onReload }: PreviewProps) {
  // Defensive filter (SPEC PT-12): an entry with path===null whose mode isn't
  // a recognized sentinel (`_render`, `_listing`) is dropped. Filtering here
  // keeps the non-empty dispatch check honest (an all-unknown list falls back
  // instead of crashing TemplatePreview).
  const templates = stat.templates.filter(
    (t) => t.path !== null || KNOWN_SENTINEL_MODES.has(t.mode)
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
      />
    );
  return <FallbackPreview fsPath={fsPath} stat={stat} actionsInTopbar={actionsInTopbar} onReload={onReload} />;
}
