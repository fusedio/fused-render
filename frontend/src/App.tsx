// Route dispatch (the vanilla shell's main.js route()):
//   "/"                  -> redirect (replaceState) to /view/<start-dir>
//   /view|/embed/_panel  -> panel mode (sentinel, intercepted before stat)
//   /view|/embed/_tab    -> tab mode (sentinel)
//   "/view/<path>"       -> stat it: directory -> listing, file -> preview
// The active view is keyed by the nav epoch: every navigation remounts it,
// which is the React equivalent of the vanilla shell rebuilding the view DOM
// on each route() call (fresh iframes, fresh fetches, dropped local state).
import { useEffect, useState } from "react";
import { IS_EMBED, fsPathFromLocation, urlForFsPath, navHintIsDir } from "./lib/router";
import { useSessionRestore, useSessionTracking } from "./lib/session";
import { useRecentsTracking } from "./lib/recents";
import { statPath, getMounts, reconnectMount, type Config, type Mount, type StatResult } from "./lib/api";
import { useNavEpoch, useDocumentTitle } from "./lib/hooks";
import { useMountHealth } from "./lib/mountHealth";
import { basename } from "./lib/format";
import { maybeAutoStartTour } from "./lib/tour";
import Sidebar from "./components/Sidebar";
import ToastHost from "./components/ToastHost";
import ServerStatusBanner from "./components/ServerStatusBanner";
import { Breadcrumb, StaticBreadcrumb } from "./components/Breadcrumb";
import Listing from "./views/Listing";
import Preview from "./views/Preview";
import Panel from "./views/Panel";
import Tabs from "./views/Tabs";
import Preferences from "./views/Preferences";
import Templates from "./views/Templates";
import Mounts from "./views/Mounts";
import BookmarkOpen from "./views/BookmarkOpen";

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
function LoadingScaffold({ fsPath, isDir }: { fsPath: string; isDir: boolean }) {
  return (
    <>
      <div className="preview-header">
        <h1 title={fsPath}>{basename(fsPath)}</h1>
        <div className="preview-actions">
          <span className="mode-icon-spinner" aria-label="Loading" />
        </div>
      </div>
      <div className="preview-body">
        {isDir ? (
          // provisional: the hint could be stale (file, not dir). Suppress
          // Listing's hard "Failed to list" error while stat resolves — a 404
          // here just means the hint was wrong; stat will paint the file view.
          <Listing fsPath={fsPath} provisional />
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

// Stat-backed views (listing/preview): breadcrumb + content under one hook
// component so useStat only runs when the pathname is a real fs path, not a
// sentinel.
function StatView({ fsPath, epoch, home }: { fsPath: string; epoch: number; home: string }) {
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
  // Sidebar "Recents": record the open, then keep the entry's url (and its
  // title, once known) live as params/title change — same confirmed-file
  // gate as session tracking.
  useRecentsTracking(fsPath, isDir, renderedTitle);
  let content = null;
  if (stat.status === "loading") {
    // Not a blank screen: paint the scaffold immediately (Fix #1). A directory
    // nav also starts its listing fetch now, parallel with stat (Fix #2).
    content = <LoadingScaffold fsPath={fsPath} isDir={navIsDir === true} />;
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
      content = <Listing fsPath={fsPath} />;
    } else if (!ready) {
      // Brief; only for files opened with an empty query while the sidecar
      // read resolves. Directories and param/bookmark opens are ready
      // synchronously (useSessionRestore), so no flash on those paths. Paint
      // the same file scaffold as the stat-loading branch (header + spinner in
      // the file's chrome) rather than a bare centered "Loading…" — on a cold
      // mount this wait is ~2s and must never read as a blank/black screen.
      content = <LoadingScaffold fsPath={fsPath} isDir={false} />;
    } else {
      content = <Preview fsPath={fsPath} stat={s} onRenderedTitle={setRenderedTitle} />;
    }
  }
  return (
    <>
      <div id="breadcrumb">
        <Breadcrumb fsPath={fsPath} home={home} renderedTitle={renderedTitle} />
      </div>
      <div id="content">{content}</div>
    </>
  );
}

export default function App({ config }: { config: Config }) {
  const epoch = useNavEpoch();

  // Background mount-health poll → global disconnect/reconnect toasts. Mounted
  // once here for the page's lifetime (no-ops in embed); renders via ToastHost.
  useMountHealth();

  // First-run onboarding tour: fire once after first paint so the listing and
  // breadcrumb are mounted (maybeAutoStartTour no-ops in embed / if already
  // seen). Empty deps — App mounts once for the page's lifetime.
  useEffect(() => {
    if (IS_EMBED) return;
    const id = setTimeout(() => maybeAutoStartTour(), 600);
    return () => clearTimeout(id);
  }, []);

  // Root redirect, exactly like the vanilla route(): replaceState so "/"
  // never enters history. Render-time write is safe — it changes pathname,
  // so the re-render (via fused:urlchange) derives the real route.
  if (location.pathname === "/") {
    history.replaceState(null, "", urlForFsPath(config.start_dir));
  }
  // The old standalone Fused-account page folded into Preferences as a tab
  // (D125) — redirect its sentinel the same render-time way so existing
  // bookmarks and the Deploy modal's "Set up hosted environment" link still
  // land somewhere real instead of a dead route.
  if (location.pathname === "/view/_account") {
    history.replaceState(null, "", "/view/_prefs?tab=account");
  }

  const pathname = location.pathname;
  const isPanel = pathname === "/view/_panel" || pathname === "/embed/_panel";
  const isTabs = pathname === "/view/_tab" || pathname === "/embed/_tab";
  const isPrefs = pathname === "/view/_prefs";
  const isTemplates = pathname === "/view/_templates";
  // PROTOTYPE: mounts sentinel (see views/Mounts.tsx).
  const isMounts = pathname === "/view/_mounts";
  const isBookmark = pathname === "/view/_bookmark";
  const fsPath =
    isPanel || isTabs || isPrefs || isTemplates || isMounts || isBookmark
      ? null
      : fsPathFromLocation();
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
              : isBookmark || bookmarkFile
                ? "Bookmark"
                : fsPath
                  ? undefined
                  : null
  );

  let main;
  if (isPanel) {
    main = (
      <>
        <div id="breadcrumb">
          <StaticBreadcrumb label="Panel" />
        </div>
        <div id="content">
          <Panel key={epoch} config={config} />
        </div>
      </>
    );
  } else if (isTabs) {
    main = (
      <>
        <div id="breadcrumb">
          <StaticBreadcrumb label="Tabs" />
        </div>
        <div id="content">
          <Tabs key={epoch} config={config} />
        </div>
      </>
    );
  } else if (isPrefs) {
    // Preferences (SPEC §20): a sentinel pathname like _panel/_tab — not a
    // file; entered from the sidebar's gear. /view only (no embed variant —
    // settings chrome inside a pane makes no sense).
    main = (
      <>
        <div id="breadcrumb">
          <StaticBreadcrumb label="Preferences" />
        </div>
        <div id="content">
          <Preferences key={epoch} />
        </div>
      </>
    );
  } else if (isTemplates) {
    // Templates management (TEMPLATE_MGMT_SPEC §3): a sentinel pathname like
    // _prefs — not a file; entered from the sidebar footer. /view only.
    main = (
      <>
        <div id="breadcrumb">
          <StaticBreadcrumb label="Templates" />
        </div>
        <div id="content">
          <Templates key={epoch} />
        </div>
      </>
    );
  } else if (isMounts) {
    // PROTOTYPE — remote-storage mounts, same sentinel pattern as _prefs.
    main = (
      <>
        <div id="breadcrumb">
          <StaticBreadcrumb label="Mounts" />
        </div>
        <div id="content">
          <Mounts key={epoch} />
        </div>
      </>
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
        <div id="content">
          <BookmarkOpen key={epoch} file={bookmarkFile ?? undefined} />
        </div>
      </>
    );
  } else if (!fsPath) {
    main = (
      <>
        <div id="breadcrumb" />
        <div id="content">
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

  return (
    <div id="app">
      {!IS_EMBED && <Sidebar config={config} />}
      <div id="main">{main}</div>
      <ToastHost />
      {!IS_EMBED && <ServerStatusBanner />}
    </div>
  );
}
