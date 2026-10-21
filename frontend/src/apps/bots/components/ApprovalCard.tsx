import type { BotEvent } from "../lib/api";
import { severityLabel } from "../lib/risk";

/** The approval card: risk pill, the model's one-sentence summary, the raw command (muted), then Approve, Always approve, Deny.
 *  The thread's delegated click handler reads the data-* hooks and `.msg.approval:not(.settled)`; a settled card keeps the
 *  pill and summary, only the buttons hide (CSS). */
export function ApprovalCard({ e, live, title }: { e: BotEvent; live: boolean; title?: string }) {
  const label = severityLabel(e.severity);
  const why = (e.risk_summary || "").trim();
  return (
    <div className={`msg approval${live ? "" : " settled"}`} data-seq={e.seq} title={title}>
      {label ? <div className="riskhead"><span className={`riskpill ${e.severity}`}>{label}</span></div> : null}
      {why ? <div className="risksum">{why}</div> : null}
      <div className={why ? "riskraw" : undefined}>{e.text}</div>
      <div className="btns">
        <button className="primary" data-approve="1" disabled={!live}>Approve</button>
        {e.forever ? (
          // A yes that also writes the card's rule (bot.json allow_rules), so this bot never asks again when it does the
          // same thing. The label is "Always approve"; the text SENT stays "Approve, don't ask again" with forever=true
          // (the backend's YES/Forever match depends on it). Only cards whose call can be remembered carry `forever`.
          <button className="forever" data-forever="1" disabled={!live} title={`Never ask again when this bot ${e.forever.label}. Undo it in Settings › Permissions.`}>
            Always approve
          </button>
        ) : null}
        <button data-deny="1" disabled={!live}>Deny</button>
      </div>
    </div>
  );
}
