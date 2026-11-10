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
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { useDeferredClose } from "@platform/lib/hooks";
import { isTopmost, popModal, pushModal } from "./esc-stack";
import { popoverDrag, popoverPlacement, type PopoverBox, type PopoverEdge, type PopoverLimits } from "./popover-place";
import { OVERLAY_EXIT_MS } from "@platform/lib/exit-animation";
import {
  CLOSE_CONTROL_SELECTOR,
  backdropPressCloses,
  decideClose,
  isDisarmingInteraction,
} from "./dirty-guard";

// `iframe` is in the list because a framed document IS a focusable stop — the
// Tasks page's card popup is a chat in a frame, and a trap that skipped it would
// cycle the head's two buttons forever while the thing the dialog exists for
// sat one Tab away and unreachable (Bugbot, #1009).
const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),iframe,[tabindex]:not([tabindex="-1"])';

/** What the chassis hands a caller's own head (`ModalProps.head`). */
export interface ModalHeadContext {
  titleId: string;
  /** The ✕'s guarded close: busy and dirty guards, the exit animation. */
  close: () => void;
  /** No close while an action that must not be abandoned is running. */
  busy: boolean;
  /** A dirty close was asked for once: the next press discards. */
  armed: boolean;
  parts: {
    title: ReactNode;
    titleLead?: ReactNode;
    titleAside?: ReactNode;
    headActions?: ReactNode;
  };
}

