// Route dispatch (super-app step 2 — shell + three sub-apps):
//   "/"                      -> redirect (replaceState) to /explorer
//   "/apps"                  -> apps homepage (the app home)
//   "/apps/<tag>/<name>"     -> app builder (StatView variant "app")
//   "/explorer"              -> file-explorer homepage (FilesHome)
//   "/explorer/view/<path>"  -> stat it: directory -> listing, file -> preview
//   "/explorer/embed/<path>" -> chrome-free embed variant
//   "/learn"                 -> learn content, chrome-free (variant "learn")
//   "/preferences|/templates|/mounts" -> settings pages
// Legacy pre-rename urls (/view/..., /embed/..., /view/_prefs-family) are
// rewritten in place at boot by router.ts before any of this runs.
// The active view is keyed by the nav epoch: every navigation remounts it,
// which is the React equivalent of the vanilla shell rebuilding the view DOM
// on each route() call (fresh iframes, fresh fetches, dropped local state).
import { useEffect, useRef, useState } from "react";
import { IS_EMBED, appRouteSegments, fsPathFromLocation, fsPathFromAppRoute, navHintIsDir } from "@platform/lib/router";
import { useSessionRestore, useSessionTracking } from "@platform/lib/session";
import { useRecentsTracking } from "@apps/explorer/lib/recents";
import { useAppRecentsTracking } from "@apps/builder/lib/recents";
import { statPath, getLinkedAppPath, getMounts, reconnectMount, type Config, type Mount, type StatResult } from "@platform/lib/api";
import { LINKED_TAG } from "@platform/lib/appEntry";
import { useNavEpoch, useDocumentTitle, useRefreshOnReturn, useLearnMountReady, useSessionsMountReady } from "@platform/lib/hooks";
import { useMountHealth } from "@platform/lib/mountHealth";
import { basename } from "@platform/lib/format";
import { maybeAutoStartTour } from "@platform/lib/tour";
import { useThemeSync } from "@platform/lib/theme";
import ShellSidebar from "@shell/ShellSidebar";
import ExplorerSidebar from "@apps/explorer/sidebar/ExplorerSidebar";
import BuilderSidebar from "@apps/builder/sidebar/BuilderSidebar";
import CloneAppHost from "@platform/cloud/CloneAppHost";
import NotificationHost from "@platform/ui/NotificationHost";
import ShortcutsOverlay from "@platform/ui/ShortcutsOverlay";
import { isMod } from "@platform/lib/platform";
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import { getClipboard, setClipboard } from "@apps/explorer/lib/fs-clipboard";
import { reconcileOsClipboard } from "@apps/explorer/lib/os-clipboard";
import { Breadcrumb, StaticBreadcrumb } from "@apps/explorer/Breadcrumb";
import Listing from "@apps/explorer/Listing";
import Preview from "@apps/explorer/Preview";
import Panel from "@apps/explorer/Panel";
import Tabs from "@apps/explorer/Tabs";
import Preferences from "@shell/Preferences";
import Templates from "@shell/templates/Templates";
import Mounts from "@shell/Mounts";
import Apps from "@apps/builder/Apps";
import FilesHome from "@apps/explorer/FilesHome";
import { learnEntryPath } from "@apps/learn";
import { sessionsEntryPath } from "@apps/sessions";
import BookmarkOpen from "@apps/explorer/BookmarkOpen";

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

// The mode list the app builder pins its views to — the modes that make sense
// over an app folder, in switcher order. `app` (the app itself, full-bleed) is
// first because it is what opening an app lands on; `claude` is where an
// app is built. A mode absent from this list is filtered out of the switcher
// entirely (Preview's allowModes), so this is what makes the plain view
// reachable. The URL's `_mode` semantics are unchanged.
//
// The pin is load-bearing rather than cosmetic: an app folder is still a
// directory, so without it the builder would also offer the other directory
// modes (`git`, `graph`, `zarr_aoi`). It is a curation of this view,
// not a divergence from the explorer — every mode listed here behaves in the
// builder exactly as it does in the explorer for the same folder.
const APP_MODES = ["app", "claude", "history"];

