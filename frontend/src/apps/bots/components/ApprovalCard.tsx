import { approvalParts } from "@platform/lib/approvalParts";
import { approvalVerdict } from "@platform/lib/risk";
import { GateCard, type GateStatus } from "@platform/ui/GateCard";

import type { BotEvent } from "../lib/api";

/** The settled card's caption, from the user line that answered it (null while that line is unknown). */
function verdictStatus(answer: string | null | undefined): GateStatus | undefined {
  const v = approvalVerdict(answer);
  return v === "Approved" ? { tone: "allow", text: "✓ Approved" } : v === "Denied" ? { tone: "deny", text: "✗ Denied" } : undefined;
}

/** The approval card, drawn by the shared GateCard: risk pill, the model's one-sentence summary, the raw command in its
 *  block, then Approve, Always approve, Deny. The thread's delegated click handler reads the data-* hooks and
 *  `.msg.approval:not(.settled)`; a settled card keeps the pill, summary and command, only the buttons go.
 *  `answer`: the user line that settled this card (normalized), for the verdict caption. */
export function ApprovalCard({ e, live, title, botName, answer }: { e: BotEvent; live: boolean; title?: string; botName?: string; answer?: string | null }) {
  const who = botName || "The bot";
  const { body, bodyWhat, sub } = approvalParts(e);
  return (
    <GateCard
      className={`msg approval${live ? "" : " settled"}`}
      rootProps={{ "data-seq": e.seq, title }}
      head={live ? `${who} wants your OK` : `${who} asked for your OK`}
      severity={e.severity}
      riskSummary={e.risk_summary}
      sub={sub}
      body={body}
      bodyWhat={bodyWhat}
      resolved={!live}
      status={live ? undefined : verdictStatus(answer)}
      actions={[
        { key: "approve", text: "Approve", kind: "primary", attrs: { "data-approve": "1" } },
        // A yes that also writes the card's rule (bot.json allow_rules), so this bot never asks again when it does the
        // same thing. The label is "Always approve"; the text SENT stays "Approve, don't ask again" with forever=true
        // (the backend's YES/Forever match depends on it). Only cards whose call can be remembered carry `forever`.
        ...(e.forever
          ? [{ key: "forever", text: "Always approve", kind: "default" as const, title: `Never ask again when this bot ${e.forever.label}. Undo it in Settings › Permissions.`, attrs: { "data-forever": "1" } }]
          : []),
        { key: "deny", text: "Deny", kind: "default", attrs: { "data-deny": "1" } },
      ]}
    />
  );
}