export interface ModalProps {
  title: ReactNode;
  /**
   * The dialog's ACCESSIBLE NAME, when it is not the visible title.
   *
   * The visible bar is often the short form — a heading has the surrounding
   * page for context, while a spoken dialog name has nothing. T:4405 is the
   * case this exists for: the record's bar says "What was sent" and its
   * `aria-label` says "What was sent to the agent", which is the half that says
   * WHOSE record it is.
   *
   * OPTIONAL AND OFF BY DEFAULT: with it absent the dialog keeps
   * `aria-labelledby` pointing at the `h2`, so every existing caller — and the
   * whole flag-off shell — renders byte-identical markup. Only a caller that
   * passes it gets `aria-label` instead, which is the correct precedence (an
   * explicit `aria-label` wins over `aria-labelledby`, so setting both would
   * leave the weaker one dead in the tree).
   */
  ariaLabel?: string;
  /**
   * ONE MARK BESIDE THE HEADING, on the same row and at the heading's left —
   * the New task card's `from TASK-003` chip (NewJobModal, design.md B).
   *
   * It looks like part of the title and it is not allowed to BE part of one: the
   * `h2` is what `aria-labelledby` points at, so a control rendered inside it
   * would make the dialog's own name "New task from TASK-003" and would put an
   * interactive element inside a heading — a thing a screen reader's heading
   * walk reads out and cannot press.
   *
   * OPTIONAL AND OFF BY DEFAULT, like `ariaLabel` above: with it absent the head
   * renders the bare `h2` it always has, so every existing caller's markup is
   * byte-identical. A caller that passes it gets the two wrapped in
   * `.modal-head-title`, which is the flex row that keeps the old placement.
   *
   * NOT `headActions`, which is the other end of the same row: those sit against
   * the ✕ (`margin-left: auto`) because they are controls that act on the
   * dialog. This is a caption on the title.
   */
  titleAside?: ReactNode;
  /**
   * A control that sits BEFORE the title, in the same `.modal-head-title` row
   * (the New task modal's agent picker: `[ Claude ▾ › ] New task`). Same rule as
   * `titleAside`: it is rendered beside the `h2`, never inside it, because the
   * `h2` is what `aria-labelledby` names the dialog by and a `<button>` has no
   * place in a heading.
   *
   * OPTIONAL AND OFF BY DEFAULT: with neither this nor `titleAside` the head
   * renders the bare `h2`, byte-identical for every existing caller.
   */
  titleLead?: ReactNode;
  /**
   * THE CALLER'S OWN HEAD, in place of the chassis' `.modal-head` — the shared
   * task head (shell/TaskHead) on the New task card's open chat and the Cards
   * popup, so a task reads the same in a dialog as in every other view of it.
   * `title`, `titleAside`, `titleLead`, `headActions` and the ✕ are then not
   * drawn: the head brings its own corner control.
   *
   * A FUNCTION OF THE CHASSIS' CONTEXT: `titleId`, because the dialog is still
   * named by its heading (`aria-labelledby`, unless `ariaLabel` says
   * otherwise) and the head puts the id on its own title element; and `close`,
   * the ✕'s own guarded close (busy, dirty, the exit animation), which the
   * head's corner control must call instead of the caller's raw `onClose`.
   * Absent: the head every caller has always had, byte for byte.
   *
   * …AND THE PIECES THE CALLER PASSED for the chassis' own head (`parts`), so a
   * caller whose head is built from them (the New task card's form: the agent
   * crumb, the name, the source chip, the Folder and When pills) hands the
   * same nodes to its own head instead of building them twice; and `busy` /
   * `armed`, the two states the ✕ shows (hidden while busy, amber once a
   * dirty close has been asked for).
   */
  head?: (ctx: ModalHeadContext) => ReactNode;
  onClose: () => void;
  children: ReactNode;
  // When true, Esc / backdrop / ✕ do NOT close (an action is running that must
  // not be abandoned). A modal whose action continues server-side regardless of
  // whether the dialog is open can instead pass false and stay closeable (#12).
  busy?: boolean;
  width?: number | string;
  footer?: ReactNode;
  initialFocus?: RefObject<HTMLElement | null>;
  // RE-RUN `initialFocus` WHEN THE REF FILLS LATE. The mount effect below reads
  // `initialFocus.current` once, during the dialog's own commit — which is the
  // right moment for a render-time ref (an iframe element) and too early for one
  // an effect inside a LAZY child fills (TaskPeek's native chat: the composer's
  // textarea does not exist until the chat chunk resolves, so focus fell back to
  // the head's ✕). A caller that knows when its ref is ready bumps this, and the
  // focus is taken then — but only if nothing in the body has the caret yet, so
  // a reader who has already clicked or typed somewhere is never yanked away.
  focusSignal?: number;
  // When dirty, the first close attempt is intercepted with an inline hint and
  // the next one actually closes (RowEditorModal). Arming is cleared by
  // interacting with the form again, not by a timeout.
  dirty?: boolean;
  // Extra class on the dialog for per-modal width/padding tweaks
  // (e.g. "templates-editor", "templates-import").
  dialogClassName?: string;
  // Tooltip for the ✕ button (e.g. "the action keeps running" for a busy={false} modal).
  closeTitle?: string;
  // Controls that sit in the head BESIDE the ✕ — the same row, the same weight
  // (`.deploy-close`), to its left. For a dialog whose actions are about the
  // thing it shows rather than about the form in it (the Tasks page's card
  // popup: open in List, open in Explorer, archive), where a footer row of
  // buttons under a live chat read as a form's Save/Cancel.
  headActions?: ReactNode;
  // Drop the `deploy-body` form vocabulary from the body — its descendant
  // `button`/`p` rules (fields.css) out-specify a component's own classes and
  // would re-skin a surface that arrives already designed. For hosting a
  // component lifted verbatim from a page (the /apps composer in the sidebar's
  // New app modal, D489); every FORM modal keeps the default.
  plainBody?: boolean;
  /** The ✕ in the head's LEFT corner, before the title — the rail cards'
   *  seat (TasksPeek, the bot card). Default: the right end. */
  closeAtStart?: boolean;
  /** No ✕ at all (a card whose Back is the way out while it shows a chat). */
  hideClose?: boolean;
  // WHERE TO PORTAL, when it is not `document.body` (D890, the plan popup).
  // Read ONCE, at mount (see the lazy `useState` below) — a portal target does
  // not change out from under an already-open dialog. Returning `null` (not
  // framed, no `window.top`, a cross-origin throw, a test shim) falls back to
  // this document's own `document.body`, exactly as every caller that omits
  // this prop already gets. Every internal `document.*` reference below reads
  // off the RESOLVED container's owner document instead of the global
  // `document`, so Esc handling and focus restore still work when the
  // container is a different (top) document than the one this script's own
  // `document` global points at.
  getContainer?: () => Element | null;
  /**
   * POPOVER PRESENTATION (owner, 2026-11-07: the rail's + opens the New task
   * card "like the popover for task chats" — the same card, floating beside
   * the rail, not a modal). The same head, body, footer, Esc stack, busy and
   * dirty guards; what changes:
   *   • no scrim — the page stays live (`.modal-popover`, pointer-events only
   *     on the card), no `aria-modal`, no Tab trap, no focus reclaim;
   *   • placed to the LEFT of `beside` (or `anchor`), top-aligned with
   *     `anchor`, clamped inside the viewport (`popover-place.ts`), and kept
   *     there as the card's height or the window changes;
   *   • an OUTSIDE PRESS closes it like the ✕ (unless `pinned`) — except a
   *     press on the anchor (the opener's own), inside another dialog stacked over
   *     it, or inside a portaled popper (`[data-radix-popper-content-wrapper]`,
   *     `[data-modal-popover-keep]`): the card's own pill menus live inside
   *     the dialog, so containment already covers them;
   *   • focus goes back to the anchor on close only when it would otherwise
   *     be lost (on the card, or nowhere) — an outside press that put the
   *     caret elsewhere keeps it there.
   * Absent: the modal, byte-identical.
   */
  popover?: {
    anchor: RefObject<HTMLElement | null>;
    /** Whose LEFT edge the card stands beside (the rail); `anchor`'s if absent. */
    beside?: RefObject<HTMLElement | null>;
    /** Where the card's top is level with, when not `anchor`'s own box (the
     *  rail's draft cards stand at the list's first ring, as window A does,
     *  whichever ring opened them). Null: `anchor`'s. */
    anchorBox?: () => { left: number; top: number } | null;
    /** Bumped by the opener to close the card the chassis' own way (the
     *  exit animation, the busy and dirty guards) — the rail's hover preview
     *  timing out. */
    closeSignal?: number;
    /**
     * HOVER PREVIEW AND PIN (owner, 2026-11-07: "hover on plus icon should
     * show the card as well, and click does cement it unless you click on the
     * cross icon"). Absent: the popover above, unchanged. Otherwise:
     *   • `false` — a PREVIEW: it takes no focus at mount (a hover must not
     *     pull the caret off the page) and gives none back at close; an
     *     outside press and Escape close it; the first thing the reader does
     *     INSIDE it — a press, the focus arriving on a field — calls `onPin`;
     *   • `true` — PINNED: only its ✕ (and the caller's own actions, Create)
     *     close it — an outside press and Escape do not.
     */
    pinned?: boolean;
    /** The reader acted inside a preview (`pinned: false`): pin it. */
    onPin?: () => void;
    /** The pointer entered / left the card (the opener's hover grace). */
    onPointerEnter?: () => void;
    onPointerLeave?: () => void;
    /** A FIXED SIZE (owner, 2026-11-07: the rail's + "same width/height as
     *  window A"): the card takes this width and height — clamped inside the
     *  viewport, an inset on every side (popover-place.ts) — and its body
     *  scrolls inside, so the ✕ and the footer are never clipped. Absent:
     *  `width`, and the content's own height. */
    size?: { width: number; height: number };
    /** RESIZABLE (owner, 2026-11-08: the +'s card "is supposed to be window
     *  A, but resize is not working for that modal"): a sized card grows the
     *  rail card's grips — its two left corners and its left, top and bottom
     *  edges (`.modal-popover-grip`, the rail's own look) — and the drag
     *  resizes it in place (popover-place.ts `popoverDrag`). The release
     *  reports the size dragged to (`onResize`); the caller remembers it and
     *  hands it back as `size`, which re-places the card level with its
     *  anchor again. Absent: no grips. */
    /** WHERE IT STANDS, THE CALLER'S WAY (owner, 2026-11-08: the +'s card is
     *  window A's and must keep clear of window B's card, as the rail's own
     *  cards do — rail-peek-lib `placeClear`): asked for the box on every
     *  measure, with the size wanted; null falls back to `popoverPlacement`
     *  above. `placeKey` re-measures when it changes (window B's box). */
    place?: (want: { width: number; height: number }) => PopoverBox | null;
    placeKey?: unknown;
    /** An element whose resize re-measures the placement — the workspace
     *  row, which a sidebar collapsing reshapes while the dialog and the
     *  window keep their sizes (CodeRabbit on #2189). */
    watch?: () => Element | null;
    resize?: PopoverLimits & {
      /** A floor the bottom may not be dragged past (viewport y), asked at
       *  the drag's start with the card's box: window B's card's top, for a
       *  card standing above it (rail-peek-lib `bottomClear`). */
      maxBottom?: (box: { left: number; top: number; width: number; height: number }) => number | undefined;
      /** The box the drag keeps the card inside (the rail's workspace row);
       *  the viewport if absent or null. Asked at the drag's start. */
      bounds?: () => { left: number; top: number; width: number; height: number } | null;
      /** The size let go at, and WHICH of its two dimensions the drag moved
       *  (an edge drag moves one): the caller keeps its remembered value for
       *  the other, so a width the window clamped is not saved over it by a
       *  height-only drag (CodeRabbit on #2189). */
      onResize: (size: { width: number; height: number }, changed: { width: boolean; height: boolean }) => void;
    };
  };
}

