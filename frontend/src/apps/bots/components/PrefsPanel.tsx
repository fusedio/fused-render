// #ppanel: the shell's Preferences page in an iframe, shown while ui.panel === "prefs" — the Builds/Apps panel
// pattern (full window, a top bar with Back, Esc closes). Fused Bot has no shell sidebar, so its gear lives in the
// bot list's footer (BotList.tsx) and lands here; Render reaches Preferences from its own sidebar and never mounts
// this with a door. `embed=1` makes the framed shell render the page bare (no sidebar, status bar or notifier of its
// own — platform/lib/router IS_EMBED).
//
// THE FRAME IS LOADED FRESH ON EVERY OPEN and dropped on close (Bugbot, #1504): Preferences links out (the Phone
// row to /bots?phone=1, AI to /ai-models…), and a frame kept across opens would reopen on whatever page the
// reader last navigated to, inside a panel titled Preferences. Esc is listened for on the frame's own document
// too (same origin), since a key pressed in a focused field inside the page never reaches the parent document.
import { ArrowLeftIcon } from "lucide-react";
import { useEffect, useRef } from "react";
import { MONITOR_FLAG_MESSAGE, publishMonitorEnabled } from "@platform/lib/monitor-flag";
import { closePanel, useBotsSelector } from "../state/store";

export const PREFS_EMBED_URL = "/preferences?embed=1";

export function PrefsPanel() {
  const open = useBotsSelector((s) => s.ui.panel === "prefs");
  const frame = useRef<HTMLIFrameElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closePanel(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);
  // The framed page's Monitor toggle (Preferences › Monitor) gates the System chip in THIS document's status bar:
  // the flag module is per document, so the frame posts the change up and it is republished here.
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (e.origin !== location.origin || e.source !== frame.current?.contentWindow) return;
      const d = e.data as { type?: unknown; enabled?: unknown } | null;
      if (d && d.type === MONITOR_FLAG_MESSAGE && typeof d.enabled === "boolean") publishMonitorEnabled(d.enabled);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);
  // Esc inside the frame: bind on each load (a navigation inside the frame replaces its document).
  const onFrameLoad = () => {
    const doc = frame.current?.contentDocument;
    if (!doc) return;
    doc.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });
  };
  return (
    <div id="ppanel" className={open ? "show" : ""}>
      <div className="topbar">
        {/* The title lives up here like Builds/Apps; the framed page drops its own heading under embed=1 (shell/Preferences.tsx). */}
        <button id="pback" className="backtxt" aria-label="Back" title="Back to bots (Esc)" onClick={closePanel}><ArrowLeftIcon /></button>
        <b className="ttl">Preferences</b>
      </div>
      {open ? <iframe id="pframe" ref={frame} src={PREFS_EMBED_URL} title="Preferences" onLoad={onFrameLoad} /> : null}
    </div>
  );
}
