// Route dispatch (super-app step 2 — shell + three sub-apps):
//   "/"                      -> redirect (replaceState) to /explorer
//   "/apps"                  -> apps homepage (the app home)
//   "/explorer"              -> file-explorer homepage (FilesHome)
//   "/explorer/view/<path>"  -> stat it: directory -> listing, file -> preview
//   "/explorer/embed/<path>" -> chrome-free embed variant
//   "/learn"                 -> learn content, chrome-free (variant "learn")
//   "/claude-config"         -> Claude config panel (native, no mount)
//   "/claude-md"             -> legacy; redirects into the panel's MD Files tab
//   "/ai-models"             -> Hugging Face cache inventory
//   "/preferences|/templates|/mounts" -> settings pages
// Legacy pre-rename urls (/view/..., /embed/..., /view/_prefs-family) are
// rewritten in place at boot by router.ts before any of this runs.
// The active view is keyed by the nav epoch: every navigation remounts it,
// which is the React equivalent of the vanilla shell rebuilding the view DOM
// on each route() call (fresh iframes, fresh fetches, dropped local state).
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { IS_EMBED, fsPathFromLocation, isPanelPath, navHintIsDir } from "@platform/lib/router";
import { useSessionRestore, useSessionTracking } from "@platform/lib/session";
import { useRecentsTracking } from "@apps/explorer/lib/recents";
import { statPath, getMounts, reconnectMount, type Config, type Mount, type StatResult } from "@platform/lib/api";
import { useNavEpoch, useDocumentTitle, useRefreshOnReturn, useLearnMountReady, useSessionsMountReady } from "@platform/lib/hooks";
import { useMountHealth } from "@platform/lib/mountHealth";
import { useScheduleEvents } from "@platform/lib/scheduleEvents";
import { basename } from "@platform/lib/format";
import { maybeAutoStartTour } from "@platform/lib/tour";
import { useThemeSync } from "@platform/lib/theme";
import GlobalSidebar from "@shell/GlobalSidebar";
import NotificationHost from "@platform/ui/NotificationHost";
import QueueDock from "@shell/QueueDock";
import ShortcutsOverlay from "@platform/ui/ShortcutsOverlay";
import { isMod } from "@platform/lib/platform";
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import { getClipboard, setClipboard } from "@apps/explorer/lib/fs-clipboard";
import { reconcileOsClipboard } from "@apps/explorer/lib/os-clipboard";
import { BreadcrumbBar, StaticBreadcrumb } from "@apps/explorer/Breadcrumb";
import Listing from "@apps/explorer/Listing";
import Preview from "@apps/explorer/Preview";
import { PreviewSideSlot } from "@apps/explorer/PreviewSidebar";
import Panel from "@apps/explorer/Panel";
import Tabs from "@apps/explorer/Tabs";
import FilesHome from "@apps/explorer/FilesHome";
import Home from "@shell/Home";
import { learnEntryPath } from "@apps/learn";
import { sessionsEntryPath } from "@apps/sessions";
import { useClaudeConfigAvailable } from "@apps/claude_config/available";

// Route-gated surfaces, lazy-loaded: none of these render on the front door
// (the explorer route above stays eager), only once a route nobody may ever
// visit this session is actually opened — the settings pages, the AI Models
// page, the app-builder hub, the Claude Config panel, and the bookmark-open
// redirector. Splitting them out of the main chunk is what fixes vite's
// "chunks larger than 500 kB" build warning without just raising the limit.
const Preferences = lazy(() => import("@shell/Preferences"));
const Templates = lazy(() => import("@shell/templates/Templates"));
const Mounts = lazy(() => import("@shell/Mounts"));
const AiModels = lazy(() => import("@shell/AiModels"));
const Scheduled = lazy(() => import("@shell/Scheduled"));
const Apps = lazy(() => import("@apps/builder/Apps"));
const ClaudeConfig = lazy(() =>
  import("@apps/claude_config").then((m) => ({ default: m.ClaudeConfig })),
);
const BookmarkOpen = lazy(() => import("@apps/explorer/BookmarkOpen"));
// Canvases (legacy-workbench local development): the listing and the
// per-canvas workspace with the embedded live workbench.
const Canvases = lazy(() =>
  import("@apps/canvases").then((m) => ({ default: m.Canvases })),
);
const CanvasWorkspace = lazy(() =>
  import("@apps/canvases").then((m) => ({ default: m.CanvasWorkspace })),
);

type StatState =
  | { status: "loading" }
  | { status: "ok"; stat: StatResult }
  | { status: "error"; message: string };