// Stat-backed views (listing/preview): breadcrumb + content under one hook
// component so useStat only runs when the pathname is a real fs path, not a
// sentinel.
//
// `variant` selects the sub-app chrome:
//   "explorer" (default) — breadcrumb, full preview header, file recents.
//   "app"                — no breadcrumb, header kept (mode switcher pinned to
//                          APP_MODES), app recents (needs `fusedDir`).
//   "learn"              — no breadcrumb, no preview header, no recents.
function StatView({
  fsPath,
  epoch,
  home,
  variant = "explorer",
  fusedDir = "",
}: {
  fsPath: string;
  epoch: number;
  home: string;
  variant?: "explorer" | "app" | "learn";
  fusedDir?: string;
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
  // Recents: each sub-app records into its OWN store. Explorer files use the
  // confirmed-file gate (same as session tracking); the app builder records
  // the app folder into apps/builder's store (fusedDir empty = disabled, so
  // learn and explorer never write there). Both hooks are unconditional
  // (hooks rules) and gate internally on their args.
  useRecentsTracking(fsPath, variant === "explorer" ? isDir : null, renderedTitle);
  useAppRecentsTracking(fsPath, variant === "app" ? fusedDir : "", renderedTitle);
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
          allowModes={variant === "app" ? APP_MODES : undefined}
          hideHeader={variant === "learn"}
          actionsInTopbar={variant === "explorer"}
        />
      );
    }
  }
  return (
    <>
      {/* Only the explorer carries a breadcrumb bar — the app builder and
          learn render their content directly (no path chrome). */}
      {variant === "explorer" && (
        <div id="breadcrumb">
          <Breadcrumb fsPath={fsPath} home={home} renderedTitle={renderedTitle} />
        </div>
      )}
      <div id="content">{content}</div>
    </>
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

export default function App({ config }: { config: Config }) {
  const epoch = useNavEpoch();

  // Background mount-health poll → global disconnect/reconnect toasts. Mounted
  // once here for the page's lifetime (no-ops in embed); renders via NotificationHost.
  useMountHealth();

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

  // The explorer home is the front door — "/" lands there. Render-time
  // write is safe — it changes pathname, so the re-render (via fused:urlchange)
  // derives the real route. (Legacy /view/_home, /view/_account, and the whole
  // /view//embed namespaces are rewritten at boot by router.ts.)
  if (location.pathname === "/") {
    history.replaceState(null, "", "/explorer");
  }

  const pathname = location.pathname;
  const isPanel = pathname === "/explorer/view/_panel" || pathname === "/explorer/embed/_panel";
  const isTabs = pathname === "/explorer/view/_tab" || pathname === "/explorer/embed/_tab";
  const isPrefs = pathname === "/preferences";
  const isTemplates = pathname === "/templates";
  // PROTOTYPE: mounts page (see shell/Mounts.tsx).
  const isMounts = pathname === "/mounts";
  // Apps hub = the app home: all detected apps with search + tag filters.
  const isApps = pathname === "/apps";
  // File-explorer homepage: the bookmark launcher.
  const isExplorerHome = pathname === "/explorer";
  const isLearn = pathname === "/learn";
  const isSessions = pathname === "/sessions";
  const isBookmark = pathname === "/explorer/view/_bookmark";
  // App-builder route: /apps/<tag>/<name> resolves to the app folder under
  // the workspace — a pure codec, no server lookup (router.fsPathFromAppRoute).
  // EXCEPT the virtual "linked" tag: those folders live anywhere on disk, so
  // the name resolves through the registry (GET /api/apps/linked-path) — one
  // async hop, after which the exact same StatView/app view renders on the
  // real folder. An unknown name (or a fetch failure) falls back to the codec
  // path, which doesn't exist — the same missing-folder card a bad workspace
  // app route gets.
  const fusedDir = config.fused_dir.replace(/\\/g, "/");
  const appSegs = isApps ? null : appRouteSegments(pathname);
  const linkedName = appSegs && appSegs.tag === LINKED_TAG ? appSegs.name : null;
  // The resolved path is keyed to the name it was fetched for: on a
  // linked-to-linked navigation the effect (and its reset) only runs AFTER
  // the first render of the new route, so an unkeyed value would hand the
  // previous app's folder to StatView for a frame.
  const [linkedResolved, setLinkedResolved] =
    useState<{ name: string; path: string } | null>(null);
  const linkedPath =
    linkedResolved && linkedResolved.name === linkedName ? linkedResolved.path : null;
  useEffect(() => {
    if (!linkedName) return;
    let stale = false;
    const fallback = fusedDir.replace(/\/+$/, "") + "/" + LINKED_TAG + "/" + linkedName;
    getLinkedAppPath(linkedName).then(
      (r) => {
        if (!stale)
          setLinkedResolved({
            name: linkedName,
            path: (r.path ?? fallback).replace(/\\/g, "/"),
          });
      },
      () => { if (!stale) setLinkedResolved({ name: linkedName, path: fallback }); }
    );
    return () => { stale = true; };
  }, [linkedName, fusedDir]);
  const appFsPath = linkedName ? linkedPath : fsPathFromAppRoute(pathname, fusedDir);
  // Registry lookup in flight: hold the route (spinner below) instead of
  // letting it fall through to the "Unrecognized URL" branch for a frame.
  const linkedResolving = linkedName !== null && linkedPath === null;
  const isSentinel =
    isPanel || isTabs || isPrefs || isTemplates || isMounts || isApps || isExplorerHome || isLearn || isSessions || isBookmark;
  const fsPath = isSentinel || appFsPath ? null : fsPathFromLocation();
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
              : isApps
                ? "Apps"
                : isExplorerHome
                  ? "File Explorer"
                  : isLearn
                    ? "Learn"
                    : isSessions
                      ? "Sessions"
                      : isBookmark || bookmarkFile
                      ? "Bookmark"
                      : fsPath || appFsPath
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
        <Preferences key={epoch} />
      </div>
    );
  } else if (isTemplates) {
    // Templates management (TEMPLATE_MGMT_SPEC §3): shell settings page, no
    // topbar.
    main = (
      <div id="content" key={epoch}>
        <Templates key={epoch} />
      </div>
    );
  } else if (isMounts) {
    // PROTOTYPE — remote-storage mounts, same chrome-free settings pattern.
    main = (
      <div id="content" key={epoch}>
        <Mounts key={epoch} />
      </div>
    );
  } else if (isApps) {
    // Apps hub — the app home. No breadcrumb bar; the page owns its own
    // header. The shell sidebar renders beside it.
    main = (
      <div id="content" key={epoch}>
        <Apps key={epoch} />
      </div>
    );
  } else if (isExplorerHome) {
    // File-explorer homepage: the bookmark launcher (FilesHome).
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
  } else if (appFsPath) {
    // App builder: the app folder rendered in one of APP_MODES, no breadcrumb
    // (StatView variant "app" carries its own #content).
    main = (
      <StatView
        key={epoch + ":" + appFsPath}
        fsPath={appFsPath}
        epoch={epoch}
        home={config.home.replace(/\\/g, "/")}
        variant="app"
        fusedDir={fusedDir}
      />
    );
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
          <BookmarkOpen key={epoch} file={bookmarkFile ?? undefined} />
        </div>
      </>
    );
  } else if (linkedResolving) {
    // /apps/linked/<name> with the registry lookup still in flight — a blank
    // beat, never the "Unrecognized URL" error for a URL that is about to
    // resolve.
    main = (
      <>
        <div id="breadcrumb" />
        <div id="content" key={epoch} />
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

  // Each sub-app owns its sidebar; the shell only picks which one matches the
  // active route: builder on /apps/<tag>/<name>, explorer on fs-path routes,
  // the shell's own app-switcher everywhere else (homepages, settings, learn).
  const sidebar = appFsPath || linkedResolving ? (
    <BuilderSidebar config={config} />
  ) : fsPath || isPanel || isTabs || isBookmark ? (
    <ExplorerSidebar config={config} />
  ) : (
    <ShellSidebar config={config} />
  );

  return (
    <div id="app">
      {!IS_EMBED && sidebar}
      <div id="main">{main}</div>
      <NotificationHost />
      {/* Opening a deployed app is requested from the path bar (a pasted https:// link) and
          from the Apps page; the modal is mounted HERE so both reach one flow (SPEC §35 CL-1). */}
      {!IS_EMBED && <CloneAppHost />}
      {shortcutsOpen && <ShortcutsOverlay onClose={() => setShortcutsOpen(false)} />}
    </div>
  );
}
