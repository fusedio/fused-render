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
import { activateOnKey, guardedOpenOnClick } from "./planAffordance";
import { PlanModal } from "./PlanModal";

/** THE QUEUE'S LATCH, shared with the approval card (see PermCard). `decidePlan`
 *  goes through the controller's one `decide`, so under the flag this answer can
 *  be HELD exactly as an approval's can — and a card that reported "✓ Plan
 *  approved" over a decision the CLI has not seen would be claiming the work had
 *  started. */
import { answerHeld, queuedAnswerText } from "./PermCard";

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
  // The popup's only state that is not shared with the decision itself: which
  // dialog is on screen. Everything ELSE the modal shows — `note`, `posting`,
  // `status` below — is this same closure's own variables, handed straight
  // through as props (D890), so there is one `sent`/`note`/`threw` per row no
  // matter which surface a reader decides from.
  const [modalOpen, setModalOpen] = useState(false);
  const openModal = () => setModalOpen(true);
  const closeModal = () => setModalOpen(false);

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
  const held = answerHeld(row);
  const resolved = !!row.decision || held;
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

  const status = held
    ? // FIRST, ahead of the three verdicts — the folder was busy, so the
      // decision is stored and goes in the moment that run ends. See PermCard.
      { cls: "queued", text: queuedAnswerText(row.queuedAhead || "") }
    : resolved
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
      {/* THE EXPLICIT AFFORDANCE (D890) — a sibling of `.perm-head`, not a
          child of it: `.perm-head`'s OWN text is pinned verbatim by
          `cards.test.tsx` ("Claude had/has a plan"), so a caption living
          inside it would join that string. Not a `<button>`: several pinned
          tests assert `labels(r)` (every `<button>` in the tree) is `[]` once
          a plan is resolved, and this control is offered in every state,
          resolved included — the modal is still there to reread, read-only.
          `role="button"` + a key handler stand in for the semantics a real
          button would give for free. */}
      {plan ? (
        <div
          className="plan-open-chip"
          role="button"
          tabIndex={0}
          aria-label="Open plan in full view"
          onClick={guardedOpenOnClick(openModal)}
          onKeyDown={activateOnKey(openModal)}
        >
          Open ⤢
        </div>
      ) : null}
      {/* The one payload rendered as markdown, through the funnel every reply
          uses; a plan quotes code, so the copy buttons come with it. Wrapped
          in the same clickable affordance — clicking the plan itself opens
          the wide popup (D890) — but the wrapper adds no `className`, so
          `withClass(r, "plan-body")` still finds exactly the `MarkdownView`
          pinned tests already expect. */}
      {plan ? (
        <div
          className="plan-open-target"
          role="button"
          tabIndex={0}
          aria-label="Open plan in full view"
          onClick={guardedOpenOnClick(openModal)}
          onKeyDown={activateOnKey(openModal)}
        >
          <MarkdownView className="plan-body" text={plan} />
        </div>
      ) : null}
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
      {modalOpen ? (
        <PlanModal
          onClose={closeModal}
          plan={plan}
          extra={extra ? JSON.stringify(extra, null, 2) : null}
          status={status}
          resolved={resolved}
          posting={posting}
          note={note}
          onNoteChange={setNote}
          onApprove={() => {
            // Deciding FROM the modal resolves the card and closes the modal
            // (D890) — closed right away rather than waiting on `resolved`
            // to flip, since a `sendError` leaves `resolved` false and would
            // otherwise strand the popup open over a card the reader has
            // already told to go ahead.
            closeModal();
            void send("allow", landing(), "");
          }}
          onKeepPlanning={() => {
            closeModal();
            void send("deny", "", note);
          }}
        />
      ) : null}
    </div>
  );
}

export default PlanCard;
