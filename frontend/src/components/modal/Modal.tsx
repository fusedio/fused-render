// Shared modal chassis for every dialog in the app (SPEC: modal/form design
// system). Renders overlay > dialog with the a11y contract every modal needs:
//   • role="dialog" + aria-modal + aria-labelledby → the h2 (stable useId)
//   • focus trap: Tab/Shift+Tab cycle within the dialog; on mount focus
//     `initialFocus` (or the first focusable), on unmount restore the element
//     that was focused when the modal opened.
//   • Esc / backdrop / ✕ close, gated by `busy`; ✕ disabled while busy.
//   • optional `dirty` guard: the first close attempt shows an inline
//     "close again to discard" hint and only the second (within ~2s) closes.
// Chrome reuses the existing .deploy-* CSS (the body carries both `modal-body`
// and `deploy-body` so descendant skins that key off .deploy-body keep working).
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

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

export interface ModalProps {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  // When true, Esc / backdrop / ✕ do NOT close (an action is running that must
  // not be abandoned). Note: DeployModal deliberately passes false — its action
  // continues server-side and the dialog stays closeable (#12).
  busy?: boolean;
  width?: number | string;
  footer?: ReactNode;
  initialFocus?: RefObject<HTMLElement | null>;
  // When dirty, the first close attempt is intercepted with an inline hint and
  // only a second attempt within ~2s actually closes (RowEditorModal).
  dirty?: boolean;
  // Extra class on the dialog for per-modal width/padding tweaks
  // (e.g. "templates-editor", "templates-import").
  dialogClassName?: string;
  // Tooltip for the ✕ button (e.g. DeployModal's "the action keeps running").
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
  const dialogRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<Element | null>(null);
  const [confirmClose, setConfirmClose] = useState(false);
  const confirmTimer = useRef<number | null>(null);

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

  useEffect(
    () => () => {
      if (confirmTimer.current !== null) window.clearTimeout(confirmTimer.current);
    },
    [],
  );

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
    if (busy) return;
    if (dirty && !confirmClose) {
      setConfirmClose(true);
      if (confirmTimer.current !== null) window.clearTimeout(confirmTimer.current);
      confirmTimer.current = window.setTimeout(() => setConfirmClose(false), 2000);
      return;
    }
    onClose();
  }, [busy, dirty, confirmClose, onClose]);

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

  return (
    <div
      className="modal-overlay deploy-overlay"
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
          <button
            type="button"
            className="modal-close deploy-close"
            aria-label="Close"
            title={closeTitle ?? "Close"}
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
    </div>
  );
}

export default Modal;
