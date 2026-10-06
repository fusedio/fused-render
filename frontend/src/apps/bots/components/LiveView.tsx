// OpenBot's #full live view (live.js renderFullMirrors / renderTabs and the topbar + header controls), shown while
// the store's `fast` flag is on. The socket, frames and input forwarding live in lib/cdp.ts; this renders the
// chrome around them from the store and the link state.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type Bot } from "../lib/api";
import { gotoTyped, handBack, inFull, installLive, nav, tabstripClick, toggleCtl, useLinked } from "../lib/cdp";
import { statusLabel } from "../lib/derive";
import { showUrl } from "../lib/live";
import { act, eventsOf, useBotsSelector } from "../state/store";
import { Toast } from "./Toast";

// Tab strip: shown with more than one tab or while you are in control; switching follows the bot's own driven tab.
function Tabs({ b }: { b: Bot }) {
  const tabs = b.browser?.tabs || [];
  const show = tabs.length > 1 || (!!b.control && tabs.length > 0);
  return (
    <div className={`tabstrip${show ? " show" : ""}`} id="tabstrip" onClick={(e) => { void tabstripClick(e.target as Element); }}>
      {show ? (
        <>
          {tabs.map((t) => (
            <div key={t.id || t.i} className={`tab ${t.active ? "active" : ""}`} data-i={t.i} title={t.url}>
              <span>{t.title || t.url || "New tab"}</span>
              {tabs.length > 1 ? <span className="x" data-close={t.i} title="Close tab">×</span> : null}
            </div>
          ))}
          <div className="newtab" data-new="1" title="New tab">+</div>
        </>
      ) : null}
    </div>
  );
}

export function LiveView() {
  const open = useBotsSelector((s) => s.fast);
  const b = useBotsSelector((s) => s.bots.find((x) => x.id === s.sel));
  const evs = useBotsSelector((s) => eventsOf(s.sel));
  const isLinked = useLinked();
  const stageRef = useRef<HTMLDivElement>(null);
  const furlRef = useRef<HTMLInputElement>(null);
  const [winBusy, setWinBusy] = useState<string | null>(null);  // the pop-out button's "Opening…" / "Docking…" while it works

  useEffect(() => (stageRef.current ? installLive(stageRef.current) : undefined), []);

  // The URL bar mirrors the driven page unless you are typing in it.
  const url = b?.browser?.url;
  useLayoutEffect(() => {
    const f = furlRef.current;
    if (f && b && document.activeElement !== f) f.value = showUrl(url);
  }, [b, url]);

  const ctl = open && !!b?.control && isLinked;
  const vis = !!b?.browser?.visible;
  // Status strip: while you drive it says so; otherwise the bot's state plus its latest thought, action or harness note.
  let fstat = "";
  if (b) {
    let last: (typeof evs)[number] | undefined;
    for (let i = evs.length - 1; i >= 0; i--) if (evs[i].role === "thought" || evs[i].role === "action" || evs[i].role === "note") { last = evs[i]; break; }
    fstat = !isLinked && open
      ? (b.browser?.running ? "Connecting to the browser…" : "Browser is asleep · waking it…")
      : b.control ? "You're driving · bot paused"
      : statusLabel(b) + (last && b.status === "running" ? " · " + last.text : "");
  }

  // Same pop-out as the bot menu's "Open in a Chrome window": Chrome relaunches visible on the desktop (a few seconds); the live view keeps mirroring it.
  const onWin = async () => {
    if (!b || winBusy) return;
    const was = vis, id = b.id;
    setWinBusy(was ? "Docking…" : "Opening…");
    try { await act(() => api.window(id, !was)); } finally { setWinBusy(null); }
  };

  // .show = the view is open, .nolink = no frames yet (the copied thumbnail shows), .ctl = you drive (accent outline, nav enabled).
  const cls = [open && "show", open && !isLinked && "nolink", ctl && "ctl"].filter(Boolean).join(" ");
  return (
    <div id="full" className={cls}>
      <div className="topbar">
        <button id="giveback2" className="backtxt" title="Back to chat; the bot continues" onClick={() => { void handBack(true); }}>Back</button>
        {b ? <Tabs b={b} /> : <div className="tabstrip" id="tabstrip" />}
        <span className="winacts">
          <button id="fwin" className={winBusy ? "busy" : undefined} onClick={() => { void onWin(); }}
            title={vis ? "Close the desktop window and drive it headless here again" : "Pop this bot's browser out as a real Chrome window on your desktop"}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4" /><path d="M12 13V7" /><path d="m9 10 3-3 3 3" /></svg>
            <span className="lbl">{winBusy || (vis ? "Bring back here" : "Open in browser")}</span>
          </button>
          <button id="ctl" className="primary" onClick={() => { void toggleCtl(); }}
            title={b?.control ? "Let the bot drive again" : "Pause the bot and drive this page yourself"}>
            <span className="lbl">{b?.control ? "Hand back" : "Take over"}</span>
          </button>
          <button id="giveback" title="Back to chat; the bot continues" onClick={() => { void handBack(true); }}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M20 4l-6 6M20 10h-6V4" /><path d="M4 20l6-6M4 14h6v6" /></svg>
          </button>
        </span>
      </div>
      <header>
        <span className={`dot ${b?.status || ""}`} id="fdot" />
        <span className="navwrap" id="navwrap" style={{ display: "flex", flex: 1, gap: 8, alignItems: "center" }}>
          <button id="nback" className="navctl" title="Back (Alt+← or ⌘[)" onClick={() => nav("back")}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          </button>
          <button id="nfwd" className="navctl" title="Forward (Alt+→ or ⌘])" onClick={() => nav("forward")}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg>
          </button>
          <button id="nreload" className="navctl" title="Reload (⌘R)" onClick={() => nav("reload")}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.34-5.66" /><path d="M20 4v5h-5" /></svg>
          </button>
          <input id="furl" ref={furlRef} className="navctl" placeholder="Take over to navigate this bot's browser"
            onKeyDown={(e) => { if (e.key === "Enter" && inFull()) void gotoTyped(e.currentTarget.value); }} />
        </span>
        <span className="fstat" id="fstat" title="What the bot is doing">{fstat}</span>
      </header>
      <div className="stage" id="stage" tabIndex={0} ref={stageRef}>
        <img id="fshot" alt="" draggable={false} />
        <Toast id="ftoast" />
      </div>
    </div>
  );
}
