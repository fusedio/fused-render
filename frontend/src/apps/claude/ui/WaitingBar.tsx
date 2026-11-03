// THE WAITING BAR: while a blocking card (question, approval, plan) is open the
// composer steps aside for one slim line that says what the run is waiting for.
// The card itself is an ordinary block at the end of the log; when it is
// scrolled out of view the bar offers a jump back to it.
import type { ReactNode } from "react";

import type { PermissionRow } from "../protocol/types";
import { ANSWERABLE_TOOL, PLAN_TOOL } from "../protocol/summaries";

export type WaitingKind = "question" | "permission" | "plan";

/** The rows a card is open for: the one rule the card and the bar share. */
export function openCardRows(rows: PermissionRow[]): PermissionRow[] {
  return rows.filter((p) => p && p.id && !p.decision);
}

/** The kind of the NEWEST open row, or null when no card is open. */
export function waitingKind(rows: PermissionRow[]): WaitingKind | null {
  const open = openCardRows(rows);
  const last = open[open.length - 1];
  if (!last) return null;
  return last.tool === ANSWERABLE_TOOL ? "question" : last.tool === PLAN_TOOL ? "plan" : "permission";
}

export function waitingText(kind: WaitingKind, agentName: string): string {
  return kind === "question"
    ? `${agentName} is waiting for your answer`
    : kind === "plan"
      ? "Waiting for you to review the plan"
      : "Waiting for your approval";
}

export function jumpLabel(kind: WaitingKind): string {
  return kind === "question" ? "Jump to question" : kind === "plan" ? "Jump to plan" : "Jump to approval";
}

export interface WaitingBarProps {
  kind: WaitingKind;
  agentName: string;
  showJump: boolean;
  onJump: () => void;
}

export function WaitingBar({ kind, agentName, showJump, onJump }: WaitingBarProps) {
  return (
    <div className="waiting-bar" role="status">
      <span className="waiting-bar-dot" aria-hidden="true" />
      <span className="waiting-bar-text">{waitingText(kind, agentName)}</span>
      {showJump ? (
        <button type="button" className="waiting-bar-jump" onClick={onJump}>
          {jumpLabel(kind)}
        </button>
      ) : null}
    </div>
  );
}

/** Swaps the composer for the bar while `kind` is set. The composer stays
 *  MOUNTED (hidden) so its draft, attachments and queued lines survive. */
export function ComposerGate({
  kind,
  agentName,
  showJump,
  onJump,
  children,
}: Partial<Omit<WaitingBarProps, "kind">> & { kind: WaitingKind | null; children: ReactNode }) {
  return (
    <>
      {kind ? (
        <WaitingBar kind={kind} agentName={agentName ?? "Claude"} showJump={!!showJump} onJump={onJump ?? (() => {})} />
      ) : null}
      <div className="composer-gate" hidden={!!kind}>
        {children}
      </div>
    </>
  );
}

export default WaitingBar;
