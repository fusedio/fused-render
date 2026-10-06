// The bottom button above the composer (OpenBot #tobottom): a round arrow while scrolled up; an accent "N new messages"
// pill while unread messages sit below you. Arrow or pill, a click goes to the very end and retires the count.
import type { RefObject } from "react";
import { setNewCount, useBotsSelector } from "../state/store";
import { pinToEnd, updateToBottom, viewAll } from "./threadDom";

export interface ToBottomProps { threadRef: RefObject<HTMLDivElement> }

export function ToBottom({ threadRef }: ToBottomProps) {
  const has = useBotsSelector((s) => !!s.sel && s.bots.some((b) => b.id === s.sel));
  const n = useBotsSelector((s) => s.newCount), pinned = useBotsSelector((s) => s.pinned);
  const sel = useBotsSelector((s) => s.sel);
  const fresh = has && !!n;
  return (
    <button className={`tobottom${fresh ? " new" : ""}${has && (fresh || !pinned) ? " show" : ""}`} id="tobottom" type="button"
      title="Jump to the latest message" aria-label="Jump to the latest message"
      onClick={() => {
        const th = threadRef.current;
        if (sel) viewAll(sel);
        setNewCount(0);
        // A jump, not a glide: a smooth scroll over a long thread is still animating when the next message lands, and the
        // render it triggers would hold your place mid-flight and strand you there. Landing immediately also marks you pinned.
        if (th) { pinToEnd(th, th.dataset.bot); updateToBottom(th); }
      }}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 5v14" /><path d="m19 12-7 7-7-7" /></svg>
      <span id="tobottomn">{fresh ? `${n} new message${n > 1 ? "s" : ""}` : ""}</span>
    </button>
  );
}