// `reloadKey` re-runs the stat without a navigation — used to recover after a
// disconnected mount is reconnected in place (StatErrorView), where fsPath and
// epoch are both unchanged.
function useStat(fsPath: string | null, epoch: number, reloadKey: number): StatState {
  const [state, setState] = useState<StatState>({ status: "loading" });
  useEffect(() => {
    if (!fsPath) {
      setState({ status: "loading" });
      return;
    }
    let alive = true;
    setState({ status: "loading" });
    statPath(fsPath).then(
      (stat) => alive && setState({ status: "ok", stat }),
      (err: Error) => alive && setState({ status: "error", message: err.message })
    );
    return () => {
      alive = false;
    };
  }, [fsPath, epoch, reloadKey]);
  return state;
}

// A file on a mount goes unreachable when the mount is disconnected or wedged.
// The raw stat error is a dead end, so detect that the failing path sits under
// a known mount and offer to reconnect it in place. `state` is a real health
// probe (rcd listing + a timed listdir, shell/mounts.py), but a stat can fail
// under a mount for reasons the probe misses, so the button shows whenever the
// path is under a mount — not only when it reports down.
function StatErrorView({
  fsPath,
  message,
  onReload,
}: {
  fsPath: string;
  message: string;
  onReload: () => void;
}) {
  // undefined = still checking; null = not under any mount.
  const [mount, setMount] = useState<Mount | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [mountErr, setMountErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getMounts().then(
      (r) => {
        if (!alive) return;
        // Longest matching mountpoint wins (nested mounts).
        const hit = r.mounts
          .filter((m) => fsPath === m.mountpoint || fsPath.startsWith(m.mountpoint + "/"))
          .sort((a, b) => b.mountpoint.length - a.mountpoint.length)[0];
        setMount(hit ?? null);
      },
      () => alive && setMount(null)
    );
    return () => {
      alive = false;
    };
  }, [fsPath]);

  const reconnect = async () => {
    if (!mount) return;
    setBusy(true);
    setMountErr(null);
    try {
      // reconnectMount handles every bad state in one call: clears rcd's
      // tracking, force-unmounts a dead kernel mount that rejects a plain
      // umount (the wedged-NFS case), then mounts fresh.
      await reconnectMount(mount.id);
      setBusy(false);
      onReload(); // re-stat; success replaces this view with the preview
    } catch (e) {
      setMountErr((e as Error).message);
      setBusy(false);
    }
  };

  // Mount lookup still in flight: hold off rather than flash the generic
  // stat error and then flip it to the reconnect card a beat later.
  if (mount === undefined) return null;
  if (mount) {
    const wedged = mount.state !== "unmounted";
    return (
      <div className="status-message error">
        <p>
          <strong>{mount.name}</strong> {wedged ? "isn’t responding" : "is disconnected"} — this
          file is on a mount that isn’t currently available.
        </p>
        <button type="button" disabled={busy} onClick={reconnect}>
          {busy ? "Reconnecting…" : wedged ? "Reconnect" : "Mount"}
        </button>
        {mountErr && <div className="deploy-error">{mountErr}</div>}
      </div>
    );
  }
  return (
    <div className="status-message error">
      Failed to stat {fsPath}: {message}
    </div>
  );
}

// First paint while `stat` is still in flight (~1.6s on a cold remote mount),
// so a navigation shows a populated scaffold instead of a blank screen. The
// breadcrumb is already rendered by StatView; here the preview header shows the
// folder/file name (from the URL) with a spinner where the template
// ModeSwitcher will land once stat resolves. When the nav hint says this is a
// directory, the real Listing mounts NOW — its /api/fs/list runs in parallel
// with stat rather than serialized behind it, and the same fetch is reused
// (api.prefetchListDir) when stat resolves and the preview remounts the
// listing. Without a directory hint we can't safely show a listing (a file's
// list would 404), so only the header + a neutral loading body paint.
function LoadingScaffold({ fsPath, isDir, headerless }: { fsPath: string; isDir: boolean; headerless?: boolean }) {
  return (
    <>
      {/* Mirror the loaded Header exactly (Preview.tsx `Header`): the name in a
          `.preview-title` group, and a `.mode-switcher-placeholder` that reserves
          the mode switcher's button height in the actions slot. Without this the
          header grows (spinner → 28px buttons) when stat resolves, dropping the
          name and the whole body — a visible layout shift on every navigation.
          Skipped for the explorer (`headerless`): its actions live in the
          breadcrumb bar's slot, and there is no second header bar at all. */}
      {!headerless && (
        <div className="preview-header">
          <div className="preview-title">
            <h1 title={fsPath}>{basename(fsPath)}</h1>
          </div>
          <div className="preview-actions">
            <span className="mode-switcher-placeholder" aria-label="Loading">
              <span className="mode-icon-spinner" />
            </span>
          </div>
        </div>
      )}
      <div className="preview-body">
        {isDir ? (
          // provisional: the hint could be stale (file, not dir). Suppress
          // Listing's hard "Failed to list" error while stat resolves — a 404
          // here just means the hint was wrong; stat will paint the file view.
          // `barChrome` on the scaffold too (same condition as `headerless`):
          // whenever the nav hint already says "directory" this claims the
          // bar's layout zone from the first paint, so the splits don't flash
          // in and out across the scaffold→resolved swap. Only a hinted nav
          // gets that — open a folder URL directly (or reload) and there is no
          // `history.state` hint, so no Listing mounts here and the splits do
          // still show for the length of the stat.
          <Listing fsPath={fsPath} provisional barChrome={headerless} />
        ) : (
          <div className="preview-resolving">
            <span className="mode-icon-spinner" />
            Loading…
          </div>
        )}
      </div>
    </>
  );
}

