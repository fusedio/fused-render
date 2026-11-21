// The task chat's jump button: the bots `#tobottom` (apps/bots/components/
// ToBottom.tsx) — a round arrow, growing into an accent "N new messages" pill
// while unread replies sit below you — turned toward your last message
// (jumpTarget.ts): down to it from above, down to the end once you are on it,
// back up to it from the reply under it. The pill always goes to the end.
//
// Sticky at the bottom of the scrollport (a zero-height box after the log), so
// it floats over the transcript without a positioned wrapper around the port.
import { memo } from "react";

import { jumpTo, type JumpMode } from "./jumpTarget";

export interface JumpButtonProps {
  mode: JumpMode;
  /** Unread replies below you. */
  newCount: number;
  onJump: (to: "msg" | "end") => void;
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

export const JumpButton = memo(function JumpButton({ mode, newCount, onJump }: JumpButtonProps) {
  const fresh = newCount > 0;
  const show = fresh || mode !== "none";
  const to = jumpTo(mode, newCount);
  const up = !fresh && mode === "up-msg";
  const label = to === "msg" ? "Jump to your last message" : "Jump to the latest message";
  return (
    <div className="chat-jump" aria-hidden={show ? undefined : true}>
      <button
        type="button"
        className={"chat-jump-btn" + (fresh ? " is-new" : "") + (show ? " is-shown" : "") + (up ? " is-up" : "")}
        data-to={to}
        tabIndex={show ? 0 : -1}
        title={label}
        aria-label={label}
        onClick={() => onJump(to)}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          {up ? UP : DOWN}
        </svg>
        {fresh ? <span>{`${newCount} new message${newCount > 1 ? "s" : ""}`}</span> : null}
      </button>
    </div>
  );
});
