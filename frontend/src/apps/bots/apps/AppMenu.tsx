// The viewer's ⋯ menu (OpenBot apps.js openAppMenu, which reused #cmenu): everything that used to be a toolbar button,
// plus "New task…", a Claude session inside the app's folder that edits it in place (newAppTask). Its own `.cmenu`
// element (#amenu) so the bot menu's outside-click rule (closest("#cmenu")) stays BotMenu's. Positioned under the
// kebab, right-aligned, clamped into the window; closes on any outside click, Escape or window blur.
// (FusedBot's "Pin to menu bar" item is not ported: fused-render has no menu-bar dock.)
import { useEffect, useLayoutEffect, useRef, type ReactNode } from "react";
import { api } from "../lib/api";
import { errMsg, showBanner } from "../state/store";
import { buildsOn, newAppTask } from "../builds/builds";
import { appEmbedBase, appOpenUrl, appStateParams, closeApps, copyAppState, getViewedApp, viewFrameSrc } from "./apps";
import { showAppBeside } from "./side";

const mi = (d: ReactNode) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{d}</svg>
);
const AMI = {
  reload: mi(<><path d="M20 12a8 8 0 1 1-2.34-5.66" /><path d="M20 4v5h-5" /></>),
  copy: mi(<><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></>),
  side: mi(<><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M14 4v16" /></>),
  tab: mi(<><path d="M14 4h6v6" /><path d="M20 4 10 14" /><path d="M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5" /></>),
  task: mi(<path d="M12 5v14M5 12h14" />),
  finder: mi(<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />),
};
type Act = keyof typeof AMI;

function run(a: Act) {
  const app = getViewedApp(); if (!app) return;
  const params = appStateParams();
  switch (a) {
    case "reload": viewFrameSrc(appEmbedBase(app.dir, params)); break;  // reload keeps the state, like a browser does
    case "copy": copyAppState(app); break;
    case "side": closeApps(); showAppBeside({ ...app, params }); break;
    case "tab": window.open(appOpenUrl(app.dir, params), "_blank", "noopener"); break;
    case "finder": api.revealApp(app.dir).catch((e) => showBanner("Could not open Finder: " + errMsg(e))); break;
    case "task": void newAppTask(app); break;
  }
}

/** `anchor` is the kebab's rect while the menu is open, else null. */
export function AppMenu({ anchor, onClose }: { anchor: DOMRect | null; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const m = ref.current; if (!m || !anchor) return;
    const r = m.getBoundingClientRect();
    m.style.left = Math.max(8, Math.min(anchor.right - r.width, innerWidth - r.width - 8)) + "px";
    m.style.top = Math.min(anchor.bottom + 6, innerHeight - r.height - 8) + "px";
  }, [anchor]);
  useEffect(() => {
    if (!anchor) return;
    const onClick = (e: MouseEvent) => { if (!(e.target as Element).closest?.("#amenu")) onClose(); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("click", onClick); document.addEventListener("keydown", onKey); window.addEventListener("blur", onClose);
    return () => { document.removeEventListener("click", onClick); document.removeEventListener("keydown", onKey); window.removeEventListener("blur", onClose); };
  }, [anchor, onClose]);
  const item = (a: Act, label: string) => <button data-a={a} onClick={() => { onClose(); run(a); }}>{AMI[a]}{label}</button>;
  return (
    <div id="amenu" ref={ref} className={`cmenu${anchor ? " show" : ""}`}>
      {anchor ? (<>
        {item("reload", "Reload")}
        {item("copy", "Copy state")}
        <hr />
        {item("side", "Beside chat")}
        {item("tab", "Open in tab")}
        {item("finder", "Open in Finder")}
        {buildsOn() ? <><hr />{item("task", "New task…")}</> : null}
      </>) : null}
    </div>
  );
}
