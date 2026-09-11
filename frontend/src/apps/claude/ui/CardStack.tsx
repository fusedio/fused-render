// WHERE the three kinds of card sit, which is a decision in its own right
// (T:14659-14781, PR #1042).
//
// An OPEN card is the thing the run is blocked on, so together the open ones are
// the LAST thing before the status line — whatever else got mounted after them
// (a reply bubble on a re-attach, a follow-up's fresh bubble, a note). With a
// long enough reply anything landing between the two pushes the card above the
// fold while the status line below says "Waiting for your approval".
//
// An ANSWERED card belongs where it was answered. While it is open it lives at
// the bottom of the log because that is where a control the run is blocked on
// has to be — but the turn's prose and tool chips render inside the streaming
// reply ABOVE it, so every segment produced after the click appended above the
// card and a decision made mid-turn slid to the bottom of the log for the rest
// of it: a receipt filed after everything it came before. So on resolve it is
// parked at the live turn's tail, i.e. in chronological order.
//
// In this tree that is two render sites for one list: `placement="open"` right
// above the working line, `placement="parked"` as the live turn's last child.
import type { ChatController } from "../protocol/controller-api";
import { ANSWERABLE_TOOL, PLAN_TOOL } from "../protocol/summaries";
import type { PermissionMode, PermissionRow } from "../protocol/types";
import { PermCard } from "./PermCard";
import { PlanCard } from "./PlanCard";
import { QuestionCard } from "./QuestionCard";

/** Just the controller methods a card can call. Narrow on purpose: a card must
 *  not be able to start or stop a run. */
export interface CardActions {
  decidePermission: ChatController["decidePermission"];
  answerQuestion: ChatController["answerQuestion"];
  decidePlan: ChatController["decidePlan"];
  dismissCard: ChatController["dismissCard"];
}

export interface CardStackProps {
  /** `state.permissions` — open and parked, in arrival order. */
  rows: PermissionRow[];
  /** "open" = the bottom of the log: still-unanswered cards (pinned last, above
   *  the status line) AND any answered card with no turn to be filed into;
   *  "parked" = answered and filed into `turnKey`, where it was answered. */
  placement: "open" | "parked";
  /** `placement="parked"` only: the turn this stack is the last child of. A row
   *  is drawn here when it was PARKED here (`parkedIn`) — never merely because
   *  this turn happens to be the one streaming now, which filed a card answered
   *  in turn 1 under whatever is streaming in turn 4. */
  turnKey?: string;
  /** Narrow the stack to these row ids, in the list's own order. The parked
   *  placement uses it to draw ONE position's cards (Transcript's `parkPlan`):
   *  a resolved card sits after the tool chip it answered, so a turn with three
   *  approvals has three stacks in three places rather than one at its tail. */
  ids?: string[];
  liveMode?: PermissionMode;
  /** The picker's `permission` param, for a plan's landing mode. */
  pickerMode?: string;
  actions: CardActions;
}

/** A question is not a permission — there is nothing to allow, only something to
 *  answer — and a plan is a third thing again. Same row shape for all three; the
 *  difference is entirely in what the card can send (T:13901-13905). */
export function CardStack({
  rows,
  placement,
  turnKey,
  ids,
  liveMode,
  pickerMode,
  actions,
}: CardStackProps) {
  const only = ids ? new Set(ids) : null;
  // T:14732's "no live turn leaves the card where it is": a decision that
  // landed after the run ended, or a card re-attached with its verdict already
  // on disk, has no `parkedIn` — so it stays in the bottom stack, above the
  // open block that `publishPermissions` sorts last.
  const shown = rows.filter((p) =>
    !p || !p.id || (only && !only.has(p.id))
      ? false
      : placement === "open"
        ? !p.decision || !p.parkedIn
        : !!p.decision && p.parkedIn === turnKey,
  );
  if (!shown.length) return null;
  return (
    <>
      {shown.map((row) =>
        row.tool === ANSWERABLE_TOOL ? (
          <QuestionCard
            key={row.id}
            row={row}
            onAnswer={actions.answerQuestion}
            onDismiss={actions.dismissCard}
          />
        ) : row.tool === PLAN_TOOL ? (
          <PlanCard key={row.id} row={row} pickerMode={pickerMode} onDecide={actions.decidePlan} />
        ) : (
          <PermCard
            key={row.id}
            row={row}
            liveMode={liveMode}
            onDecide={actions.decidePermission}
          />
        ),
      )}
    </>
  );
}

export default CardStack;
