// Shared modal chassis for every dialog in the app (SPEC: modal/form design
// system). Renders overlay > dialog with the a11y contract every modal needs:
//   • role="dialog" + aria-modal + aria-labelledby → the h2 (stable useId)
//   • focus trap: Tab/Shift+Tab cycle within the dialog; on mount focus
//     `initialFocus` (or the first focusable), on unmount restore the element
//     that was focused when the modal opened.
//   • Esc / backdrop / ✕ close, gated by `busy`; ✕ disabled while busy.
//   • optional `dirty` guard: the first close attempt arms the ✕ and shows an
//     inline "close again to discard" hint; the NEXT close attempt discards,
//     however long the user takes over it. Arming is cleared by going back to
//     the form (typing/clicking inside the dialog), not by a clock — see
//     `attemptClose` and the disarm effect below for why.
//     BOTH halves matter — the hint says it in words, the button says it where
//     the press happened. See the ✕ below.
// Chrome reuses the existing .deploy-* CSS (the body carries both `modal-body`
// and `deploy-body` so descendant skins that key off .deploy-body keep working,
// e.g. RowEditorModal).
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { useDeferredClose } from "@platform/lib/hooks";
import { OVERLAY_EXIT_MS } from "@platform/lib/exit-animation";
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
  // Extra class on the dialog for per-modal width/padding tweaks
  // (e.g. "templates-editor", "templates-import").
  dialogClassName?: string;
  // Tooltip for the ✕ button (e.g. "the action keeps running" for a busy={false} modal).
  closeTitle?: string;
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
}: ModalProps) {
  const titleId = useId();
  // Exit animation. Callers render this as `{open && <Modal …/>}`, so the modal
  // cannot keep itself mounted — it defers the onClose that makes the caller
  // unmount it, and paints `.closing` in the meantime (lib/exit-animation).
  // Consequences worth knowing: the caller's state (and therefore the
  // overlay-lock count in lib/ui-overlay, which is keyed on that state) stays
  // held for the whole exit, and focus restore still runs on the real unmount.
  // Only the chassis' own close paths (Esc / backdrop / ✕) animate; a caller
  // that calls its own onClose from a footer action closes immediately.
  const { closing, requestClose } = useDeferredClose(onClose, OVERLAY_EXIT_MS);
  const dialogRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<Element | null>(null);
  const [confirmClose, setConfirmClose] = useState(false);

  // Store the previously-focused element, move focus into the dialog on mount,
  // and restore it on unmount. Callers win: an `initialFocus` ref takes
  // precedence, and a field that already grabbed focus via `autoFocus` (React
  // focuses those during commit, before this effect) is left alone. Otherwise
  // prefer the first focusable in the body/footer so focus doesn't land on the
  // header ✕.
  useEffect(() => {
    restoreRef.current = document.activeElement;
    const dialog = dialogRef.current;
    if (initialFocus?.current) {
      initialFocus.current.focus();
    } else if (!(dialog && dialog.contains(document.activeElement))) {
      const focusables = Array.from(
        dialog?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [],
      );
      const target =
        dialog?.querySelector<HTMLElement>("[autofocus]") ??
        focusables.find((el) => !el.closest(".modal-head")) ??
        focusables[0] ??
        dialog;
      target?.focus();
    }
    return () => {
      const el = restoreRef.current as HTMLElement | null;
      el?.focus?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  // Reclaim focus when it would escape the dialog while the modal is mounted —
  // e.g. a focused chip-✕ removes its own chip, or a nested popover's focused
  // element unmounts, dropping activeElement to <body>. Without this, Esc/Tab
  // handling (attached to the dialog subtree) goes dead. Reclaim on the next
  // frame and only when focus really ended up on body/outside — never fight a
  // nested [role="dialog"] (a popover) that legitimately holds focus.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const onFocusOut = (e: FocusEvent) => {
      const next = e.relatedTarget as Node | null;
      if (next && dialog.contains(next)) return;
      requestAnimationFrame(() => {
        if (!dialog.isConnected) return; // modal already unmounted
        const active = document.activeElement;
        if (active && active !== document.body) {
          if (dialog.contains(active)) return;
          // Focus moved into some other open dialog/popover — leave it alone.
          if ((active as Element).closest?.('[role="dialog"]')) return;
          return;
        }
        (dialog.querySelector<HTMLElement>(FOCUSABLE) ?? dialog).focus();
      });
    };
    dialog.addEventListener("focusout", onFocusOut);
    return () => dialog.removeEventListener("focusout", onFocusOut);
  }, []);

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

  // Esc is handled at the document level (bubble phase), not on the dialog
  // subtree — so it keeps working even if focus momentarily escapes to <body>.
  // Nested popovers (TemplatePicker, chip-draft inputs) stopPropagation on
  // their own Esc, which also stops the native event before it reaches this
  // document listener, so they close themselves without closing the modal.
  useEffect(() => {
    const onDocKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") attemptClose();
    };
    document.addEventListener("keydown", onDocKey);
    return () => document.removeEventListener("keydown", onDocKey);
  }, [attemptClose]);

  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const nodes = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
      (el) => el.offsetParent !== null || el === document.activeElement,
    );
    if (nodes.length === 0) {
      e.preventDefault();
      dialog.focus();
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement;
    if (e.shiftKey) {
      if (active === first || !dialog.contains(active)) {
        e.preventDefault();
        last.focus();
      }
    } else if (active === last || !dialog.contains(active)) {
      e.preventDefault();
      first.focus();
    }
  };

  const dialogStyle: CSSProperties | undefined = width !== undefined ? { width } : undefined;

  // Portal to <body>: modals mount from arbitrary spots (e.g. AI Models'
  // NewJobModal inside a page's toolbar), and ancestor-scoped `button` rules
  // were leaking into the dialog chrome (a boxed ✕ from just one caller's styles).
  return createPortal(
    <div
      className={"modal-overlay deploy-overlay" + (closing ? " closing" : "")}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) attemptClose();
      }}
      onKeyDown={onKeyDown}
    >
      <div
        ref={dialogRef}
        className={"modal-dialog deploy-dialog" + (dialogClassName ? " " + dialogClassName : "")}
        style={dialogStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="modal-head deploy-head">
          <h2 id={titleId}>{title}</h2>
          {/* ARMED, ON THE BUTTON ITSELF. The footer hint below says the same
              thing, and on its own it was not enough: the press happens at the
              top-right corner of the card and the hint appears at the bottom-left
              of the footer — 12px, muted, up to 500px away, and gone again in two
              seconds. A user watching their own cursor saw a click that did
              nothing (QA, 2026-08-18).

              So the control that was pressed changes too. Same two-step guard,
              same second press to discard — this only makes the first press
              visible where the user is already looking. The amber now lasts as
              long as the armed state itself does (no 2s fade), so what the user
              sees and what the next press will do can never disagree. `is-armed`
              is the vocabulary the New task card's Delete button already uses for
              exactly this "the next press does it" state. */}
          <button
            type="button"
            className={"modal-close deploy-close" + (confirmClose ? " is-armed" : "")}
            // The label carries the state for a screen reader, which has no
            // corner to look at. The footer hint is `role="status"` and is
            // announced too; this is what the button itself answers to when the
            // user tabs back to it.
            aria-label={confirmClose ? "Close and discard changes" : "Close"}
            title={confirmClose ? "Press again to discard" : (closeTitle ?? "Close")}
            disabled={busy}
            onClick={attemptClose}
          >
            ✕
          </button>
        </div>
        <div className="modal-body deploy-body">{children}</div>
        {(footer || confirmClose) && (
          <div className="modal-footer">
            {confirmClose && (
              <span className="modal-dirty-hint" role="status">
                Unsaved changes — close again to discard
              </span>
            )}
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

export default Modal;
