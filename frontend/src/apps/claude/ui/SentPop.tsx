// "What was sent": the composed message a receipt stands for, on demand
// (T:10965-11045, 4403-4413).
//
// PR1 shows the one part that always exists — the exact text the agent was
// handed. The overview screenshot, each comment with its label/anchor/timing
// and the other attached pictures are PR2/PR3's sections of this same box; the
// headings they sit under are the ones below.
import { Dialog, DialogContent, DialogTitle } from "@platform/shadcn/ui/dialog";

export interface SentPopProps {
  open: boolean;
  onClose(): void;
  /** The raw outgoing text, wire blocks and all (`UserTurn.raw`). */
  outgoing: string;
}

export function SentPop({ open, onClose, outgoing }: SentPopProps) {
  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent
        showCloseButton={false}
        className="c-overlay c-sentpop-box flex w-[min(720px,calc(100vw-48px))] max-w-none flex-col gap-0 overflow-hidden rounded-[10px] border border-[var(--c-border)] bg-[var(--c-bg)] p-0 text-[var(--c-fg)] ring-0"
      >
        <div className="c-sentpop-bar">
          <DialogTitle className="text-[13px] font-semibold">
            What was sent
          </DialogTitle>
          <span className="c-spacer" />
          <button type="button" className="c-pill" onClick={onClose} autoFocus>
            Close
          </button>
        </div>
        <div className="c-sentpop-body">
          <h4>Exact message the agent received</h4>
          <pre className="c-sent-wire">{outgoing}</pre>
        </div>
      </DialogContent>
    </Dialog>
  );
}