// Suspense fallback for the lazy-loaded routes above. Brief on a local
// server — most resolve within a frame or two — so this deliberately carries
// no label of its own; each panel paints its own scaffolding once it mounts.
function RouteFallback() {
  return (
    <div className="preview-resolving">
      <span className="mode-icon-spinner" />
    </div>
  );
}

// Stat-backed views (listing/preview): breadcrumb + content under one hook
// component so useStat only runs when the pathname is a real fs path, not a
// sentinel.
//
// `variant` selects the sub-app chrome:
//   "explorer" (default) — breadcrumb, full preview header, file recents.
//   "learn"              — no breadcrumb, no preview header, no recents.
//
// There was a third, "app", for the /apps/<tag>/<name> route: no breadcrumb, the
// builder's sidebar, and the mode switcher pinned to an APP_MODES allowlist
// (`app`, `claude`, and a per-path timeline mode). Route and variant are both
// gone — an app folder
// is browsed on the explorer route now, where it gets the breadcrumb it always
// had a path for and the switcher's full list, whose extra directory entries
// (`git`, `graph`, `zarr_aoi`) the pin existed to hide and which are perfectly
// sensible over an app.
function StatView({
  fsPath,
  epoch,
  home,
  variant = "explorer",
}: {
  fsPath: string;
  epoch: number;
  home: string;
  variant?: "explorer" | "learn";
}) {
  // Bumped by StatErrorView to re-stat in place after reconnecting a mount.
  const [reloadKey, setReloadKey] = useState(0);
  // Directory hint from the navigation that mounted this view (see router
  // navHintIsDir). Captured ONCE at mount — StatView is keyed by epoch+fsPath
  // so it remounts per navigation. In-place param syncs go through
  // router.replaceSearch, which preserves history.state, so the hint survives
  // for Back/Forward; capturing once here is belt-and-braces (and correct even
  // if some future caller forgets to preserve it).
  const [navIsDir] = useState<boolean | null>(() => navHintIsDir());
  const stat = useStat(fsPath, epoch, reloadKey);
  // null until the stat resolves — the session hooks opt out for anything that
  // is not a confirmed file, so a directory never gets a restore/track before
  // its kind is known.
  const isDir = stat.status === "ok" ? stat.stat.is_dir : null;
  // null until stat resolves. A non-writable file (read-only mount) can't hold
  // a session sidecar, so the session hooks skip it — crucially, restore does
  // NOT block the template on a cold, guaranteed-null /api/session read there.
  const writable = stat.status === "ok" ? stat.stat.writable ?? null : null;
  // Per-file session restore (LSN-*): replay the file's last URL query on a
  // bare open, and track qualifying param changes back into the sidecar.
  // `ready` gates the preview so the iframe mounts with the restored params
  // already on the shell URL (no param flash from defaults -> restored).
  const ready = useSessionRestore(fsPath, isDir, writable);
  useSessionTracking(fsPath, isDir, writable);
  // A "_render" preview (the file's own HTML, no template) reports its
  // authored <title> here (Preview -> TemplatePreview); everything else
  // (templates, listings, fallback cards) has no better name than the
  // file's own, so this stays null and the basename wins below. Local state
  // is safe to reset only on remount (StatView is keyed by fsPath in App),
  // not on a `_mode` switch within the same file — TemplatePreview owns that.
  const [renderedTitle, setRenderedTitle] = useState<string | null>(null);
  useDocumentTitle(fsPath === "/" ? null : renderedTitle || basename(fsPath));
  // Recents: the explorer's own store, gated on a confirmed FILE (same gate as
  // session tracking), so learn and embed panes never write there. The app
  // builder's parallel (tag, name) store went with its route — nothing displays
  // it now that the builder sidebar is gone.
  useRecentsTracking(fsPath, variant === "explorer" ? isDir : null, renderedTitle);
  let content = null;
  if (stat.status === "loading") {
    // Not a blank screen: paint the scaffold immediately (Fix #1). A directory
    // nav also starts its listing fetch now, parallel with stat (Fix #2).
    content = <LoadingScaffold fsPath={fsPath} isDir={navIsDir === true} headerless={variant === "explorer"} />;
  } else if (stat.status === "error") {
    content = (
      <StatErrorView
        fsPath={fsPath}
        message={stat.message}
        onReload={() => setReloadKey((k) => k + 1)}
      />
    );
  } else if (stat.status === "ok") {
    // Dispatch (ARCHITECTURE §6): a target with templates previews — even a
    // directory. Every directory resolves at least the universal `/` key's
    // `["_listing"]` (D81), so the built-in listing is now the `_listing`
    // sentinel mode and flows through Preview like any other mode (Preview
    // renders the shell Listing component for it). A directory resolves to an
    // empty list only when a `null` binding disables it; the shell still lists
    // it then — a folder must always render something.
    const s = stat.stat;
    if (s.is_dir && s.templates.length === 0) {
      content = <Listing fsPath={fsPath} barChrome={variant === "explorer"} />;
    } else if (!ready) {
      // Brief; only for files opened with an empty query while the sidecar
      // read resolves. Directories and param/bookmark opens are ready
      // synchronously (useSessionRestore), so no flash on those paths. Paint
      // the same file scaffold as the stat-loading branch (header + spinner in
      // the file's chrome) rather than a bare centered "Loading…" — on a cold
      // mount this wait is ~2s and must never read as a blank/black screen.
      content = <LoadingScaffold fsPath={fsPath} isDir={false} headerless={variant === "explorer"} />;
    } else {
      content = (
        <Preview
          fsPath={fsPath}
          stat={s}
          onRenderedTitle={setRenderedTitle}
          hideHeader={variant === "learn"}
          actionsInTopbar={variant === "explorer"}
        />
      );
    }
  }
  // The PAGE-LEVEL split: this view's own column on the left — its bar and its
  // content, one above the other — and, when a file preview opens one, the
  // sidebar's column on the right (Preview's `_side`, apps/explorer/
  // PreviewSidebar). The wrapper is unconditional so opening the sidebar cannot
  // restructure the tree above `#content` and remount the view.
  //
  // The bar is INSIDE the left column, which is the whole point: it ends at the
  // divider instead of spanning the window over both columns, so the sidebar's
  // own header row is the top of the window on its side rather than a bar-height
  // below the left one. Same shape as the listing and its preview pane over a
  // folder (.listing-split / .listing-main), for the same reason.
  //
  // The slot stands empty on every route that has no sidebar — every folder, the
  // learn, embed panes — and `display: contents` on an empty
  // element costs the layout nothing (explorer.css).
  return (
    <div className="stat-split">
      <div className="stat-main">
        {/* Only the explorer carries a breadcrumb bar — learn renders its
            content directly (no path chrome). BreadcrumbBar
            owns the `#breadcrumb` box itself: over a folder it portals the whole
            bar down into the listing's left column, so it can't be a wrapper
            rendered here (Breadcrumb.tsx). */}
        {variant === "explorer" && (
          <BreadcrumbBar fsPath={fsPath} home={home} renderedTitle={renderedTitle} />
        )}
        <div id="content">{content}</div>
      </div>
      <PreviewSideSlot />
    </div>
  );
}

