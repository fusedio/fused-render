// The confirm in front of the Schedule button (T:12036-12157).
//
// The button sits 34px from Send and the click LEAVES the conversation, so a
// miss is expensive in a way a mistyped pill never was. One short question, and
// a sub-line naming what rides along — the two things the icon cannot say.
//
// Modelled on the pills' menu dismissal contract (outside press, Escape,
// window blur) rather than on a modal's: this is a note pinned to a 32px
// button, and dimming a chat the reader may be about to come straight back to
// would be the heavier gesture of the two. That is why this is a Popover and
// not the Dialog design.md §5 maps the other overlays onto.
import { PopoverContent } from "@platform/shadcn/ui/popover";

export interface SchedConfirmProps {
  /** Continue: re-guarded, draft read HERE and not at open time, then navigate. */
  onGo(): void;
  /** Cancel: focus goes back where the reader was — the draft is the thing they
   *  were in the middle of (T:12122). */
  onCancel(): void;
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
      <div className="c-schedpop-title">Schedule this as a task?</div>
      <div className="c-schedpop-sub">
        This task will be scheduled to run at a specific time.
      </div>
      <div className="c-schedpop-row">
        <button type="button" className="c-schedpop-btn" onClick={onCancel}>
          Cancel
        </button>
        <button type="button" className="c-schedpop-btn is-go" onClick={onGo}>
          Continue
        </button>
      </div>
    </PopoverContent>
  );
}
