// The plan popup (D890) — the SAME plan text, through the SAME markdown
// funnel as the card (`MarkdownView`), at a wide, comfortable reading width.
//
// PURELY PRESENTATIONAL: every piece of decision state (`note`, whether a
// decision is in flight, the status line) is a prop PlanCard hands down from
// its own `useState`s — there is exactly ONE state instance per row, shared
// between the card and this modal, which is what makes them agree by
// construction rather than by synchronization, and what makes a double-click
// safe (the `posting` prop that disables the card's buttons disables the
// modal's identical buttons too, because it is the same boolean).
//
// Portals to the TOP document via `getContainer={topDocumentBody}` (see that
// module for why): a modal boxed into a small iframe pane could never be
// "wide". The wrapper below wears `.chat-root` (the class that carries the
// chat's own typography AND its `--c-*` palette — see chat.css's docblock)
// PLUS `.perm`/`.plan` so the existing `.chat-root .perm .plan-body` etc.
// selectors in transcript.css apply unchanged; no CSS is duplicated here,
// only the layout override in styles/composer.css's "plan modal" section.
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";
import { Modal } from "@platform/ui/modal/Modal";

import { PLAN_NOTE_LIMIT } from "../protocol/summaries";
import { MarkdownView } from "./MarkdownView";
import { topDocumentBody } from "./topContainer";

export interface PlanModalStatus {
  cls: string;
  text: string;
}

export interface PlanModalProps {
  onClose: () => void;
  plan: string;
  /** JSON dump of any leftover/unrenderable input — same disclosure rule as
   *  the card: nothing the model wrote is invisible. */
  extra?: string | null;
  status: PlanModalStatus;
  /** True once the card has a decision (or a held/queued one) — read-only. */
  resolved: boolean;
  posting: boolean;
  note: string;
  onNoteChange?: (value: string) => void;
  onApprove?: () => void;
  onKeepPlanning?: () => void;
}

export function PlanModal({
  onClose,
  plan,
  extra,
  status,
  resolved,
  posting,
  note,
  onNoteChange,
  onApprove,
  onKeepPlanning,
}: PlanModalProps) {
  return (
    <Modal
      title={resolved ? "Claude had a plan" : "Claude has a plan"}
      onClose={onClose}
      dialogClassName="c-plan-modal c-tokens"
      plainBody
      getContainer={topDocumentBody}
    >
      {/* TWO NESTED ELEMENTS, not one node wearing all four classes: the
          existing `.chat-root .perm .plan-body` (and `.perm-status`/
          `.perm-actions`/`.perm-btn`/`.plan-note`) rules in transcript.css are
          DESCENDANT selectors, which never match a class sitting on the SAME
          element as `.chat-root` — the live card satisfies them only because
          `.chat-root` is a real ancestor several levels up in ClaudeChat's own
          tree. `.chat-root` goes on the outer node (the typography + `--c-*`
          palette every plan/card/status rule reads), `.perm`/`.plan` on the
          inner one, reproducing that same ancestor relationship so every
          existing rule applies unchanged and none of it is duplicated here. */}
      <div className="chat-root c-plan-modal-root">
        <div className="perm plan">
          <div className="plan-modal-reading">
            {plan ? <MarkdownView className="plan-body" text={plan} /> : null}
            {extra ? <pre>{extra}</pre> : null}
            {resolved ? null : (
              <>
                <textarea
                  className="plan-note"
                  rows={3}
                  maxLength={PLAN_NOTE_LIMIT}
                  aria-label="Note for revising the plan"
                  placeholder="Optional: what to change (sent with “Keep planning”)"
                  disabled={posting}
                  value={note}
                  onChange={(ev) => onNoteChange?.(ev.target.value)}
                />
                <div className="perm-actions">
                  <Button
                    type="button"
                    variant="ghost"
                    className="perm-btn primary"
                    disabled={posting}
                    onClick={onApprove}
                  >
                    Approve plan
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    className="perm-btn"
                    disabled={posting}
                    onClick={onKeepPlanning}
                  >
                    Keep planning
                  </Button>
                </div>
              </>
            )}
            <div className={cn("perm-status", status.cls)}>{status.text}</div>
          </div>
        </div>
      </div>
    </Modal>
  );
}

export default PlanModal;
