// THE COMPOSER'S MIC (owner, 2026-11-19: "add mic to task exactly like the
// bots"). Dictation, the Bots composer's own flow (`@platform/lib/dictation`):
// a press records, a second press stops and the words land at the caret for
// review, Esc throws the take away. Nothing is sent on its own.
//
// Seated immediately right of the paperclip — the Bots order, attach then mic —
// in the group of things that act on THIS draft (attach, mic, schedule, send).
import { Mic } from "lucide-react";

import { MIC_HINT, MIC_STOP_HINT, type Dictation } from "@platform/lib/dictation";
// The take's states (red clock, busy ring) and their keyframes live with the
// composer's sheet; a host that seats this button elsewhere needs them too.
import "../styles/composer.css";

export function MicButton({
  mic,
  disabled,
  disabledReason,
  className = "c-pill c-micbtn",
  hidden,
  skipTab,
  onPress,
}: {
  mic: Dictation;
  disabled?: boolean;
  disabledReason?: string;
  /** The folded card's own seat (`c-mic-idle`, beside the folded paperclip)
   *  instead of the shelf's pill. */
  className?: string;
  /** Faded out and out of reach (the folded seat while the card is open). */
  hidden?: boolean;
  /** Pointer-only seat, as the folded paperclip is. */
  skipTab?: boolean;
  /** After the press started or stopped a take (the folded seat opens the card). */
  onPress?: () => void;
}) {
  const live = mic.rec || mic.busy;
  const label = mic.rec ? "Stop recording" : mic.busy ? "Transcribing" : "Dictate";
  const title = mic.rec
    ? MIC_STOP_HINT
    : mic.busy
      ? mic.note || "Transcribing…"
      : disabled && disabledReason
        ? disabledReason
        : MIC_HINT;
  const cls = [className, mic.rec ? "is-rec" : "", mic.busy ? "is-busy" : "", hidden ? "is-hidden" : ""]
    .filter(Boolean)
    .join(" ");
  return (
    <button
      type="button"
      className={cls}
      aria-label={label}
      aria-pressed={mic.rec}
      title={title}
      // A RUNNING TAKE STAYS STOPPABLE, whatever disabled the seat meanwhile.
      disabled={!!disabled && !live}
      {...(hidden || skipTab ? { tabIndex: -1 } : {})}
      {...(hidden ? { "aria-hidden": true } : {})}
      // A press takes no focus, for the folded paperclip's reason: focus moving
      // onto this button opens the card, which hides the folded seat between
      // the press and its release.
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => {
        if (mic.busy) return;
        mic.toggle();
        onPress?.();
      }}
    >
      <Mic size={14} strokeWidth={2} aria-hidden="true" />
      {mic.rec ? <span className="c-mictime">{mic.time}</span> : null}
    </button>
  );
}
