// OpenBot's #full live view (live.js renderFullMirrors / renderTabs and the topbar + header controls), shown while
// the store's `fast` flag is on. The socket, frames and input forwarding live in lib/cdp.ts; this renders the
// chrome around them from the store and the link state.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type Bot } from "../lib/api";
import { closeOverlay, gotoTyped, handBack, inFull, installLive, nav, pickerChange, pickSelect, runItem, tabstripClick, toggleCtl, useLinked, useOverlay } from "../lib/cdp";
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

// Our stand-ins for what headless Chrome never paints (lib/cdp.ts overlays): select menu, datalist suggestions, native
// pickers (the viewer's own input of the same type laid over the field) and the context menu.
function Overlays() {
  const o = useOverlay();
  const ref = useRef<HTMLDivElement>(null);
  const inRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!o) return;
    if (o.kind === "select" || o.kind === "context") ref.current?.querySelector<HTMLButtonElement>(o.kind === "select" ? "button[data-sel]" : "button")?.focus();
    if (o.kind === "picker") {
      const el = inRef.current; if (!el) return;
      el.focus();
      try { (el as HTMLInputElement & { showPicker?: () => void }).showPicker?.(); } catch { /* needs a fresh user gesture; the field still takes typing */ }
    }
  }, [o]);
  if (!o) return null;
  // Arrow keys walk the buttons; Escape closes; nothing leaks to the page (the stage listens on keydown too).
  const menuKeys = (e: React.KeyboardEvent) => {
    const items = [...(ref.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") || [])];
    const i = items.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "ArrowDown") { e.preventDefault(); items[Math.min(i + 1, items.length - 1)]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[Math.max(i - 1, 0)]?.focus(); }
    else if (e.key === "Escape") { e.preventDefault(); closeOverlay(); }
    e.stopPropagation();
  };
  if (o.kind === "picker") {
    return <input ref={inRef} className="lvov pickov" type={o.type} defaultValue={o.value} aria-label="Pick a value for the page's field"
      style={{ left: o.left, top: o.top, width: o.width, height: o.height }}
      onChange={(e) => pickerChange(e.currentTarget.value)} onBlur={() => closeOverlay()}
      onKeyDown={(e) => { if (e.key === "Escape") closeOverlay(); e.stopPropagation(); }} />;
  }
  if (o.kind === "context") {
    return (
      <div ref={ref} className="lvov selmenu" role="menu" style={{ left: o.left, top: o.top }} onKeyDown={menuKeys}>
        {o.items.map((it) => <button key={it.label} type="button" role="menuitem" onClick={() => runItem(it)}>{it.label}</button>)}
      </div>
    );
  }
  const list = o.kind === "list";
  return (
    <div ref={ref} className="lvov selmenu" role="listbox" style={{ left: o.left, top: o.top, minWidth: Math.max(o.width, 120) }}
      onKeyDown={list ? undefined : menuKeys} onMouseDown={list ? (e) => e.preventDefault() : undefined /* suggestions: keep typing where you were */}>
      {o.opts.map((opt, i) => {
        const hi = list ? i === o.hi : opt.s;
        return (
          <button key={i} type="button" role="option" aria-selected={hi} disabled={opt.d} data-sel={hi ? "1" : undefined}
            className={hi ? "sel" : undefined} onClick={() => pickSelect(opt.v)}>{opt.t || "\u00a0"}</button>
        );
      })}
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

  useEffect(() => (stageRef.current ? installLive(stageRef.current) : undefined), []);

  // The URL bar mirrors the driven page unless you are typing in it.
  const url = b?.browser?.url;
  useLayoutEffect(() => {
    const f = furlRef.current;
    if (f && b && document.activeElement !== f) f.value = showUrl(url);
  }, [b, url]);

  const ctl = open && !!b?.control && isLinked;
  const headed = !!b?.browser?.headed;
  const [winBusy, setWinBusy] = useState<string | null>(null);  // "Opening…" / "Docking…" while Chrome relaunches (a few seconds)
  // The explicit escape hatch: the same profile as a real Chrome window, for what no screencast carries (passkeys, password
  // manager, print). Chrome relaunches, so it takes a few seconds; the mirror here keeps streaming it meanwhile.
  const onWin = async () => {
    if (!b || winBusy) return;
    const id = b.id;
    setWinBusy(headed ? "Docking…" : "Opening…");
    try { await act(() => headed ? api.dock(id) : api.popout(id)); } finally { setWinBusy(null); }
  };
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

  // .show = the view is open, .nolink = no frames yet (the copied thumbnail shows), .ctl = you drive (accent outline, nav enabled).
  const cls = [open && "show", open && !isLinked && "nolink", ctl && "ctl"].filter(Boolean).join(" ");
  return (
    <div id="full" className={cls}>
      <div className="topbar">
        <button id="giveback2" className="backtxt" title="Back to chat; the bot continues" onClick={() => { void handBack(true); }}>Back</button>
        {b ? <Tabs b={b} /> : <div className="tabstrip" id="tabstrip" />}
        <span className="winacts">
          <button id="fwin" className={winBusy ? "busy" : undefined} onClick={() => { void onWin(); }}
            title={headed ? "Close the desktop window and drive it here again" : "Open this browser as a real Chrome window on your desktop, for passkeys and password managers. Chrome relaunches (a few seconds)."}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4" /><path d="M12 13V7" /><path d="m9 10 3-3 3 3" /></svg>
            <span className="lbl">{winBusy || (headed ? "Back here" : "Real window")}</span>
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
        {/* Keyboard target while you drive (lib/cdp.ts installLive): a hidden textarea, because only an editable element composes
            dead keys and IME input; plain keys are forwarded and never land in it. */}
        <textarea id="fkeys" aria-label="Type into the bot's page" autoComplete="off" autoCapitalize="off" autoCorrect="off" spellCheck={false} tabIndex={-1} />
        <Overlays />
        {/* The drag image for an intercepted HTML5 drag (lib/cdp.ts showGhost): a crop of the frame that follows the pointer. */}
        <img id="fghost" className="lvov ghost" alt="" hidden draggable={false} />
        <Toast id="ftoast" />
      </div>
    </div>
  );
}
