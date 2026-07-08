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
import { statPath, type Config, type StatResult } from "./lib/api";
import { useNavEpoch } from "./lib/hooks";
import Sidebar from "./components/Sidebar";
import { Breadcrumb, StaticBreadcrumb } from "./components/Breadcrumb";
import Listing from "./views/Listing";
import Preview from "./views/Preview";
import Panel from "./views/Panel";
import Tabs from "./views/Tabs";

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
    // `["_listing"]` (D78), so the built-in listing is now the `_listing`
    // sentinel mode and flows through Preview like any other mode (Preview
    // renders the shell Listing component for it). A directory resolves to an
    // empty list only when a `null` binding disables it; the shell still lists
    // it then — a folder must always render something.
    const s = stat.stat;
    content =
      s.is_dir && s.templates.length === 0 ? (
        <Listing fsPath={fsPath} />
      ) : (
        <Preview fsPath={fsPath} stat={s} />
      );
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

  // Root redirect, exactly like the vanilla route(): replaceState so "/"
  // never enters history. Render-time write is safe — it changes pathname,
  // so the re-render (via fused:urlchange) derives the real route.
  if (location.pathname === "/") {
    history.replaceState(null, "", urlForFsPath(config.start_dir));
  }

  const pathname = location.pathname;
  let main;
  if (pathname === "/view/_panel" || pathname === "/embed/_panel") {
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
  } else if (pathname === "/view/_tab" || pathname === "/embed/_tab") {
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
  } else {
    const fsPath = fsPathFromLocation();
    if (!fsPath) {
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
  }

  return (
    <div id="app">
      {!IS_EMBED && <Sidebar config={config} />}
      <div id="main">{main}</div>
    </div>
  );
}
