// "Schedule this as a task" — the HANDOFF, and the handoff is one button
// (T:11940-12035, 12075-12128).
//
// The composer used to try to schedule on its own; everything past a bare
// deferral is the /tasks page's form, so what travels is what the page cannot
// know and this composer always does: the FOLDER, the words already in the box,
// and the conversation they were written in. What does NOT travel is how this
// chat is configured — a task runs unattended and the page owns those answers.
import { useCallback, useEffect, useState } from "react";
import { Popover, PopoverTrigger } from "@platform/shadcn/ui/popover";
import { SchedConfirm } from "./SchedConfirm";
import { schedulerUrl, stashDraft } from "./sched-draft";
import { useDismissOnWindow } from "./useDismissOnWindow";

export interface SchedButtonProps {
  file: string | null;
  /** "" on the landing page, which is correct rather than missing: there is no
   *  session yet, and the store reads "" as "start a new one" (T:12022). */
  sessionId: string;
  /** Read at CONTINUE time and not at open time, so a paste made with the
   *  confirm already up still travels (T:12094). */
  draft(): string;
  /** Where "Back to chat" has to land — the host's own path. */
  back: string;
  /** A pending scheduled message shuts this door as well as the composer's
   *  (`schedBlocked`, PR4). Never true for the landing card (T:16851). */
  disabled?: boolean;
  /**
   * WHY it is refusing, when the reason is one the reader can act on — the nav
   * lock's `NAV_LOCKED_REASON` (T:6896's "Finish or discard the notes first"),
   * or the schedule block's own sentence (T:17237-17246).
   *
   * It rides the `title` AND the spoken name, because `disabled` takes the
   * button out of tab order: the title is then unreachable by keyboard and the
   * name is all a reader browsing this row will hear. The name stays FIRST —
   * what the control is, then why it is off — so the button is still
   * identifiable while it is refusing. The Back button's twin refusal says the
   * same sentence the same two ways (`ClaudeChat`).
   */
  disabledReason?: string;
  /** Cancel puts the focus back in the box the draft is in. */
  onCancel?(): void;
  onNavigate?(url: string): void;
}

/** T:16860-16861 — the pristine wording, in ONE place: a re-word here cannot
 *  leave a refusal restoring a tooltip nobody writes any more. */
export const SCHED_LABEL = "Schedule this as a task";

/** T:4184-4188 / 4243-4247, verbatim. */
function CalendarIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
    >
      <rect
        x="2"
        y="3.2"
        width="12"
        height="10.6"
        rx="1.6"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <path
        d="M2 6.6h12M5.4 1.8v2.6M10.6 1.8v2.6"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function SchedButton({
  file,
  sessionId,
  draft,
  back,
  disabled,
  disabledReason,
  onCancel,
  onNavigate,
}: SchedButtonProps) {
  const [open, setOpen] = useState(false);
  const why = disabled && disabledReason ? SCHED_LABEL + " — " + disabledReason : SCHED_LABEL;

  /**
   * T:17253 — A CONFIRM CAN ALREADY BE UP WHEN THE BLOCK LANDS. "Schedule this
   * as a task?" is then a question about a button that has just died, and its
   * Continue would hit the guard in `go` and do nothing visible. Take it down
   * instead, so what the reader ends up looking at is the banner.
   *
   * Only on the way IN: closing a confirm the user opened the moment the block
   * lifted would be the block reaching past its own end.
   */
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  const go = useCallback(() => {
    // The confirm can OUTLIVE the press that opened it — the schedule poll may
    // block this chat while the question is still on screen — so the last word
    // on whether a task may be made from here is read HERE (T:12091).
    if (disabled) return;
    const text = draft().trim();
    setOpen(false);
    // The draft SURVIVES the trip: leaving unloads this view, and a rebuilt
    // composer used to come back empty (T:12011).
    stashDraft(file, text);
    const url = schedulerUrl({ file, draft: text, sessionId, back });
    onNavigate?.(url);
  }, [disabled, draft, file, sessionId, back, onNavigate]);

  const cancel = useCallback(() => {
    setOpen(false);
    onCancel?.();
  }, [onCancel]);

  /**
   * THE OTHER HALF OF THE DISMISSAL CONTRACT (T:12146, 12149), and it matters
   * more here than on any pill: this popover's Continue NAVIGATES AWAY FROM THE
   * CONVERSATION. An orphaned confirm floating over a pane the reader has since
   * clicked into is one keypress from leaving the chat — and P4-15 just made
   * that keypress Enter.
   *
   * Closed WITHOUT `onCancel`: a blur is not a reader answering the question,
   * so there is no focus to hand back to a draft nobody left.
   */
  const dismiss = useCallback(() => setOpen(false), []);
  useDismissOnWindow(open, dismiss);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <button
            type="button"
            className="c-pill c-schedbtn"
            aria-label={why}
            title={disabled && disabledReason ? disabledReason : SCHED_LABEL}
            disabled={disabled}
          >
            <CalendarIcon />
          </button>
        }
      />
      <SchedConfirm onGo={go} onCancel={cancel} />
    </Popover>
  );
}
