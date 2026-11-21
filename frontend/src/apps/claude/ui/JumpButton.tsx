// The task chat's jump button: the bots `#tobottom` (apps/bots/components/
// ToBottom.tsx), same rules — a round arrow while you are scrolled up from the
// end, an accent "N new messages" pill while unread replies sit below you.
// Arrow or pill, a click goes to the very end and retires the count.
//
// Sticky at the bottom of the scrollport (a zero-height box after the log), so
// it floats over the transcript without a positioned wrapper around the port.
import { memo } from "react";

export interface JumpButtonProps {
  /** The port sits at the end (`END_GAP_PX`). */
  atEnd: boolean;
  /** Unread replies below you. */
  newCount: number;
  onJump: () => void;
}

export const JumpButton = memo(function JumpButton({ atEnd, newCount, onJump }: JumpButtonProps) {
  const fresh = newCount > 0;
  const show = fresh || !atEnd;
  return (
    <div className="chat-jump" aria-hidden={show ? undefined : true}>
      <button
        type="button"
        className={"chat-jump-btn" + (fresh ? " is-new" : "") + (show ? " is-shown" : "")}
        tabIndex={show ? 0 : -1}
        title="Jump to the latest message"
        aria-label="Jump to the latest message"
        onClick={onJump}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M12 5v14" />
          <path d="m19 12-7 7-7-7" />
        </svg>
        {fresh ? <span>{`${newCount} new message${newCount > 1 ? "s" : ""}`}</span> : null}
      </button>
    </div>
  );
});