// /learn: the bundled learn content rendered chrome-free (no breadcrumb, no
// preview header) inside the shell frame. Waits on the learn mount record
// (useLearnMountReady) before statting the entry, so a boot-race never shows
// a dead 404.
function LearnView({ config, epoch }: { config: Config; epoch: number }) {
  const ready = useLearnMountReady(config.learn_mount_ready);
  const entry = learnEntryPath(config);
  if (!ready || !entry) {
    return (
      <div id="content">
        <div className="preview-resolving">
          <span className="mode-icon-spinner" />
          Preparing learn content…
        </div>
      </div>
    );
  }
  return <StatView key={epoch + ":" + entry} fsPath={entry} epoch={epoch} home="" variant="learn" />;
}

// /sessions: the bundled Claude Sessions inbox, same chrome-free treatment as
// learn (variant "learn" — no breadcrumb, no preview header, no recents).
// Waits on the sessions mount record before statting the entry, so a
// boot-race never shows a dead 404.
function SessionsView({ config, epoch }: { config: Config; epoch: number }) {
  const ready = useSessionsMountReady(config.sessions_mount_ready);
  const entry = sessionsEntryPath(config);
  if (!ready || !entry) {
    return (
      <div id="content">
        <div className="preview-resolving">
          <span className="mode-icon-spinner" />
          Preparing sessions content…
        </div>
      </div>
    );
  }
  return <StatView key={epoch + ":" + entry} fsPath={entry} epoch={epoch} home="" variant="learn" />;
}

