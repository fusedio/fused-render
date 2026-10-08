// The hand-back row while you hold the bot's browser (`control`): how long you have had it, and the one exit, Done, hand
// back. It closes the bot's last message in the thread: the live hand-over question for a bot-initiated hand-over, or a
// "You're driving" note at the end of the thread for a take-over of yours (HandoverNote).
import { useEffect, useState } from "react";
import type { Bot } from "../lib/api";
import { handBack } from "../lib/cdp";
import { fmtSecs } from "../lib/format";

/** The elapsed pill turns red after this long: the bot is still waiting, maybe forgotten. */
const LATE_S = 600;

export function HandBackRow({ b }: { b: Bot }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const t = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(t); }, []);
  const [busy, setBusy] = useState(false);
  const secs = b.control_since ? Math.max(0, Math.floor(now / 1000 - b.control_since)) : null;
  return (
    <div className="hbrow">
      <button className="primary" disabled={busy}
        title={`Give the browser back; ${b.name} looks at the page again and carries on`}
        onClick={() => { setBusy(true); void handBack(true).finally(() => setBusy(false)); }}>Done, hand back</button>
      {secs != null ? <span className={`pill${secs >= LATE_S ? " late" : ""}`} title="How long you have had the browser">{fmtSecs(secs)}</span> : null}
    </div>
  );
}

/** The thread's last message while you hold the browser and no live hand-over question carries the hand-back row. */
export function HandoverNote({ b }: { b: Bot }) {
  const paused = b.status === "paused" || b.status === "waiting";
  return (
    <div className="ev">
      <div className="msg question handover" role="status">
        {b.control_by === "bot" ? `${b.name} needs you in the browser` : paused ? `You're driving · ${b.name} paused` : "You're driving"}
        <HandBackRow b={b} />
      </div>
    </div>
  );
}
