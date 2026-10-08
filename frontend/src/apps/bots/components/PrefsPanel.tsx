// #ppanel: the shell's Preferences page in an iframe, shown while ui.panel === "prefs" — the Builds/Apps panel
// pattern (full window, a top bar with Back, Esc closes). Fused Bot has no shell sidebar, so its gear lives in the
// bot list's footer (BotList.tsx) and lands here; Render reaches Preferences from its own sidebar and never mounts
// this with a door. The frame loads `/preferences?embed=1` once, on the first open, and keeps it: `embed=1` makes the
// framed shell render the page bare (no sidebar, status bar or notifier of its own — platform/lib/router IS_EMBED).
import { ArrowLeftIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { closePanel, useBotsSelector } from "../state/store";

export const PREFS_EMBED_URL = "/preferences?embed=1";

export function PrefsPanel() {
  const open = useBotsSelector((s) => s.ui.panel === "prefs");
  const [src, setSrc] = useState("");
  useEffect(() => { if (open && !src) setSrc(PREFS_EMBED_URL); }, [open, src]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closePanel(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);
  return (
    <div id="ppanel" className={open ? "show" : ""}>
      <div className="topbar">
        <button id="pback" className="backtxt" aria-label="Back" title="Back to bots (Esc)" onClick={closePanel}><ArrowLeftIcon /></button>
        <b className="ttl">Preferences</b>
      </div>
      {src ? <iframe id="pframe" src={src} title="Preferences" /> : null}
    </div>
  );
}