// /claude-config: the native Claude Config panel. Chrome-free like
// learn/sessions, but native React — no mount, no StatView; the availability
// gate mirrors the sidebar entry's, so a direct URL hit while ~/.claude is
// absent shows an honest empty state instead of a dead panel.
function ClaudeConfigView() {
  const available = useClaudeConfigAvailable();
  return (
    <div id="content">
      <div className="cc-page">
        {available ? (
          <Suspense fallback={<RouteFallback />}>
            <ClaudeConfig />
          </Suspense>
        ) : (
          <div className="preview-resolving">No Claude Code configuration found (~/.claude).</div>
        )}
      </div>
    </div>
  );
}

export default function App({ config }: { config: Config }) {
  const epoch = useNavEpoch();

  // Background mount-health poll → global disconnect/reconnect toasts. Mounted
  // once here for the page's lifetime (no-ops in embed); renders via NotificationHost.
  useMountHealth();

  // The same shape, for scheduled messages: nobody is looking at /scheduled when
  // one fires, so "it ran" / "it failed" / "it was missed" has to arrive on its
  // own rather than wait to be discovered.
  useScheduleEvents();

  // Keep <html data-theme> in step with the appearance preference for the
  // page's lifetime (SPEC §30): another window's override, and — while the
  // setting is System — the OS flipping mid-session, including macOS's
  // automatic sunset switch. Mounted in embed too: every pane's embed shell is
  // its own document and has to repaint with the rest. The FIRST application
  // already happened in index.html's pre-paint bootstrap, so this can never
  // cause a flash, and it only ever writes an attribute — no re-render reaches
  // a live iframe.
  useThemeSync();

  // Adopt files copied in the native file manager (SPEC §3). Returning to the
  // app is the only moment the system clipboard can have changed from the
  // user's point of view, and useRefreshOnReturn already coalesces the doubled
  // focus/visibilitychange pair. It deliberately skips mount, so the app's
  // first read is the effect below — otherwise a copy made in Finder *before*
  // the window opened would never be seen.
  useRefreshOnReturn(() => {
    void reconcileOsClipboard();
  });
  useEffect(() => {
    void reconcileOsClipboard();
  }, []);

  // Mod+K cheat sheet. Owned by App, not Listing: it documents the whole shell
  // (breadcrumb, history, view chords), so it has to open from any route — a
  // preview, panel/tab mode, Preferences, or an unrecognized URL where no
  // Listing is mounted at all. Listing therefore has NO Mod+K binding of its
  // own, which also means the chord can't be handled twice.
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  // Read inside the once-registered listener so it can't re-open an overlay
  // that's already up (while open, ShortcutsOverlay's own handler owns Mod+K
  // and closes it — a stale-closure `false` here would immediately reopen it).
  const shortcutsOpenRef = useRef(false);
  shortcutsOpenRef.current = shortcutsOpen;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.isComposing) return;
      if (!isMod(e) || e.key.toLowerCase() !== "k") return;
      if (shortcutsOpenRef.current) return; // the overlay handles its own close
      // Don't stack the cheat sheet on a dialog, context menu, or preview that
      // already holds the overlay lock — Esc would then close them in the wrong
      // order.
      if (isOverlayOpen()) return;
      e.preventDefault(); // don't let the browser's Ctrl/Cmd+K take it
      setShortcutsOpen(true);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Escape cancels a pending copy/cut. Owned by App, not Listing, for the same
  // reason as Mod+K: the clipboard is a module-level store that outlives any one
  // view, so a copy made in the listing is still pending while you sit in a
  // preview — and there Listing isn't mounted to hear the key at all.
  //
  // CAPTURE phase, deliberately. Listing's own Escape branch (clear selection)
  // must lose to this one, and bubble-phase order can't guarantee that: React
  // flushes effects child-first, so on the initial mount Listing registers its
  // document listener BEFORE App's, and after a navigation (StatView is keyed
  // by epoch+fsPath, so Listing remounts) it registers AFTER — the order
  // literally flips. A capture listener on `document` always runs before every
  // bubble listener on `document`, because the keydown target is the focused
  // element (body at worst), never the document itself. Listing then sees
  // e.defaultPrevented and stands down.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.isComposing || e.key !== "Escape") return;
      // A dialog / context menu / cheat sheet owns Escape while it's up.
      if (isOverlayOpen()) return;
      // Escape inside a text field belongs to that field (the listing's search
      // box clears the query, the crumb path editor discards the edit).
      const el = document.activeElement as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      if (!getClipboard()) return;
      e.preventDefault(); // signals "handled" to Listing's selection branch
      setClipboard(null);
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, []);

  // The Home page is the front door — "/" lands there. Render-time
  // write is safe — it changes pathname, so the re-render (via fused:urlchange)
  // derives the real route. (Legacy /view/_home, /view/_account, and the whole
  // /view//embed namespaces are rewritten at boot by router.ts.)
  if (location.pathname === "/") {
    history.replaceState(null, "", "/home");
  }
  // Legacy: the CLAUDE.md explorer is a section of the Config panel again, so
  // the old page URL folds into it (same render-time rewrite as "/" above)
  // rather than 404ing on someone's bookmark.
  if (location.pathname === "/claude-md") {
    history.replaceState(null, "", "/claude-config?cctab=claudemd");
  }

  const pathname = location.pathname;
  // Via the router's predicate, not a second copy of the two spellings: a pane's
  // Listing asks the SAME question of its host document (IS_PANEL_PANE), and one
  // route must not be spelled in two places.
  const isPanel = isPanelPath(pathname);
  const isTabs = pathname === "/explorer/view/_tab" || pathname === "/explorer/embed/_tab";
  const isPrefs = pathname === "/preferences";
  const isTemplates = pathname === "/templates";
  // PROTOTYPE: mounts page (see shell/Mounts.tsx).
  const isMounts = pathname === "/mounts";
  // Scheduled Claude messages (shell/Scheduled.tsx) — same chrome-free settings
  // pattern as Mounts.
  const isScheduled = pathname === "/scheduled";
  // What the Hugging Face cache holds on this machine (shell/AiModels.tsx).
  const isAiModels = pathname === "/ai-models";
  // Apps hub = the app home: all detected apps with search + tag filters.
  const isApps = pathname === "/apps";
  // File-explorer homepage: the recents/sessions/repos launcher.
  const isExplorerHome = pathname === "/explorer";
  // The app's front door: search hero + the three recency strips.
  const isHome = pathname === "/home";
  const isLearn = pathname === "/learn";
  const isSessions = pathname === "/sessions";
  const isClaudeConfig = pathname === "/claude-config";
  // Canvases: the listing plus the parameterized workspace route. The name is
  // constrained to the CLI's own canvas-name alphabet, so the match below is
  // also the validation.
  const isCanvases = pathname === "/canvases";
  const canvasWorkspaceName = /^\/canvases\/([A-Za-z0-9_]+)$/.exec(pathname)?.[1] ?? null;
  const isBookmark = pathname === "/explorer/view/_bookmark";
  // `/apps/<tag>/<name>` used to resolve HERE, to the app folder under the
  // workspace (a pure fused_dir codec) or — for the virtual "linked" tag, whose
  // folders live anywhere on disk — through GET /api/apps/linked-path, one async
  // hop the route had to hold a blank frame for. Both are gone with the route:
  // an app folder is an ordinary fs path, which /explorer/view/<path> already
  // carries with no lookup at all. Anything under /apps that isn't the hub falls
  // through to the "Unrecognized URL" branch below, deliberately unredirected.
  const isSentinel =
    isPanel || isTabs || isPrefs || isTemplates || isMounts || isScheduled || isAiModels || isApps || isExplorerHome || isHome || isLearn || isSessions || isClaudeConfig || isCanvases || canvasWorkspaceName !== null || isBookmark;
  const fsPath = isSentinel ? null : fsPathFromLocation();
  // Browsing to a `.bookmark` file in the explorer opens it like a Finder
  // double-click (SB-9): same component as the `_bookmark` sentinel, fed the
  // fs path directly — never StatView (the file describes a view, it isn't one).
  const bookmarkFile = fsPath && fsPath.toLowerCase().endsWith(".bookmark") ? fsPath : null;
  // A resolved fsPath mounts StatView below, which owns the title itself.
  useDocumentTitle(
    isPanel
      ? "Panel"
      : isTabs
        ? "Tabs"
        : isPrefs
          ? "Preferences"
          : isTemplates
            ? "Templates"
            : isMounts
              ? "Mounts"
              : isScheduled
                ? "Scheduled messages"
              : isAiModels
                ? "AI Models"
                : isApps
                ? "Apps"
                : isHome
                  ? "Home"
                : isExplorerHome
                  ? "File Explorer"
                  : isLearn
                    ? "Learn"
                    : isSessions
                      ? "Sessions"
                      : isClaudeConfig
                      ? "Claude Config"
                      : isCanvases
                      ? "Canvases"
                      : canvasWorkspaceName
                      ? `Canvas: ${canvasWorkspaceName}`
                      : isBookmark || bookmarkFile
                      ? "Bookmark"
                      : fsPath
                        ? undefined
                        : null
  );

  // First-run onboarding tour: fire after paint so the listing and breadcrumb
  // are mounted (maybeAutoStartTour no-ops in embed / if already seen). Keyed
  // on `pathname`, not mount-once: App never remounts, and a first visit now
  // lands on the chrome-free "/" where there is no #sidebar to point at, so the
  // attempt has to repeat until a route with the shell chrome comes up. The ref
  // stops the retries once the tour has run — otherwise a browser that refuses
  // the "seen" write would restart it on every navigation.
  const tourPending = useRef(true);
  useEffect(() => {
    if (IS_EMBED || !tourPending.current) return;
    const id = setTimeout(() => {
      tourPending.current = !maybeAutoStartTour();
    }, 600);
    return () => clearTimeout(id);
  }, [pathname]);

  // Route fade (A5). Every route hard-remounts on the nav epoch, so a cross-fade
  // between old and new content is impossible — instead #content plays a short
  // fade-in (shell.css) so a navigation cut reads as intentional rather than as
  // a flicker. A CSS animation only replays on a NEWLY CREATED element, and the
  // `<div id="content">` wrappers below outlive an epoch change (only their
  // keyed child remounts), hence `key={epoch}` on each of them. StatView's own
  // #content needs no key: StatView is already keyed on epoch+fsPath.
  //
  // Deliberately keyed on the nav epoch and nothing else: an iframe writing view
  // params bumps useUrlVersion, which re-renders chrome without remounting, so
  // param changes never re-trigger the fade.
  let main;
  if (isPanel) {
    // No title row: a whole 48px bar that said only "Panel" (plus a ★) is
    // 48px of the grid the panes actually need. The ★ moved into each pane
    // bar's left edge (Panel.tsx) — it bookmarks the same `_layout` URL it
    // always did. Nothing portals into #topbar-mode-slot on this route: the
    // panes are /embed iframes, and an embed hides its own breadcrumb.
    main = (
      <div id="content" key={epoch}>
        <Panel key={epoch} config={config} />
      </div>
    );
  } else if (isTabs) {
    main = (
      <>
        <div id="breadcrumb">
          <StaticBreadcrumb label="Tabs" />
        </div>
        <div id="content" key={epoch}>
          <Tabs key={epoch} config={config} />
        </div>
      </>
    );
  } else if (isPrefs) {
    // Preferences (SPEC §20): a shell settings page — no topbar. Bookmark and
    // split actions are explorer concepts and never render outside it.
    main = (
      <div id="content" key={epoch}>
        <Suspense fallback={<RouteFallback />}>
          <Preferences key={epoch} />
        </Suspense>
      </div>
    );
  } else if (isTemplates) {
    // Templates management (TEMPLATE_MGMT_SPEC §3): shell settings page, no
    // topbar.
    main = (
      <div id="content" key={epoch}>
        <Suspense fallback={<RouteFallback />}>
          <Templates key={epoch} />
        </Suspense>
      </div>
    );
  } else if (isMounts) {
    // PROTOTYPE — remote-storage mounts, same chrome-free settings pattern.
    main = (
      <div id="content" key={epoch}>
        <Suspense fallback={<RouteFallback />}>
          <Mounts key={epoch} />
        </Suspense>
      </div>
    );
  } else if (isScheduled) {
    // Scheduled Claude messages — the durable list plus the form that adds to
    // it. Keyed on `epoch` like its neighbours: the page has no URL-held view
    // state of its own, so a remount per navigation is just a fresh read.
    main = (
      <div id="content" key={epoch}>
        <Suspense fallback={<RouteFallback />}>
          <Scheduled key={epoch} />
        </Suspense>
      </div>
    );
  } else if (isCanvases) {
    // Canvases listing — same chrome-free settings pattern as Scheduled.
    main = (
      <div id="content" key={epoch}>
        <Suspense fallback={<RouteFallback />}>
          <Canvases key={epoch} />
        </Suspense>
      </div>
    );
  } else if (canvasWorkspaceName !== null) {
    // Canvas workspace: the embedded workbench + sync strip. Keyed on the
    // canvas name (not epoch): the page holds a live iframe and a token
    // handshake, and its only same-route churn is its own sync poll.
    main = (
      <div id="content">
        <Suspense fallback={<RouteFallback />}>
          <CanvasWorkspace key={canvasWorkspaceName} name={canvasWorkspaceName} />
        </Suspense>
      </div>
    );
  } else if (isAiModels) {
    // AI Models — the Hugging Face cache inventory, in the cc-* page
    // chrome. Reachable by URL even where the sidebar hides its
    // entry (no cache dir yet); the page states that case itself.
    //
    // **Not keyed on `epoch`, unlike every branch around it.** The only
    // same-route navigation this page has is its own Local/Discover toggle,
    // which lives in the URL (`?tab=`) so the back button can undo it — and
    // remounting a page to change its own view state would re-walk every blob
    // in the Hugging Face cache and throw away whatever was typed into
    // Discover's search. The page subscribes to the URL itself instead.
    // Arriving from any other route still mounts it fresh: the branches differ
    // in their children, so React replaces the subtree regardless.
    main = (
      <div id="content">
        <div className="cc-page">
          <Suspense fallback={<RouteFallback />}>
            <AiModels />
          </Suspense>
        </div>
      </div>
    );
  } else if (isApps) {
    // Apps hub — the app home. No breadcrumb bar; the page owns its own
    // header. The shell sidebar renders beside it.
    //
    // Not keyed on `epoch` (same exception as AiModels above): the page's only
    // same-route navigation is its tag filter, which lives in the URL
    // (`?tag=`) so back/forward can undo it — and remounting would reload
    // every app-preview iframe just to switch a chip. The page subscribes to
    // the URL itself. Arriving from any other route still mounts it fresh.
    main = (
      <div id="content">
        <Suspense fallback={<RouteFallback />}>
          <Apps config={config} />
        </Suspense>
      </div>
    );
  } else if (isHome) {
    // The front door: search hero + Fused Apps / Claude Sessions / Recent
    // files strips (shell/Home.tsx).
    main = (
      <div id="content" key={epoch}>
        <Home key={epoch} config={config} />
      </div>
    );
  } else if (isExplorerHome) {
    // File-explorer homepage: the recents/sessions/repos launcher (FilesHome).
    main = (
      <div id="content" key={epoch}>
        <FilesHome key={epoch} config={config} />
      </div>
    );
  } else if (isLearn) {
    // Learn content, chrome-free (LearnView renders a StatView that carries
    // its own #content).
    main = <LearnView key={epoch} config={config} epoch={epoch} />;
  } else if (isSessions) {
    // Claude Sessions inbox, same chrome-free treatment as learn.
    main = <SessionsView key={epoch} config={config} epoch={epoch} />;
  } else if (isClaudeConfig) {
    // Claude Config panel — native, no mount (see ClaudeConfigView).
    main = <ClaudeConfigView key={epoch} />;
  } else if (isBookmark || bookmarkFile) {
    // `.bookmark` open flow (SB-9, D99): Finder double-click lands on the
    // `/view/_bookmark?file=` sentinel; browsing to the file in the explorer
    // renders the same redirector with the fs path as a prop.
    main = (
      <>
        <div id="breadcrumb">
          <StaticBreadcrumb label="Bookmark" />
        </div>
        <div id="content" key={epoch}>
          <Suspense fallback={<RouteFallback />}>
            <BookmarkOpen key={epoch} file={bookmarkFile ?? undefined} />
          </Suspense>
        </div>
      </>
    );
  } else if (!fsPath) {
    main = (
      <>
        <div id="breadcrumb" />
        <div id="content" key={epoch}>
          <div className="status-message error">Unrecognized URL: {pathname}</div>
        </div>
      </>
    );
  } else {
    // Windows expanduser returns backslashes; fsPath is always forward-slash.
    main = (
      <StatView key={epoch + ":" + fsPath} fsPath={fsPath} epoch={epoch} home={config.home.replace(/\\/g, "/")} />
    );
  }

  // ONE sidebar for every route (it replaced the per-context pair: the
  // explorer's on fs routes, the shell app-switcher elsewhere). The shell no
  // longer picks — GlobalSidebar carries nav, recents, bookmarks and the
  // bottom Preferences menu itself.
  const sidebar = <GlobalSidebar config={config} />;

  return (
    <div id="app">
      {!IS_EMBED && sidebar}
      <div id="main">{main}</div>
      {/* ONE work-in-progress card in the notification column, not two: QueueDock
          is the shell's wrapper around the platform activity card (it fills that
          card's queue slot), handed in from here rather than imported there
          because it speaks explorerUrl, which lives in this layer. */}
      <NotificationHost activity={<QueueDock />} />
      {shortcutsOpen && <ShortcutsOverlay onClose={() => setShortcutsOpen(false)} />}
    </div>
  );
}
