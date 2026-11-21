// The task chat's jump buttons: the bots `#tobottom` (apps/bots/components/
// ToBottom.tsx) — a round arrow, growing into an accent "N new messages" pill
// while unread replies sit on its side — in two fixed directions
// (jumpTarget.ts): ↑ at the top to your last message, ↓ at the bottom to the
// end. The pill goes to the NEW rule instead.
//
// Sticky to the scrollport's top or bottom edge (a zero-height box before or
// after the log), so it floats over the transcript without a positioned
// wrapper around the port.
import { memo } from "react";

export interface JumpButtonProps {
  dir: "up" | "down";
  shown: boolean;
  /** Unread replies on this side (> 0 = the pill). */
  newCount: number;
  onJump: () => void;
}

const DOWN = (
  <>
    <path d="M12 5v14" />
    <path d="m19 12-7 7-7-7" />
  </>
);
const UP = (
  <>
    <path d="M12 19V5" />
    <path d="m5 12 7-7 7 7" />
  </>
);

export const JumpButton = memo(function JumpButton({ dir, shown, newCount, onJump }: JumpButtonProps) {
  const fresh = newCount > 0;
  const show = fresh || shown;
  const up = dir === "up";
  const label = fresh
    ? "Jump to the new messages"
    : up
      ? "Jump to your last message"
      : "Jump to the latest message";
  return (
    <div className={"chat-jump" + (up ? " is-top" : "")} aria-hidden={show ? undefined : true}>
      <button
        type="button"
        className={"chat-jump-btn" + (fresh ? " is-new" : "") + (show ? " is-shown" : "") + (up ? " is-up" : "")}
        data-to={fresh ? "new" : up ? "msg" : "end"}
        tabIndex={show ? 0 : -1}
        title={label}
        aria-label={label}
        onClick={onJump}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          {up ? UP : DOWN}
        </svg>
        {fresh ? <span>{`${newCount} new message${newCount > 1 ? "s" : ""}`}</span> : null}
      </button>
    </div>
  );
});
