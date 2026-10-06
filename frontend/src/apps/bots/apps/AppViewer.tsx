// #vpanel (OpenBot index.html + apps.js viewApp/closeView): one app full-size inside this page, above the gallery.
// The iframe is unsandboxed and focusable so the app's own buttons, params and runPython work; apps.ts drives its src
// (viewApp loads it after applyAppParams, closeView blanks it so the app stops running).
import { useCallback, useEffect, useState, type MouseEvent } from "react";
import { closeMenu } from "../state/store";
import { closeView, setViewFrame, useViewedApp } from "./apps";
import { AppMenu } from "./AppMenu";

export function AppViewer() {
  const a = useViewedApp();
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const close = useCallback(() => setAnchor(null), []);
  useEffect(() => { if (!a) setAnchor(null); }, [a]);
  const onMenu = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    if (anchor) { close(); return; }
    closeMenu();
    setAnchor(e.currentTarget.getBoundingClientRect());
  };
  return (
    <>
      <div id="vpanel" className={a ? "show" : ""}>
        <div className="topbar">
          <button id="vback" className="backtxt" title="Back to apps (Esc)" onClick={closeView}>Back</button>
          <b className="ttl" id="vttl">{a ? a.name || a.folder || "App" : "App"}</b><small className="muted" id="vsub">{a ? a.desc || a.dir : ""}</small>
          <span className="winacts">
            <button id="vmenu" className="kebab" title="Reload, copy state, open elsewhere, or start a task that edits this app" aria-label="App actions" onClick={onMenu}>
              <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="2" /><circle cx="12" cy="12" r="2" /><circle cx="19" cy="12" r="2" /></svg>
            </button>
          </span>
        </div>
        <iframe id="vframe" ref={setViewFrame} title="App" />
      </div>
      <AppMenu anchor={a ? anchor : null} onClose={close} />
    </>
  );
}