const POPOVER_GRIPS: readonly PopoverEdge[] = ["l", "t", "b", "tl", "bl"];

/** Presses inside these never dismiss a popover-mode dialog (Modal `popover`). */
const POPOVER_KEEP = '.modal-overlay, [role="dialog"], [data-radix-popper-content-wrapper], [data-modal-popover-keep]';

export function Modal({
  title,
  ariaLabel,
  titleAside,
  titleLead,
  head,
  onClose,
  children,
  busy = false,
  width,
  footer,
  initialFocus,
  focusSignal = 0,
  dirty = false,
  dialogClassName,
  closeTitle,
  plainBody = false,
  closeAtStart = false,
  hideClose = false,
  headActions,
  getContainer,
  popover,
}: ModalProps) {
  const titleId = useId();
  // Resolved ONCE, at mount — see `getContainer`'s doc comment above.
  const [container] = useState<Element>(() => getContainer?.() ?? document.body);
  const ownerDoc = container.ownerDocument ?? document;
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
  // The popover's pin, as it is NOW — read by the document listeners and the
  // unmount below, which are bound once.
  const pinnedRef = useRef(popover?.pinned);
  pinnedRef.current = popover?.pinned;

  // Store the previously-focused element, move focus into the dialog on mount,
  // and restore it on unmount. Callers win: an `initialFocus` ref takes
  // precedence, and a field that already grabbed focus via `autoFocus` (React
  // focuses those during commit, before this effect) is left alone. Otherwise
  // prefer the first focusable in the body/footer so focus doesn't land on the
  // header ✕.
  useEffect(() => {
    // The trigger lives in THIS script's document, not necessarily the portal
    // target's: with `getContainer` pointing at a parent document, that
    // document's `activeElement` is the <iframe> element, and closing would
    // return focus to the frame instead of the Open control. Same document
    // (the default) -> `document === ownerDoc`, so nothing changes.
    restoreRef.current = document.activeElement;
    const dialog = dialogRef.current;
    // A hover preview takes no focus (header of `popover.pinned`).
    if (popover?.pinned === false) {
      // nothing
    } else if (initialFocus?.current) {
      initialFocus.current.focus();
    } else if (!(dialog && dialog.contains(ownerDoc.activeElement))) {
      const focusables = Array.from(
        dialog?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [],
      );
      const target =
        dialog?.querySelector<HTMLElement>("[autofocus]") ??
        focusables.find((el) => !el.closest(".modal-head, .modal-head-slot")) ??
        focusables[0] ??
        dialog;
      target?.focus();
    }
    return () => {
      if (popover) {
        // A popover closed by a press elsewhere: the caret went where the
        // reader sent it. Only focus that would be LOST (left on the card that
        // is going, or nowhere) goes back — to the anchor, the door it came by.
        const active = ownerDoc.activeElement as Element | null;
        const onCard = !!active && !!dialog?.contains(active);
        // A preview never had the caret unless it is on it now: nothing lost.
        if (pinnedRef.current === false && !onCard) return;
        const lost = !active || active === ownerDoc.body || !active.isConnected || onCard;
        if (!lost) return;
        // The CURRENT anchor (CodeRabbit on #2220): a card carried across a
        // page hop stands beside the new page's door, not the first one's.
        const back = (popoverRef.current?.anchor.current ?? popover.anchor.current ?? restoreRef.current) as HTMLElement | null;
        back?.focus?.({ preventScroll: true });
        return;
      }
      const el = restoreRef.current as HTMLElement | null;
      el?.focus?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The late half of the above: `focusSignal` changed, so the ref the caller
  // handed us may have filled since the mount effect read it. Skipped while the
  // caret is already on something in the BODY (the reader got there first, or
  // the ref was live at mount and this is the same element); taken when focus
  // sits on the chassis' own head — the ✕ the fallback lands on — or nowhere.
  useEffect(() => {
    if (!focusSignal) return;
    const el = initialFocus?.current;
    if (!el) return;
    const active = ownerDoc.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;
    if (active && dialog?.contains(active) && !active.closest(".modal-head, .modal-head-slot")) return;
    el.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusSignal]);

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
    // A popover is not modal: focus may leave it for the page.
    if (!dialog || popover) return;
    const onFocusOut = (e: FocusEvent) => {
      const next = e.relatedTarget as Node | null;
      if (next && dialog.contains(next)) return;
      requestAnimationFrame(() => {
        if (!dialog.isConnected) return; // modal already unmounted
        const active = ownerDoc.activeElement;
        if (active && active !== ownerDoc.body) {
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
  //
  // ONLY THE TOPMOST MODAL REACTS, which is what makes one press peel one
  // layer when a dialog is nested inside another (see `openModals`). The token
  // is this instance's own identity, registered for the life of the mount — the
  // exit animation included, since a dialog still on screen is still a layer.
  // When this dialog opened, for the scrim's grace period. A ref, not state:
  // read at press time, never rendered.
  const openedAt = useRef(performance.now());
  const token = useRef({});
  useEffect(() => {
    const mine = token.current;
    pushModal(mine);
    return () => popModal(mine);
  }, []);
  useEffect(() => {
    const onDocKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // A pinned popover closes by its ✕ only (header of `popover.pinned`).
      if (pinnedRef.current === true) return;
      if (!isTopmost(token.current)) return;
      attemptClose();
    };
    ownerDoc.addEventListener("keydown", onDocKey);
    return () => ownerDoc.removeEventListener("keydown", onDocKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attemptClose]);

  // POPOVER: the opener closes it the chassis' own way (the exit animation,
  // the busy and dirty guards) — `closeSignal` bumped by the opener.
  const closeSignal = popover?.closeSignal ?? 0;
  const seenSignal = useRef(closeSignal);
  useEffect(() => {
    if (closeSignal === seenSignal.current) return;
    seenSignal.current = closeSignal;
    attemptClose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [closeSignal]);

  // POPOVER: an outside press closes it, as the ✕ would (header of `popover`).
  // Capture phase, pointerdown — the house rule (platform/lib/dismissOnOutside):
  // the card is gone before whatever the reader aimed at reacts, and a row
  // that stops propagation cannot leave it undismissable.
  const popoverOn = !!popover;
  const popoverRef = useRef(popover);
  popoverRef.current = popover;
  const attemptCloseRef = useRef(attemptClose);
  attemptCloseRef.current = attemptClose;
  useEffect(() => {
    if (!popoverOn) return;
    const onDown = (e: Event) => {
      const t = e.target as Element | null;
      if (!t || typeof (t as Node).nodeType !== "number") return;
      const dialog = dialogRef.current;
      if (dialog && dialog.contains(t)) return;
      const anchor = popover?.anchor.current;
      if (anchor && anchor.contains(t)) return;
      const el = t.nodeType === 1 ? t : t.parentElement;
      if (el?.closest?.(POPOVER_KEEP)) return;
      // A dialog stacked over this one owns the press.
      if (!isTopmost(token.current)) return;
      // A pinned popover closes by its ✕ only (header of `popover.pinned`).
      if (pinnedRef.current === true) return;
      attemptCloseRef.current();
    };
    ownerDoc.addEventListener("pointerdown", onDown, true);
    return () => ownerDoc.removeEventListener("pointerdown", onDown, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [popoverOn]);

  const sizeW = popover?.size?.width;
  const sizeH = popover?.size?.height;
  // THE DRAG (header of `popover.resize`): the box while a grip is held,
  // then — until the caller hands back a new `size` — the size it was let go
  // at, so the card does not snap back for a frame between the release and
  // the caller's re-render.
  const resize = popover?.resize;
  const [dragBox, setDragBox] = useState<(PopoverBox & { height: number }) | null>(null);
  const [dragged, setDragged] = useState<{ width: number; height: number } | null>(null);
  const [gripHeld, setGripHeld] = useState<PopoverEdge | null>(null);
  const dragStart = useRef<{
    x: number;
    y: number;
    box: PopoverBox & { height: number };
    pointerId: number;
    edge: PopoverEdge;
    el: HTMLElement;
    floor: number | undefined;
    bounds: { left: number; top: number; width: number; height: number } | null;
  } | null>(null);
  const dragLast = useRef<(PopoverBox & { height: number }) | null>(null);
  useEffect(() => {
    setDragged(null);
  }, [sizeW, sizeH]);
  // POPOVER: where it stands (popover-place.ts), re-measured as the card's
  // height or the window changes. The first guess is taken at mount, off the
  // anchor alone (the card's height is not known yet): never hidden while it
  // is measured, because the card's field takes the caret in this very
  // commit and a `visibility: hidden` box cannot hold focus.
  const sized = sizeW !== undefined && sizeH !== undefined;
  // …or, between a drag's release and the caller handing the size back, the
  // size the drag let go at (`dragged`): the card re-places itself on it —
  // its right edge against `beside` again — rather than paint a new width
  // at the old left (Bugbot on #2189).
  const wantWidth = dragged?.width ?? sizeW ?? (typeof width === "number" ? width : 580);
  const wantHeight = dragged?.height ?? sizeH;
  const placeKey = popover?.placeKey;
  const [place, setPlace] = useState<PopoverBox | null>(() => {
    const anchor = popover?.anchor.current;
    const view = ownerDoc.defaultView;
    if (!anchor || !view || typeof anchor.getBoundingClientRect !== "function") return null;
    const want = { width: wantWidth, height: wantHeight ?? 0 };
    const own = sized ? popover?.place?.(want) : null;
    if (own) return own;
    const a = popover?.anchorBox?.() ?? anchor.getBoundingClientRect();
    const b = (popover?.beside?.current ?? anchor).getBoundingClientRect();
    return popoverPlacement(a, b, want, { width: view.innerWidth, height: view.innerHeight }, sized);
  });
  useLayoutEffect(() => {
    if (!popoverOn) return;
    const view = ownerDoc.defaultView;
    const measure = () => {
      const anchor = popover?.anchor.current;
      const dialog = dialogRef.current;
      if (!anchor || !dialog || !view) return;
      // A sized card asks for its size, not for what it now measures (its
      // own last placement).
      const want = { width: wantWidth, height: wantHeight ?? dialog.offsetHeight };
      const own = sized ? popover?.place?.(want) : null;
      const a = popover?.anchorBox?.() ?? anchor.getBoundingClientRect();
      const b = (popover?.beside?.current ?? anchor).getBoundingClientRect();
      const next = own ?? popoverPlacement(a, b, want, { width: view.innerWidth, height: view.innerHeight }, sized);
      setPlace((was) =>
        was && was.left === next.left && was.top === next.top && was.width === next.width && was.height === next.height
          ? was
          : next,
      );
    };
    measure();
    const RO = (view as (Window & { ResizeObserver?: typeof ResizeObserver }) | null)?.ResizeObserver;
    const ro = RO && dialogRef.current ? new RO(measure) : null;
    if (ro && dialogRef.current) ro.observe(dialogRef.current);
    const watched = popover?.watch?.();
    if (ro && watched) ro.observe(watched);
    view?.addEventListener("resize", measure);
    return () => {
      ro?.disconnect();
      view?.removeEventListener("resize", measure);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [popoverOn, wantWidth, wantHeight, placeKey]);

  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key !== "Tab") return;
    // A popover is not modal: Tab walks on into the page.
    if (popover) return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const nodes = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
      (el) => el.offsetParent !== null || el === ownerDoc.activeElement,
    );
    if (nodes.length === 0) {
      e.preventDefault();
      dialog.focus();
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = ownerDoc.activeElement;
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

  const onGripDown = (edge: PopoverEdge) => (e: ReactPointerEvent<HTMLDivElement>) => {
    const dialog = dialogRef.current;
    // Not on a card already leaving (CodeRabbit on #2189): a drag through
    // the exit fade would save a size nobody meant.
    if (e.button !== 0 || !dialog || !resize || closing) return;
    e.preventDefault();
    e.stopPropagation();
    const r = dialog.getBoundingClientRect();
    const box = { left: r.left, top: r.top, width: r.width, height: r.height };
    e.currentTarget.setPointerCapture(e.pointerId);
    dragStart.current = {
      x: e.clientX,
      y: e.clientY,
      box,
      pointerId: e.pointerId,
      edge,
      el: e.currentTarget,
      floor: resize.maxBottom?.(box),
      bounds: resize.bounds?.() ?? null,
    };
    dragLast.current = null;
    setDragBox(box);
    setGripHeld(edge);
  };
  const onGripMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    const view = ownerDoc.defaultView;
    if (!start || start.pointerId !== e.pointerId || !view || !resize) return;
    const next = popoverDrag(
      start.edge,
      start.box,
      e.clientX - start.x,
      e.clientY - start.y,
      { width: view.innerWidth, height: view.innerHeight },
      resize,
      start.floor,
      start.bounds ?? undefined,
    );
    dragLast.current = next;
    setDragBox((was) =>
      was && was.left === next.left && was.top === next.top && was.width === next.width && was.height === next.height
        ? was
        : next,
    );
  };
  const endDrag = () => {
    const start = dragStart.current;
    if (!start) return;
    dragStart.current = null;
    const last = dragLast.current;
    dragLast.current = null;
    if (start.el.hasPointerCapture?.(start.pointerId)) start.el.releasePointerCapture(start.pointerId);
    setGripHeld(null);
    setDragBox(null);
    if (!last || (last.width === start.box.width && last.height === start.box.height)) return;
    const size = { width: last.width, height: last.height };
    setDragged(size);
    resize?.onResize(size, { width: last.width !== start.box.width, height: last.height !== start.box.height });
  };
  const onGripUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start || start.pointerId !== e.pointerId) return;
    endDrag();
  };

  const dialogStyle: CSSProperties | undefined = popover
    ? {
        // Inline, so no stylesheet's `position`/`left` (schedule.css shifts a
        // centred card for its side panels) can move it off its anchor.
        position: "fixed",
        left: dragBox?.left ?? place?.left ?? 0,
        top: dragBox?.top ?? place?.top ?? 0,
        width: dragBox?.width ?? place?.width ?? wantWidth,
        maxHeight: "calc(100vh - 16px)",
        ...(dragBox ? { height: dragBox.height } : sized && place?.height !== undefined ? { height: place.height } : {}),
      }
    : width !== undefined
      ? { width }
      : undefined;

  // Portal to <body>: modals mount from arbitrary spots (e.g. AI Models'
  // NewJobModal inside a page's toolbar), and ancestor-scoped `button` rules
  // were leaking into the dialog chrome (a boxed ✕ from just one caller's styles).
  // THE ✕, drawn at either end of the head (`closeAtStart`); none while busy
  // (see the note in the head) or when the card says so (`hideClose`).
  const closeButton = hideClose ? null : (
    <>
    {!busy && (
            <button
              type="button"
              className={"modal-close deploy-close" + (confirmClose ? " is-armed" : "")}
              // The label carries the state for a screen reader, which has no
              // corner to look at. The footer hint is `role="status"` and is
              // announced too; this is what the button itself answers to when the
              // user tabs back to it.
              aria-label={confirmClose ? "Close and discard changes" : "Close"}
              title={confirmClose ? "Press again to discard" : (closeTitle ?? "Close")}
              onClick={attemptClose}
            >
              ✕
            </button>
    )}
    </>
  );

  return createPortal(
    <div
      className={(popover ? "modal-overlay modal-popover" : "modal-overlay deploy-overlay") + (closing ? " closing" : "")}
      // The card's left edge, for side panels that stand beside it (Browse,
      // Custom recurrence — schedule.css `.modal-popover`).
      {...(popover && place ? { style: { "--modal-pop-left": `${place.left}px` } as CSSProperties } : {})}
      onMouseDown={(e) => {
        if (popover) return;
        // Only the scrim itself, and not in its first moments: the second press
        // of a double-click on whatever opened this dialog lands here, on the
        // spot the pointer never left (dirty-guard `backdropPressCloses`).
        if (e.target !== e.currentTarget) return;
        if (!backdropPressCloses(openedAt.current, performance.now())) return;
        attemptClose();
      }}
      onKeyDown={onKeyDown}
    >
      <div
        ref={dialogRef}
        className={
          "modal-dialog deploy-dialog" +
          (popover && sized ? " is-sized" : "") +
          // Under a grip's drag (owner, 2026-11-08: "it moves from left and
          // right both sides"): schedule.css glides the dialog's `left` for
          // its side panels, and a glide under a drag let the right edge
          // wobble while the width snapped — `.is-resizing` turns it off.
          (gripHeld ? " is-resizing" : "") +
          (dialogClassName ? " " + dialogClassName : "")
        }
        style={dialogStyle}
        role="dialog"
        // BUSY, SAID ON THE CARD ITSELF (CodeRabbit on #2220): a caller that
        // must not close a card mid-save reads this, not the ✕ — which a card
        // may hide for its own reasons (`hideClose`).
        {...(busy ? { "data-busy": "" } : {})}
        {...(popover ? {} : { "aria-modal": "true" as const })}
        {...(ariaLabel
          ? // An explicit name WINS over `aria-labelledby`, so the two are
            // mutually exclusive rather than both set — leaving the heading
            // reference in place beside an `aria-label` would put a dead
            // attribute in the tree for the next reader to puzzle over.
            { "aria-label": ariaLabel }
          : { "aria-labelledby": titleId })}
        tabIndex={-1}
        onMouseDown={(e) => e.stopPropagation()}
        {...(popover
          ? {
              // The reader acting inside a preview pins it (header of
              // `popover.pinned`). React's own events, so a pill's menu
              // portaled out of the card still counts as inside it.
              onPointerDownCapture: () => {
                if (pinnedRef.current === false) popover.onPin?.();
              },
              onFocusCapture: () => {
                if (pinnedRef.current === false) popover.onPin?.();
              },
              onPointerEnter: () => popover.onPointerEnter?.(),
              onPointerLeave: () => popover.onPointerLeave?.(),
            }
          : {})}
      >
        {head ? (
          <div className="modal-head-slot">
            {head({
              titleId,
              close: attemptClose,
              busy,
              armed: confirmClose,
              parts: { title, titleLead, titleAside, headActions },
            })}
          </div>
        ) : (
        <div className={"modal-head deploy-head" + (closeAtStart ? " has-close-start" : "")}>
          {closeAtStart && closeButton}
          {titleAside || titleLead ? (
            // The heading and its caption as ONE flex child of the head, so the
            // head is still `[ title | ✕ ]` and `justify-content: space-between`
            // does not push the caption into the middle of the bar.
            <div className="modal-head-title">
              {titleLead}
              <h2 id={titleId}>{title}</h2>
              {titleAside}
            </div>
          ) : (
            <h2 id={titleId}>{title}</h2>
          )}
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
          {headActions && <div className="modal-head-actions">{headActions}</div>}
          {!closeAtStart && closeButton}
        </div>
        )}
        <div className={"modal-body " + (plainBody ? "modal-body-plain" : "deploy-body")}>{children}</div>
        {(footer || confirmClose) && (
          <div
            className="modal-footer"
            // ARROW KEYS WALK THE FOOTER (Akshil, 2026-09-21). Tab already does,
            // but a reader looking at two buttons side by side reaches for ←/→
            // first — and with focus parked on Cancel, ← → Enter is the
            // keyboard spelling of "Delete forever" that never mis-fires the
            // way an unringed Enter did. Only footer buttons take part; a key
            // typed into a footer input (the New task form has none, but the
            // chassis is shared) is left alone, and the walk wraps.
            onKeyDown={(e) => {
              if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
              const target = e.target as HTMLElement;
              if (target.tagName !== "BUTTON") return;
              const buttons = Array.from(
                e.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
              );
              const at = buttons.indexOf(target as HTMLButtonElement);
              if (at < 0 || buttons.length < 2) return;
              e.preventDefault();
              const step = e.key === "ArrowRight" ? 1 : -1;
              buttons[(at + step + buttons.length) % buttons.length].focus();
            }}
          >
            {confirmClose && (
              <span className="modal-dirty-hint" role="status">
                Unsaved changes — close again to discard
              </span>
            )}
            {footer}
          </div>
        )}
      </div>
      {popover && resize && sized && (
        // THE GRIPS (header of `popover.resize`): hit areas on the card's two
        // left corners and its left, top and bottom edges, in a frame laid
        // over the card's box — OUTSIDE the dialog, whose callers clip their
        // overflow (`.new-task-dialog`), so a grip half off the card's edge
        // is never cut. The frame takes no pointer; the grips do.
        // `aria-hidden`: a pointer-only affordance, the card's size is a
        // preference, not a control the keyboard needs.
        <div
          className="modal-popover-frame"
          aria-hidden="true"
          style={{
            left: dragBox?.left ?? place?.left ?? 0,
            top: dragBox?.top ?? place?.top ?? 0,
            width: dragBox?.width ?? place?.width ?? wantWidth,
            height: dragBox?.height ?? place?.height ?? 0,
          }}
        >
          {POPOVER_GRIPS.map((edge) => (
            <div
              key={edge}
              className={"modal-popover-grip is-" + edge + (gripHeld === edge ? " is-active" : "")}
              onPointerDown={onGripDown(edge)}
              onPointerMove={onGripMove}
              onPointerUp={onGripUp}
              onPointerCancel={onGripUp}
              onLostPointerCapture={() => dragStart.current && endDrag()}
            />
          ))}
        </div>
      )}
    </div>,
    container,
  );
}

export default Modal;
