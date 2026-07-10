// Route dispatch (the vanilla shell's main.js route()):
//   "/"                  -> redirect (replaceState) to /view/<start-dir>
//   /view|/embed/_panel  -> panel mode (sentinel, intercepted before stat)
//   /view|/embed/_tab    -> tab mode (sentinel)
//   "/view/<path>"       -> stat it: directory -> listing, file -> preview
// The active view is keyed by the nav epoch: every navigation remounts it,
// which is the React equivalent of the vanilla shell rebuilding the view DOM
// on each route() call (fresh iframes, fresh fetches, dropped local state).
import { useEffect, useState } from "react";
import { IS_EMBED, fsPathFromLocation, urlForFsPath } from "./lib/router";
import { useSessionRestore, useSessionTracking } from "./lib/session";
import { statPath, type Config, type StatResult } from "./lib/api";
import { useNavEpoch, useDocumentTitle } from "./lib/hooks";
import { basename } from "./lib/format";
import { maybeAutoStartTour } from "./lib/tour";
import Sidebar from "./components/Sidebar";
import { Breadcrumb, StaticBreadcrumb } from "./components/Breadcrumb";
import Listing from "./views/Listing";
import Preview from "./views/Preview";
import Panel from "./views/Panel";
import Tabs from "./views/Tabs";
import Preferences from "./views/Preferences";
import Templates from "./views/Templates";
import Connectors from "./views/Connectors";

type StatState =
  | { status: "loading" }
  | { status: "ok"; stat: StatResult }
  | { status: "error"; message: string };

function useStat(fsPath: string | null, epoch: number): StatState {
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
  }, [fsPath, epoch]);
  return state;
}

// Stat-backed views (listing/preview): breadcrumb + content under one hook
// component so useStat only runs when the pathname is a real fs path, not a
// sentinel.
function StatView({ fsPath, epoch }: { fsPath: string; epoch: number }) {
  const stat = useStat(fsPath, epoch);
  // null until the stat resolves — the session hooks opt out for anything that
  // is not a confirmed file, so a directory never gets a restore/track before
  // its kind is known.
  const isDir = stat.status === "ok" ? stat.stat.is_dir : null;
  // Per-file session restore (LSN-*): replay the file's last URL query on a
  // bare open, and track qualifying param changes back into the sidecar.
  // `ready` gates the preview so the iframe mounts with the restored params
  // already on the shell URL (no param flash from defaults -> restored).
  const ready = useSessionRestore(fsPath, isDir);
  useSessionTracking(fsPath, isDir);
  useDocumentTitle(fsPath === "/" ? null : basename(fsPath));
  let content = null;
  if (stat.status === "error") {
    content = (
      <div className="status-message error">
        Failed to stat {fsPath}: {stat.message}
      </div>
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
      // synchronously (useSessionRestore), so no flash on those paths.
      content = <div className="status-message">Loading…</div>;
    } else {
      content = <Preview fsPath={fsPath} stat={s} />;
    }
  }
  return (
    <>
      <div id="breadcrumb">
        <Breadcrumb fsPath={fsPath} />
      </div>
      <div id="content">{content}</div>
    </>
  );
}

export default function App({ config }: { config: Config }) {
  const epoch = useNavEpoch();

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

  const pathname = location.pathname;
  const isPanel = pathname === "/view/_panel" || pathname === "/embed/_panel";
  const isTabs = pathname === "/view/_tab" || pathname === "/embed/_tab";
  const isPrefs = pathname === "/view/_prefs";
  const isTemplates = pathname === "/view/_templates";
  // PROTOTYPE: connectors sentinel (see views/Connectors.tsx).
  const isConnectors = pathname === "/view/_connectors";
  const fsPath =
    isPanel || isTabs || isPrefs || isTemplates || isConnectors ? null : fsPathFromLocation();
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
            : isConnectors
              ? "Connectors"
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
  } else if (isConnectors) {
    // PROTOTYPE — remote-mount connectors, same sentinel pattern as _prefs.
    main = (
      <>
        <div id="breadcrumb">
          <StaticBreadcrumb label="Connectors" />
        </div>
        <div id="content">
          <Connectors key={epoch} />
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
    main = <StatView key={epoch + ":" + fsPath} fsPath={fsPath} epoch={epoch} />;
  }

  return (
    <div id="app">
      {!IS_EMBED && <Sidebar config={config} />}
      <div id="main">{main}</div>
    </div>
  );
}
