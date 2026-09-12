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

/**
 * The sentence for a decision the PROJECT QUEUE is holding — read ahead of every
 * verdict below, because there is no verdict yet: the folder was busy with
 * another task, so the answer is stored and goes in the moment that run ends.
 *
 * "runs next" and not "will run": the held answer is at the HEAD of its folder's
 * line by construction (it outranks every queued message there — somebody is
 * already waiting on it), so the next thing that happens in this folder is this.
 *
 * The holder is named when the page that clicked is still open; "" is a real
 * answer twice over — a folder held by a run with no task row, and every card
 * restored after a reload, which reads `held` off the poll and has no name to
 * read — and the sentence stops short rather than trailing off after "after".
 */
export function queuedAnswerText(ahead: string): string {
  return ahead
    ? `◷ Answer queued — runs next after ${ahead}`
    : "◷ Answer queued — runs next in this folder";
}

/**
 * Is this card's decision made but not delivered? Either the click that made it
 * said so (`queuedAhead`, this document only) or the server does (`held`, which
 * is how a card knows after a reload).
 *
 * A LANDED VERDICT OUTRANKS THE SERVER'S MARKER (round-3 review, 2026-09-12).
 * `held` rides on the poll's own row and is written for any request with an
 * entry in `held_answers.json` — a store the queue clears on its own clock, not
 * the card's. So the poll that finally carries the DELIVERED decision can carry
 * `held: true` beside it, and this rule, asked first by all three cards, then
 * printed "◷ Answer queued" over an answer the tool already has and already
 * acted on. `held` therefore speaks only while there is no verdict to speak
 * over.
 *
 * `queuedAhead` is exempt, and that is not an inconsistency: it is stamped BY
 * the click that made the decision, in the same write (`resolveLocally`), so the
 * `decision` beside it is the READER'S own and not the tool's — the annotation
 * is the only thing that tells those two apart. It cannot go stale the way
 * `held` does either: `syncPermissions` rebuilds the row from the server's the
 * moment a real verdict lands, and client-only annotations do not survive that.
 */
export function answerHeld(row: PermissionRow): boolean {
  if (row.queuedAhead !== undefined) return true;
  return row.held === true && !row.decision;
}

/** T:13957-13975 — the verdict, in the words the transcript keeps. */
function statusFor(row: PermissionRow, label: string): { cls: string; text: string } {
  // FIRST, ahead of the three verdicts. A held answer has a `decision` — the one
  // the reader made, which is what latches the card — but the tool has not seen
  // it, so "✓ Allowed" would be a claim about something that has not happened.
  // A card rebuilt by a reload has no `decision` of its own and still lands here
  // (`held`), which is the whole point of asking the server.
  //
  // First is not unconditional: `answerHeld` itself steps aside for a DELIVERED
  // verdict, so a row carrying both reads as the verdict it carries (see there).
  if (answerHeld(row)) return { cls: "queued", text: queuedAnswerText(row.queuedAhead || "") };
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
  // LATCHED BY THE HELD FLAG TOO, not only by a landed verdict. The server has
  // the answer and will write it the moment the folder frees, so a reloaded card
  // that came back with live buttons would be offering to answer a question that
  // is already answered — and the second answer is the one that would be thrown
  // away, silently, by first-writer-wins down in agent.py.
  const resolved = !!row.decision || answerHeld(row);

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
