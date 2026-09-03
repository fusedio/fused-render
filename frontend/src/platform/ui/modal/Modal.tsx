// Shared modal chassis for every dialog in the app (SPEC: modal/form design
// system), built on the shadcn/base-ui Dialog. Callers keep rendering it as
// `{open && <Modal …/>}` and get, without changes:
//   • role="dialog" + aria-modal + aria-labelledby → the title (base-ui)
//   • focus trap + focus return: base-ui moves focus into the popup on mount
//     (`initialFocus` ref → `[autofocus]` → first tabbable) and restores the
//     previously focused element on unmount.
//   • Esc / backdrop / ✕ close, gated by `busy`; ✕ not rendered while busy.
//   • optional `dirty` guard: the first close attempt arms the ✕ and shows an
//     inline "close again to discard" hint; the NEXT close attempt discards,
//     however long the user takes over it. Arming is cleared by going back to
//     the form (typing/clicking inside the dialog), not by a clock — see
//     `attemptClose` and the disarm effect below for why.
//     BOTH halves matter — the hint says it in words, the button says it where
//     the press happened. See the ✕ below.
//
// `modal="trap-focus"` rather than `true`: focus is trapped, but pointer
// interaction outside stays enabled so the notification stack (toasts with a
// "Reconnect" action, the server card) keeps working over an open dialog, as it
// always has. Outside presses are therefore NOT a dismissal — only the backdrop
// itself is (its own click handler below), which is also what keeps a click on
// a toast from arming the dirty guard.
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from "react";
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";
import { XIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Dialog, DialogClose, DialogOverlay, DialogPortal, DialogTitle } from "@platform/shadcn/ui/dialog";
import { useDeferredClose } from "@platform/lib/hooks";
import { OVERLAY_EXIT_MS } from "@platform/lib/exit-animation";
import { bucketBadge } from "@platform/ui/status-colors";
import {
  CLOSE_CONTROL_SELECTOR,
  decideClose,
  isDisarmingInteraction,
} from "./dirty-guard";

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

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
  // Extra class on the dialog for per-modal width/padding tweaks.
  dialogClassName?: string;
  // Tooltip for the ✕ button (e.g. "the action keeps running" for a busy={false} modal).
  closeTitle?: string;
  // Body without the form column layout (flex-col gap-3): for hosting a
  // component lifted verbatim from a page that arrives already laid out (the
  // /apps composer in the sidebar's New app modal, D489).
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
  // Exit animation. Callers render this as `{open && <Modal …/>}`, so the modal
  // cannot keep itself mounted — it defers the onClose that makes the caller
  // unmount it, and flips `open` false in the meantime so base-ui plays its
  // data-closed exit (lib/exit-animation). Only the chassis' own close paths
  // (Esc / backdrop / ✕) animate; a caller that calls its own onClose from a
  // footer action closes immediately.
  const { closing, requestClose } = useDeferredClose(onClose, OVERLAY_EXIT_MS);
  const popupRef = useRef<HTMLDivElement>(null);
  const [confirmClose, setConfirmClose] = useState(false);

  // DISARM ON RETURNING TO THE FORM — the other half of the guard.
  //
  // Arming used to lapse on a 2s timer, which made the guard unescapable rather
  // than safe: a press at t=0 armed, the timer disarmed at t=2s, and a press at
  // t=2.6s armed *again*. Anyone pressing ✕ slower than every two seconds — i.e.
  // anyone who stops to read the hint the first press just showed them — looped
  // forever, and a modal with no Cancel button had no way out but Save (QA,
  // 2026-08-18: presses at 2.6/5.2/7.8/10.4s all left the dialog open).
  //
  // So the clock is gone. Once armed, the next ✕/Esc/backdrop press discards,
  // however long the user takes over the decision. What ends the armed state is
  // the user answering the question the other way: going back to the form. Any
  // real interaction inside the dialog — typing, changing a field, pointing at
  // something — means "no, I'm still editing", and the guard resets so the form
  // is never left one stray click from being discarded.
  //
  // Deliberately NOT disarming: presses on the ✕ itself (that IS the second
  // press), Escape (same), and Tab/Shift (navigating back to the ✕ to press it
  // with the keyboard must not undo the arming en route).
  useEffect(() => {
    const dialog = popupRef.current;
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

  // Focus on open: the caller's `initialFocus` ref wins; then a field that asked
  // for it with `autoFocus`; otherwise the first focusable in the body/footer,
  // so focus doesn't land on the header ✕ (base-ui's own default would pick it,
  // being the first tabbable in DOM order); finally the popup itself.
  const pickInitialFocus = useCallback(() => {
    if (initialFocus?.current) return initialFocus.current;
    const popup = popupRef.current;
    if (!popup) return true;
    const auto = popup.querySelector<HTMLElement>("[autofocus]");
    if (auto) return auto;
    const focusables = Array.from(popup.querySelectorAll<HTMLElement>(FOCUSABLE));
    return focusables.find((el) => !el.closest(CLOSE_CONTROL_SELECTOR)) ?? focusables[0] ?? popup;
  }, [initialFocus]);

  const dialogStyle: CSSProperties | undefined = width !== undefined ? { width } : undefined;

  return (
    <Dialog
      open={!closing}
      modal="trap-focus"
      disablePointerDismissal
      onOpenChange={(open) => {
        if (!open) attemptClose();
      }}
    >
      <DialogPortal>
        <DialogOverlay
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) attemptClose();
          }}
        />
        <DialogPrimitive.Popup
          ref={popupRef}
          data-slot="dialog-content"
          initialFocus={pickInitialFocus}
          style={dialogStyle}
          className={cn(
            "fixed top-[8vh] left-1/2 z-50 flex max-h-[85vh] w-[min(600px,100%)] max-w-[calc(100%-2rem)] -translate-x-1/2 flex-col rounded-lg border border-border bg-popover text-sm text-popover-foreground shadow-sm outline-none duration-100",
            "data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95 motion-reduce:animate-none",
            dialogClassName,
          )}
        >
          <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
            <DialogTitle className="min-w-0 truncate text-base font-semibold">{title}</DialogTitle>
            {/* ARMED, ON THE BUTTON ITSELF. The footer hint below says the same
                thing, and on its own it was not enough: the press happens at the
                top-right corner of the card and the hint appears at the bottom-left
                of the footer — 12px, muted, up to 500px away, and gone again in two
                seconds. A user watching their own cursor saw a click that did
                nothing (QA, 2026-08-18).

                So the control that was pressed changes too. Same two-step guard,
                same second press to discard — this only makes the first press
                visible where the user is already looking. The tint lasts as long
                as the armed state itself does (no 2s fade), so what the user sees
                and what the next press will do can never disagree. Orange is the
                "waiting on the user" bucket (status-colors), warm rather than
                destructive: discarding a draft is not deleting a task. */}
            {/* NOT RENDERED WHILE BUSY (2026-08-24). It used to be drawn and
                `disabled={busy}`, which is this app's usual posture — a control
                that vanishes teaches nothing, and a disabled one with a reason in
                its `title` teaches where the door is and why it is shut.

                That argument needs the reason to be REACHABLE, and here it is
                not: a disabled button takes no pointer events, so its title never
                appears, and a busy modal is exactly the moment a user reaches for
                the corner. Akshil, on the mount sign-in: "when mounting we have a
                X button, i don't think that works". It worked as specified and
                was indistinguishable from broken.

                Every busy modal that must not be abandoned has a real way out in
                its own footer (the mount flow's Cancel stands the sign-in down and
                frees rclone's callback port, which is what closing out from under
                it would strand). So the corner is empty for those seconds rather
                than occupied by something inert. */}
            {!busy && (
              <DialogClose
                data-modal-close=""
                render={
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    className={cn("-mr-1 shrink-0" + (confirmClose ? " is-armed" : ""), confirmClose && bucketBadge.orange)}
                  />
                }
                // The label carries the state for a screen reader, which has no
                // corner to look at. The footer hint is `role="status"` and is
                // announced too; this is what the button itself answers to when the
                // user tabs back to it.
                aria-label={confirmClose ? "Close and discard changes" : "Close"}
                title={confirmClose ? "Press again to discard" : (closeTitle ?? "Close")}
              >
                <XIcon />
              </DialogClose>
            )}
          </div>
          <div
            className={cn(
              "min-h-0 flex-1 overflow-y-auto px-4 pt-3.5 pb-4",
              !plainBody && "flex flex-col gap-3",
            )}
          >
            {children}
          </div>
          {(footer || confirmClose) && (
            <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border px-4 py-3">
              {confirmClose && (
                <span className="mr-auto text-xs text-muted-foreground" role="status">
                  Unsaved changes — close again to discard
                </span>
              )}
              {footer}
            </div>
          )}
        </DialogPrimitive.Popup>
      </DialogPortal>
    </Dialog>
  );
}

export default Modal;
