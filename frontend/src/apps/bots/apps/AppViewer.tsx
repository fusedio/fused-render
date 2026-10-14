// /bots/apps/<folder> (routes.ts; OpenBot's #vpanel): one app full-size inside this page, above the gallery. The
// iframe is unsandboxed and focusable so the app's own buttons, params and runPython work. Its src is THIS
// component's state: the app's embed URL with the params the host URL carried when the page opened (viewApp wrote
// them there before navigating; the runtime keeps syncing later changes onto the host URL). Reload re-reads those
// params and remounts the frame, like a browser does. Unmounting (Back, another route) stops the app.
import { ArrowLeftIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type MouseEvent } from "react";
import { closeMenu } from "../state/store";
import { appEmbedBase, appStateParams, closeView, setViewerOpen, type AppLike } from "./apps";
import { AppMenu } from "./AppMenu";

export function AppViewer({ app }: { app: AppLike }) {
  const [frame, setFrame] = useState(() => ({ n: 0, src: appEmbedBase(app.dir, appStateParams()) }));
  const reload = useCallback(() => setFrame((f) => ({ n: f.n + 1, src: appEmbedBase(app.dir, appStateParams()) })), [app.dir]);
  // Another app on the same route (a card click while viewing): a fresh frame for it. Not on mount — the initial
  // state already holds this app, and a bump here would load the frame twice.
  const dirWas = useRef(app.dir);
  useEffect(() => {
    if (dirWas.current === app.dir) return;
    dirWas.current = app.dir;
    setFrame((f) => ({ n: f.n + 1, src: appEmbedBase(app.dir, appStateParams()) }));
  }, [app.dir]);
  useEffect(() => { setViewerOpen(true); return () => setViewerOpen(false); }, []);
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const close = useCallback(() => setAnchor(null), []);
  const onMenu = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    if (anchor) { close(); return; }
    closeMenu();
    setAnchor(e.currentTarget.getBoundingClientRect());
  };
  return (
    <>
      <div id="vpanel" className="show">
        <div className="topbar">
          <button id="vback" className="backtxt" aria-label="Back" title="Back to apps (Esc)" onClick={closeView}><ArrowLeftIcon /></button>
          <b className="ttl" id="vttl">{app.name || app.folder || "App"}</b><small className="muted" id="vsub">{app.desc || app.dir}</small>
          <span className="winacts">
            <button id="vmenu" className="kebab" title="Reload, copy state, open elsewhere, or start a task that edits this app" aria-label="App actions" onClick={onMenu}>
              <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="2" /><circle cx="12" cy="12" r="2" /><circle cx="19" cy="12" r="2" /></svg>
            </button>
          </span>
        </div>
        <iframe id="vframe" key={frame.n} src={frame.src} title="App" />
      </div>
      <AppMenu app={app} anchor={anchor} onClose={close} onReload={reload} />
    </>
  );
}
