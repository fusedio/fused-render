// The approval bridge's card (T:13899-14012).
//
// Headless claude cannot open a terminal prompt, so agent.py points its
// --permission-prompt-tool at an MCP server that parks each request on disk and
// poll hands them here. Until one of these buttons is clicked the subprocess is
// genuinely blocked — the card IS the prompt, which is why it takes the accent
// ring, why nothing in it truncates, and why a failed send has to put the
// buttons back.
import { useState } from "react";

import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";

import type { ChatController } from "../protocol/controller-api";
import {
  leftoverInput,
  permCardLabel,
  permChoices,
  PERMISSION_LABELS,
  prettyToolName,
  summarizePermission,
} from "../protocol/summaries";
import type { Decision, DecisionScope, PermissionMode, PermissionRow, SwitchableMode } from "../protocol/types";

export interface PermCardProps {
  row: PermissionRow;
  /** The mode the run is ACTUALLY in, as reported by poll — never the picker's
   *  param, which applies to the next spawn (T:13884-13886). */
  liveMode?: PermissionMode;
  onDecide: ChatController["decidePermission"];
}

/** T:13957-13975 — the verdict, in the words the transcript keeps. */
function statusFor(row: PermissionRow, label: string): { cls: string; text: string } {
  if (row.decision === "allow") {
    if (row.mode)
      return {
        cls: "allow",
        text: "✓ Allowed — approvals set to “" + (PERMISSION_LABELS[row.mode] || row.mode) + "”",
      };
    if (row.scope === "session")
      return { cls: "allow", text: "✓ Allowed — not asking again for " + label + " in this reply" };
    return { cls: "allow", text: "✓ Allowed" };
  }
  if (row.decision === "expired")
    return { cls: "expired", text: "◦ Unanswered — the reply ended before you decided" };
  return { cls: "deny", text: "✗ Denied" };
}

export function PermCard({ row, liveMode, onDecide }: PermCardProps) {
  const [sent, setSent] = useState(false);
  const [threw, setThrew] = useState("");

  const toolName = row.tool || "a tool";
  const pretty = prettyToolName(toolName);
  const label = permCardLabel(pretty);
  const { sub, body, covered } = summarizePermission(row);
  const extra = leftoverInput(row.input, covered);
  const choices = permChoices(row, liveMode);
  const resolved = !!row.decision;

  // WHY THE ROW AND NOT LOCAL STATE. `decidePermission` does not reject — the
  // controller catches the failure and writes `row.sendError`, which the poll
  // then keeps alive while the card is open (T:14113) — so a card waiting on a
  // local `catch` sat at "sending…" with every button disabled forever, and
  // never said why. The row is the answer; the local `catch` stays only for a
  // host that hands this card a throwing `onDecide` directly (a unit test).
  const sendError = row.sendError || (threw ? "Could not send that: " + threw : "");
  const posting = sent && !sendError && !resolved;

  async function send(decision: Decision, scope: DecisionScope, mode: "" | SwitchableMode) {
    setSent(true);
    setThrew("");
    try {
      await onDecide(row.id, decision, scope, mode || undefined);
    } catch (err) {
      // The subprocess is still blocked, so the buttons have to come back.
      setSent(false);
      setThrew(err instanceof Error ? err.message : String(err));
    }
  }

  const status = resolved
    ? statusFor(row, label)
    : sendError
      ? { cls: "deny", text: sendError }
      : posting
        ? { cls: "", text: "sending…" }
        : { cls: "", text: "" };

  return (
    // NO Enter SHORTCUT. `T` has none, and inventory 04 §D pins "no custom
    // Enter/Space handlers on cards except the Other textarea" (T:2121-2128) —
    // on the one card whose whole job is to be READ before it is answered, a
    // key that approves is the wrong affordance to invent.
    <div className={cn("turn", "perm", resolved && "resolved")} data-perm-id={row.id}>
      <div className="perm-head">
        {/* Past tense once it is history: a card still reading "Claude wants to
            use" above a verdict looks like a prompt that is somehow still
            waiting (T:13955). */}
        {resolved ? "Claude wanted to use " : "Claude wants to use "}
        <span className="tool" {...(pretty !== toolName ? { title: toolName } : {})}>
          {pretty}
        </span>
      </div>
      {sub ? <div className="perm-sub">{sub}</div> : null}
      {/* Verbatim, and NEVER truncated: an Allow hands the tool its input as it
          stands, so a card that showed a prefix would ask the user to approve
          bytes they never saw. */}
      {body ? <pre>{body}</pre> : null}
      {extra ? <pre>{JSON.stringify(extra, null, 2)}</pre> : null}
      {resolved ? null : (
        <div className="perm-actions">
          {choices.map((c) => (
            <Button
              key={c.text}
              type="button"
              variant="ghost"
              className={cn("perm-btn", c.primary && "primary")}
              disabled={posting}
              {...(c.title ? { title: c.title } : {})}
              onClick={() => void send(c.decision, c.scope, c.mode)}
            >
              {c.text}
            </Button>
          ))}
        </div>
      )}
      <div className={cn("perm-status", status.cls)}>{status.text}</div>
    </div>
  );
}

export default PermCard;
