// Route dispatch (the vanilla shell's main.js route()):
//   "/"                  -> redirect (replaceState) to /view/<start-dir>
//   /view|/embed/_panel  -> panel mode (sentinel, intercepted before stat)
//   /view|/embed/_tab    -> tab mode (sentinel)
//   "/view/<path>"       -> stat it: directory -> listing, file -> preview
// The active view is keyed by the nav epoch: every navigation remounts it,
// which is the React equivalent of the vanilla shell rebuilding the view DOM
// on each route() call (fresh iframes, fresh fetches, dropped local state).
import React, { useEffect, useState } from "react";
import { IS_EMBED, fsPathFromLocation, urlForFsPath } from "./lib/router.js";
import { statPath } from "./lib/api.js";
import { useNavEpoch } from "./lib/hooks.js";
import Sidebar from "./components/Sidebar.jsx";
import { Breadcrumb, StaticBreadcrumb } from "./components/Breadcrumb.jsx";
import Listing from "./views/Listing.jsx";
import Preview from "./views/Preview.jsx";
import Panel from "./views/Panel.jsx";
import Tabs from "./views/Tabs.jsx";

function useStat(fsPath, epoch) {
  const [state, setState] = useState({ status: "loading" });
  useEffect(() => {
    if (!fsPath) {
      setState({ status: "loading" });
      return;
    }
    let alive = true;
    setState({ status: "loading" });
    statPath(fsPath).then(
      (stat) => alive && setState({ status: "ok", stat }),
      (err) => alive && setState({ status: "error", message: err.message })
    );
    return () => {
      alive = false;
    };
  }, [fsPath, epoch]);
  return state;
}

// Stat-backed views (listing/preview). Split out so useStat only runs when
// the pathname is a real fs path, not a sentinel.
function StatRoute({ fsPath, epoch, config }) {
  const stat = useStat(fsPath, epoch);
  if (stat.status === "loading") {
    return { crumb: <Breadcrumb fsPath={fsPath} />, content: null };
  }
  if (stat.status === "error") {
    return {
      crumb: <Breadcrumb fsPath={fsPath} />,
      content: (
        <div className="status-message error">
          Failed to stat {fsPath}: {stat.message}
        </div>
      ),
    };
  }
  return {
    crumb: <Breadcrumb fsPath={fsPath} />,
    content: stat.stat.is_dir ? (
      <Listing fsPath={fsPath} />
    ) : (
      <Preview fsPath={fsPath} stat={stat.stat} config={config} />
    ),
  };
}

// A component wrapper so StatRoute can use hooks while App picks routes
// with plain conditionals.
function StatView({ fsPath, epoch, config }) {
  const { crumb, content } = StatRoute({ fsPath, epoch, config });
  return (
    <>
      <div id="breadcrumb">{crumb}</div>
      <div id="content">{content}</div>
    </>
  );
}

export default function App({ config }) {
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
      main = <StatView key={epoch + ":" + fsPath} fsPath={fsPath} epoch={epoch} config={config} />;
    }
  }

  return (
    <div id="app">
      {!IS_EMBED && <Sidebar config={config} />}
      <div id="main">{main}</div>
    </div>
  );
}
