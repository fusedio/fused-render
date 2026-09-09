// "What was sent": the composed message a receipt stands for, on demand
// (T:10965-11045, 4403-4413).
//
// THE DOOR IS THE RECEIPT (R4-1). This box is opened by clicking the "app state
// attached" line under the bubble it belongs to — T's own affordance
// (T:11059 `row.title = "Click to see exactly what was sent to the agent"`,
// T:1249) — not by a second control beside it. `ui/Turn` owns that press.
//
// AND IT WEARS THE APP'S MODAL CHROME, not a skin of its own: the shared
// `platform/ui/modal/Modal` chassis, the same one `EraseTaskModal` (the Delete
// task dialog this chat's kebab opens) uses — same overlay, same 600px card,
// same head with the ✕, same focus trap and Esc/backdrop close. Before this it
// was a shadcn `Dialog` with hand-rolled bar and pill, so the two dialogs the
// same menu could put on screen looked like they came from different apps.
//
// PR1 shows the one part that always exists — the exact text the agent was
// handed. The overview screenshot, each comment with its label/anchor/timing
// and the other attached pictures are PR2/PR3's sections of this same box; the
// heading they sit under is the one below.
import { Modal } from "@platform/ui/modal/Modal";

export interface SentPopProps {
  open: boolean;
  onClose(): void;
  /** The raw outgoing text, wire blocks and all (`UserTurn.raw`). */
  outgoing: string;
}

export function SentPop({ open, onClose, outgoing }: SentPopProps) {
  // `Modal` cannot unmount itself (it defers `onClose` to animate the exit), so
  // it is rendered conditionally — the shape every other caller of the chassis
  // uses. The `open` prop stays because the hosts hold the turn, not a boolean.
  if (!open) return null;
  return (
    <Modal
      title="What was sent"
      onClose={onClose}
      footer={
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          Close
        </button>
      }
    >
      {/* WHY the reader is looking at more than they typed: the pane's app
          state rides along with the prompt, and this is the whole of it. */}
      <p className="deploy-muted">
        Everything the agent received for this turn — your message and the app
        state that rode along with it.
      </p>
      <h4 className="c-sent-head">Exact message the agent received</h4>
      <pre className="c-sent-wire">{outgoing}</pre>
    </Modal>
  );
}
