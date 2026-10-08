// The pinned request card at the top of the chat column while you hold the bot's browser (`control`): what the bot
// asked for (the live question, rewritten for the rail) or "You're driving" for a take-over of yours, how long you
// have had it, and the one exit, Done, hand back. Outside the thread's scroll, so it stays put while messages arrive.
import { useEffect, useState } from "react";
import type { Bot } from "../lib/api";
import { handBack } from "../lib/cdp";
import { handoverAsk } from "../lib/derive";
import { fmtSecs } from "../lib/format";
import { eventsOf } from "../state/store";

/** The elapsed pill turns red after this long: the bot is still waiting, maybe forgotten. */
const LATE_S = 600;

export function HandoverCard({ b }: { b: Bot }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const t = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(t); }, []);
  const [busy, setBusy] = useState(false);
  const secs = b.control_since ? Math.max(0, Math.floor(now / 1000 - b.control_since)) : null;
  const byBot = b.control_by === "bot";
  const ask = byBot ? handoverAsk(b, eventsOf(b.id)) : "";
  return (
    <div className={`handover${byBot ? " bybot" : " byuser"}`} role="status">
      <div className="hd">
        <span className="ttl">{byBot ? ask || `${b.name} needs you in the browser` : b.status === "paused" || b.status === "waiting" ? `You're driving · ${b.name} paused` : "You're driving"}</span>
        {secs != null ? <span className={`pill${secs >= LATE_S ? " late" : ""}`} title="How long you have had the browser">{fmtSecs(secs)}</span> : null}
      </div>
      <button className="primary" disabled={busy}
        title={`Give the browser back; ${b.name} looks at the page again and carries on`}
        onClick={() => { setBusy(true); void handBack(true).finally(() => setBusy(false)); }}>Done, hand back</button>
    </div>
  );
}
