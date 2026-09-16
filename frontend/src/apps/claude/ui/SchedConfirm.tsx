// The confirm in front of the Schedule button (T:12036-12157).
//
// The button sits 34px from Send and the click LEAVES the conversation, so a
// miss is expensive in a way a mistyped pill never was. One short question, and
// a sub-line naming what rides along — the two things the icon cannot say.
//
// Modelled on the pills' menu dismissal contract — outside press and Escape
// from Base UI, window blur and resize from `useDismissOnWindow` at the
// `Popover` above (T:12146, 12149) — rather than on a modal's: this is a note pinned to a 32px
// button, and dimming a chat the reader may be about to come straight back to
// would be the heavier gesture of the two. That is why this is a Popover and
// not the Dialog design.md §5 maps the other overlays onto.
import { useEffect, useRef } from "react";

import { PopoverContent } from "@platform/shadcn/ui/popover";

export interface SchedConfirmProps {
  /** Continue: re-guarded, draft read HERE and not at open time, then navigate. */
  onGo(): void;
  /** Cancel: focus goes back where the reader was — the draft is the thing they
   *  were in the middle of (T:12122). */
  onCancel(): void;
}

/**
 * THE CONTENT, SPLIT OUT FOR THE SAME REASON `ShotViewerBody` and `SentPopBody`
 * are: everything above this line is a PORTAL, and a portal has no container in
 * a renderer with no document — so a suite driving `SchedConfirm` asserts on an
 * empty tree and the focus contract below could not be tested at all. The body
 * is the whole of what this overlay IS; the wrapper is only where it hangs.
 */
export function SchedConfirmBody({ onGo, onCancel }: SchedConfirmProps) {
  /**
   * ENTER CONTINUES (T:12072). The DOM order is Cancel-then-Continue because
   * that is T's READING order — the cheap way out is named first — but the
   * keyboard's default answer to this question is the affirmative one, and Base
   * UI's Popover otherwise lands on the popup's first focusable, which is
   * Cancel. So a keyboard user's Enter dismissed the very question they had
   * just opened.
   *
   * `preventScroll` for the reason it is set everywhere else in this view: the
   * composer is pinned to the bottom of a scrolling pane, and focusing into an
   * overlay above it must not move the transcript underneath.
   */
  const goRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    goRef.current?.focus({ preventScroll: true });
  }, []);

  return (
    <>
      <div className="c-schedpop-title">Schedule this as a task?</div>
      <div className="c-schedpop-sub">
        This task will be scheduled to run at a specific time.
      </div>
      <div className="c-schedpop-row">
        <button type="button" className="c-schedpop-btn" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="c-schedpop-btn is-go"
          ref={goRef}
          onClick={onGo}
        >
          Continue
        </button>
      </div>
    </>
  );
}

export function SchedConfirm({ onGo, onCancel }: SchedConfirmProps) {
  return (
    <PopoverContent
      side="top"
      align="end"
      sideOffset={6}
      aria-label="Schedule this as a task?"
      className="c-overlay c-schedpop w-[260px] min-w-0 flex-col gap-0 rounded-[10px] bg-[var(--c-panel)] p-3 text-[var(--c-fg)] shadow-none ring-0"
    >
      <SchedConfirmBody onGo={onGo} onCancel={onCancel} />
    </PopoverContent>
  );
}
