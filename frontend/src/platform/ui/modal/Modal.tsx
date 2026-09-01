// Shared modal chassis for every dialog in the app (SPEC: modal/form design
// system). Since the shadcn migration this renders on the shadcn/base-ui
// Dialog primitives — which own the portal, backdrop, focus trap/restore and
// aria wiring — while this file keeps the app's own behavioral contract on
// top of them:
//   • `busy` gate: Esc / backdrop / ✕ do NOT close while an action must not
//     be abandoned (the ✕ is not rendered at all while busy — a disabled
//     button takes no pointer events, so its explanatory title can never
//     appear; see the 2026-08-24 note in git history).
//   • `dirty` guard: the first close attempt ARMS (inline hint + amber ✕),
//     the next attempt discards, however long the user takes. Arming is
//     cleared by returning to the form (typing/clicking inside the dialog),
//     never by a clock.
//   • exit animation: callers render `{open && <Modal …/>}`, so the modal
//     defers the caller's onClose while base-ui's data-closed animation
//     plays (lib/exit-animation's OVERLAY_EXIT_MS).
// The body keeps the `modal-body deploy-body` classes so the existing form
// vocabulary keyed off .deploy-body keeps working mid-migration.
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from "react";
import { useDeferredClose } from "@platform/lib/hooks";
import { OVERLAY_EXIT_MS } from "@platform/lib/exit-animation";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { XIcon } from "lucide-react";
import { CLOSE_CONTROL_SELECTOR, decideClose, isDisarmingInteraction } from "./dirty-guard";

export interface ModalProps {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  // When true, Esc / backdrop / ✕ do NOT close (an action is running that must
  // not be abandoned). A modal whose action continues server-side regardless of
  // whether the dialog is open can instead pass false and stay closeable (#12).
  busy?: boolean;
  width?: number | string;
  footer?: ReactNode;
  initialFocus?: RefObject<HTMLElement | null>;
  // When dirty, the first close attempt is intercepted with an inline hint and
  // the next one actually closes (RowEditorModal). Arming is cleared by
  // interacting with the form again, not by a timeout.
  dirty?: boolean;
  // Extra class on the dialog for per-modal width/padding tweaks
  // (e.g. "templates-editor", "templates-import").
  dialogClassName?: string;
  // Tooltip for the ✕ button (e.g. "the action keeps running" for a busy={false} modal).
  closeTitle?: string;
  // Drop the `deploy-body` form vocabulary from the body — for hosting a
  // component lifted verbatim from a page (D489); every FORM modal keeps the
  // default.
  plainBody?: boolean;
}

export function Modal({
  title,
  onClose,
  children,
  busy = false,
  width,
  footer,
  initialFocus,
  dirty = false,
  dialogClassName,
  closeTitle,
  plainBody = false,
}: ModalProps) {
  // Exit animation: `open` flips false so base-ui plays its data-closed
  // animation, and the caller's onClose (which unmounts us) is deferred until
  // the animation has had its frames.
  const { closing, requestClose } = useDeferredClose(onClose, OVERLAY_EXIT_MS);
  const dialogRef = useRef<HTMLDivElement>(null);
  const [confirmClose, setConfirmClose] = useState(false);

  const attemptClose = useCallback(() => {
    // Armed is a latch, not a countdown: no timer re-clears it, so a press that
    // finds the guard already armed always closes. Rules live in dirty-guard.ts.
    const decision = decideClose({ busy, dirty, armed: confirmClose });
    if (decision === "block") return;
    if (decision === "arm") {
      setConfirmClose(true);
      return;
    }
    requestClose();
  }, [busy, dirty, confirmClose, requestClose]);

  // DISARM ON RETURNING TO THE FORM — the other half of the dirty guard. Any
  // real interaction inside the dialog (typing, changing a field, pointing at
  // something) means "no, I'm still editing" and resets the guard, so the form
  // is never left one stray click from being discarded. Deliberately NOT
  // disarming: presses on the ✕ itself, Escape, and Tab/Shift (keyboard travel
  // back to the ✕ must not undo the arming en route).
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog || !confirmClose) return;
    const handle = (key: string | null) => (e: Event) => {
      const target = e.target as Element | null;
      const inClose = !!target?.closest?.(CLOSE_CONTROL_SELECTOR);
      if (isDisarmingInteraction(key, inClose)) setConfirmClose(false);
    };
    const disarm = handle(null);
    const onKey = (e: Event) => handle((e as KeyboardEvent).key)(e);
    dialog.addEventListener("pointerdown", disarm);
    dialog.addEventListener("input", disarm);
    dialog.addEventListener("change", disarm);
    dialog.addEventListener("keydown", onKey);
    return () => {
      dialog.removeEventListener("pointerdown", disarm);
      dialog.removeEventListener("input", disarm);
      dialog.removeEventListener("change", disarm);
      dialog.removeEventListener("keydown", onKey);
    };
  }, [confirmClose]);

  const dialogStyle: CSSProperties | undefined =
    width !== undefined ? { width, maxWidth: "calc(100% - 2rem)" } : undefined;

  return (
    <Dialog
      open={!closing}
      // base-ui reports every close intent (Esc, backdrop, ✕) here; the app's
      // gate decides. `open` is controlled, so a blocked or merely-arming
      // attempt leaves the dialog exactly where it was.
      onOpenChange={(open) => {
        if (!open) attemptClose();
      }}
    >
      <DialogContent
        ref={dialogRef}
        showCloseButton={false}
        initialFocus={initialFocus as React.RefObject<HTMLElement> | undefined}
        className={cn("w-auto min-w-96 gap-0 p-0 sm:max-w-2xl", dialogClassName)}
        style={dialogStyle}
      >
        <DialogHeader className="modal-head flex-row items-center justify-between gap-3 border-b border-border px-4 py-3">
          <DialogTitle>{title}</DialogTitle>
          {/* Not rendered while busy: every busy modal that must not be
              abandoned has a real way out in its own footer, so the corner is
              empty for those seconds rather than occupied by something inert. */}
          {!busy && (
            <Button
              variant="ghost"
              size="icon-sm"
              className="modal-close -mr-1"
              // Armed: the control that was pressed changes too — warning ink
              // for as long as the armed state lasts, so what the user sees
              // and what the next press will do can never disagree.
              style={confirmClose ? { color: "var(--warning)" } : undefined}
              aria-label={confirmClose ? "Close and discard changes" : "Close"}
              title={confirmClose ? "Press again to discard" : (closeTitle ?? "Close")}
              onClick={attemptClose}
            >
              <XIcon />
            </Button>
          )}
        </DialogHeader>
        <div className={cn("modal-body px-4 py-4", plainBody ? "modal-body-plain" : "deploy-body")}>
          {children}
        </div>
        {(footer || confirmClose) && (
          <div className="modal-footer flex flex-wrap items-center justify-end gap-2 border-t border-border px-4 py-3">
            {confirmClose && (
              <span className="modal-dirty-hint mr-auto text-xs text-muted-foreground" role="status">
                Unsaved changes — close again to discard
              </span>
            )}
            {footer}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default Modal;
