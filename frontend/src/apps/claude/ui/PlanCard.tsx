// ExitPlanMode — the model asking to stop planning and start doing the work
// (T:14476-14615). Not an approval either: what is parked is a PLAN, and the
// two answers are "go ahead" and "keep planning".
//
//   * Approve plan sends a plain `allow`; the CLI leaves plan mode itself when
//     it sees one. A `mode` rides along ONLY when the picker already sits on a
//     looser mode, which is then the mode the session should land in;
//   * Keep planning sends a `deny` plus the optional note. The SENTENCE the
//     model reads is composed by agent.py — the note is the only part this card
//     contributes, and it is never written back into the page;
//   * there is no "allow all in this reply": there is one plan, and a session
//     grant would pre-approve the next one unseen.
//
// The plan is the ONE payload on any card rendered as markdown, because it is
// the one that genuinely IS markdown — a tool input is bytes off a disk, a plan
// is prose the model wrote for the user to read (D248).
import { useState } from "react";

import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";

import type { ChatController } from "../protocol/controller-api";
import {
  leftoverInput,
  planBody,
  PLAN_HIDDEN_INPUT_KEYS,
  PLAN_NOTE_LIMIT,
  SWITCHABLE_MODES,
} from "../protocol/summaries";
import type { PermissionRow, SwitchableMode } from "../protocol/types";
import { MarkdownView } from "./MarkdownView";

export interface PlanCardProps {
  row: PermissionRow;
  /** The picker's CURRENT `permission` param. Only a member of
   *  SWITCHABLE_MODES is offered as the landing mode; sitting on "plan first"
   *  or "ask every time" sends no mode at all (T:14586-14595). */
  pickerMode?: string;
  onDecide: ChatController["decidePlan"];
}

export function PlanCard({ row, pickerMode, onDecide }: PlanCardProps) {
  const [note, setNote] = useState("");
  const [sent, setSent] = useState(false);
  const [threw, setThrew] = useState("");

  const plan = planBody(row.input);
  // The same disclosure rule as every other card — no input key the model chose
  // is invisible — minus the CLI's own bookkeeping (`PLAN_HIDDEN_INPUT_KEYS`),
  // which is not a thing being approved. `plan` is covered only when it is
  // usable above, so an unrenderable one still falls into the dump and the card
  // never implies a plan was read (T:14496-14503).
  const extra = leftoverInput(
    row.input,
    plan ? ["plan", ...PLAN_HIDDEN_INPUT_KEYS] : PLAN_HIDDEN_INPUT_KEYS,
  );
  const resolved = !!row.decision;
  const landing = (): "" | SwitchableMode =>
    pickerMode && SWITCHABLE_MODES.has(pickerMode) ? (pickerMode as SwitchableMode) : "";

  // `decidePlan` does not reject: the controller catches and writes
  // `row.sendError` (T:14589-14594), so the reason to put the buttons back —
  // and the sentence to show — is on the row. See PermCard's fuller note.
  const sendError = row.sendError || (threw ? "Could not send that: " + threw : "");
  const posting = sent && !sendError && !resolved;

  async function send(decision: "allow" | "deny", mode: "" | SwitchableMode, text: string) {
    setSent(true);
    setThrew("");
    try {
      await onDecide(row.id, decision, mode || undefined, text || undefined);
    } catch (err) {
      // The subprocess is still blocked, so the buttons have to come back.
      setSent(false);
      setThrew(err instanceof Error ? err.message : String(err));
    }
  }

  const status = resolved
    ? row.decision === "allow"
      ? { cls: "allow", text: "✓ Plan approved" }
      : row.decision === "expired"
        ? { cls: "expired", text: "◦ Unanswered — the reply ended before you decided" }
        : // Not a refusal — the other half of a conversation, so it does not
          // read in the deny red (see `.perm.plan .perm-status.deny`).
          { cls: "deny", text: "◦ Sent back for revision" }
    : sendError
      ? { cls: "deny", text: sendError }
      : posting
        ? { cls: "", text: "sending…" }
        : { cls: "", text: "" };

  return (
    <div className={cn("turn", "perm", "plan", resolved && "resolved")} data-perm-id={row.id}>
      <div className="perm-head">{resolved ? "Claude had a plan" : "Claude has a plan"}</div>
      {/* The one payload rendered as markdown, through the funnel every reply
          uses; a plan quotes code, so the copy buttons come with it. */}
      {plan ? <MarkdownView className="plan-body" text={plan} /> : null}
      {/* The same disclosure rule as every other card: `plan` is the body above
          when it is usable, and falls into this dump when it is not, so the
          card never implies a plan was read. */}
      {extra ? <pre>{JSON.stringify(extra, null, 2)}</pre> : null}
      {resolved ? null : (
        <>
          <textarea
            className="plan-note"
            rows={2}
            // Bounded to what agent.py will actually keep (D146): an honest
            // user typing past the limit should never be the one who finds out
            // later.
            maxLength={PLAN_NOTE_LIMIT}
            aria-label="Note for revising the plan"
            placeholder="Optional: what to change (sent with “Keep planning”)"
            disabled={posting}
            value={note}
            onChange={(ev) => setNote(ev.target.value)}
          />
          <div className="perm-actions">
            <Button
              type="button"
              variant="ghost"
              className="perm-btn primary"
              disabled={posting}
              onClick={() => void send("allow", landing(), "")}
            >
              Approve plan
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="perm-btn"
              disabled={posting}
              onClick={() => void send("deny", "", note)}
            >
              Keep planning
            </Button>
          </div>
        </>
      )}
      <div className={cn("perm-status", status.cls)}>{status.text}</div>
    </div>
  );
}

export default PlanCard;
