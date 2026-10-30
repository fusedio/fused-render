// THE CARD THAT OPENS OFF THE CLAUDE RAIL (owner, 2026-10-30: "when it is
// collapsed can we show a card preview of it when I hover over the chat
// items; I should be able to interact with it as well — scroll and add
// something, click something"). Behind `canvas_rail_peek_enabled`
// (rail-peek-flag.ts); the rings' side of the wiring is ClaudeRail.tsx, the
// timings and the geometry are rail-peek-lib.ts.
//
// WHAT IT IS: the chat itself. Not a caption, not a picture — the same
// `ChatMount` the Tasks page's side peek frames (`peek`, `chatOnly`), on the
// hovered chat's session, in a 420×640 card a gap off the rail. The reader
// scrolls the transcript, answers a question, types a message, follows a
// link, as in the pane; "Open in panel →" in the card's head is the door the
// ring's own press opens (`taskHref`), for when a preview turns into a
// session.
//
// WHY IT NEVER FLASHES:
//   - ONE CARD, kept in the DOM, shown and hidden by a class: the open is an
//     opacity fade and a 6px slide (var(--dur-med)); the close the reverse.
//     Moving from ring to ring moves the card and swaps what is in it; the
//     card itself never closes and reopens.
//   - PLACED, THEN SHOWN, on two frames: the box is set while the card is
//     hidden (no transition runs on a hidden card's position), and the
//     `is-on` class lands on the next frame — so a card that closed at the
//     bottom of the rail and opens at the top does not fly between the two.
//     With the card up, a position change DOES glide, which is the swap.
//   - `position: fixed` in a body portal, with no transform on the box: a
//     transformed ancestor would re-root every fixed-position popover inside
//     the chat (the model menu, the attach picker). The slide is on an inner
//     wrapper and ends at `transform: none` once the card is up, which is no
//     containing block — the 6px offset exists only while hidden or fading,
//     when the card is inert and nothing in it can open a popover.
//
// WHY IT IS FAST: every previewed chat stays mounted after its card closes
// (`kept`, newest first, PEEK_KEEP of them) — `visibility: hidden`, not
// `display: none`, so its scroll position and layout survive — and the
// hovered one's mount starts on the pointer's arrival, before the open wait
// has run (ClaudeRail `warm`). The chat's chunk is fetched when the rail
// mounts with the flag on (`preloadChat`).
//
// ACCESSIBILITY: the card is a non-modal `dialog` named for its chat; the ring
// that opened it says `aria-expanded` and `aria-controls` (ClaudeRail). The
// keyboard's way in is ArrowLeft on a focused ring (`focusChat` lands in the
// composer, or on the card's first button while the chat is still loading);
// Escape anywhere in the card hands focus back to the ring and closes. A card
// that holds focus never closes under the pointer (PeekIntent `hold`).
import {
  forwardRef,
  useImperativeHandle,
  useLayoutEffect,
  useEffect,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type MutableRefObject,
} from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import type { Task } from "@platform/lib/api";
import { shortTaskId } from "@platform/lib/task-id";
import { ChatMount, draftHref } from "@apps/claude";
import { Kebab } from "@apps/claude/ui/Kebab";
import { cardTitleLine, draftTag, isDraftTask, nextRunChip, taskColumn, taskHref } from "@shell/tasks-lib";
import { railScope, type RailRow } from "./claude-rail-lib";
import {
  PEEK_MAX_HEIGHT,
  PEEK_PLAIN_HEIGHT,
  PEEK_WIDTH,
  peekPlacement,
  statusWords,
  type Box,
} from "./rail-peek-lib";

/** What one seat is, for the head and the body: the pulse row, the full row
 *  once held, and the answers both halves ask (RailPeek `seatFacts`). */
interface SeatFacts {
  t: RailRow["pulse"];
  row: Task | null;
  /** The pulse's live status over the full row (or the pulse alone). */
  merged: Task | RailRow["pulse"];
  label: string;
  /** Where its press goes: a draft's New task card, else the chat's door. */
  href: string | null;
  draft: boolean;
  /** `#T12` — "" on a draft, which has no task number yet. */
  id: string;
  scope: ReturnType<typeof railScope>;
  words: string;
}

function seatFacts(seat: RailRow, dir: string | null): SeatFacts {
  const { pulse: t, row } = seat;
  const merged = row ? { ...row, status: t.status } : t;
  const line = cardTitleLine(merged);
  const draft = !!row && isDraftTask(row);
  const href = (row && draft ? draftHref(row) : null) ?? taskHref(merged);
  return {
    t,
    row,
    merged,
    label: line.text || t.title || "Chat",
    href,
    draft,
    id: draft ? "" : shortTaskId(t.task_id),
    scope: railScope(t, dir),
    words: draft ? "draft" : statusWords(t, !!row?.failed),
  };
}

export interface RailPeekHandle {
  /** Put the keyboard in the shown chat's composer (or on the card's first
   *  button while the chat is still loading). False when nothing is shown. */
  focusChat(): boolean;
}

export interface RailPeekProps {
  /** The card's DOM id — what the ring's `aria-controls` names. */
  id: string;
  /** The seat the card shows, or null while it is closed (the last shown
   *  seat stays drawn through the fade-out). */
  shown: RailRow | null;
  /** Every seat whose chat stays mounted: the shown one and the recently
   *  previewed ones (rail-peek-lib `keepAlive`). */
  kept: readonly RailRow[];
  /** The ring element for a seat (its anchor), or null if it is not drawn. */
  anchorOf: (key: string) => HTMLElement | null;
  /** The rail, for its edge and for the row it stands in (its parent). */
  railEl: HTMLElement | null;
  /** The clone dir: what a legacy row's target is read against to name its
   *  node (claude-rail-lib `railScope`). */
  dir: string | null;
  /** The ⋮ filed or deleted a task: re-read the listing now rather than on
   *  the pulse's next move. */
  onReload: () => void;
  onPointerEnter: () => void;
  onPointerLeave: () => void;
  /** A pointer went down inside the card — on the card itself or on one of
   *  the chat's portaled popovers (React's capture phase crosses portals,
   *  the DOM's `contains` does not): hold it through the gesture, and tell
   *  the host's outside-press watcher this press was ours. */
  onPressInside: () => void;
  /** The card took (`true`) or lost (`false`) the keyboard focus. */
  onHold: (on: boolean) => void;
  /** Escape inside the card, or its close button. */
  onDismiss: () => void;
  /** The row left no room for a card (rail-peek-lib `peekPlacement` null —
   *  a window under ~140px tall or ~170px wide): the host drops the show so
   *  the ring is not left saying `aria-expanded` for a card that never drew. */
  onNoRoom: () => void;
  /** "Open in panel →", and any hop the chat itself makes. */
  onOpenChat: (href: string) => void;
}

export const RailPeek = forwardRef<RailPeekHandle, RailPeekProps>(function RailPeek(
  {
    id,
    shown,
    kept,
    anchorOf,
    railEl,
    dir,
    onReload,
    onPointerEnter,
    onPointerLeave,
    onPressInside,
    onHold,
    onDismiss,
    onNoRoom,
    onOpenChat,
  },
  ref,
) {
  const cardRef = useRef<HTMLDivElement | null>(null);
  // The last seat shown, so the fade-out has something to fade — the head
  // AND the chat: both key on `view`, so the transcript stays visible through
  // the fade rather than vanishing under a fading empty frame (Bugbot).
  const last = useRef<RailRow | null>(null);
  if (shown) last.current = shown;
  const view = shown ?? last.current;
  const viewKey = view?.pulse.key ?? null;

  const [box, setBox] = useState<Box | null>(null);
  const [on, setOn] = useState(false);
  const shownKey = shown?.pulse.key ?? null;

  // One composer ref per kept mount (ChatMount `focusRef`).
  const focusRefs = useRef(new Map<string, MutableRefObject<HTMLTextAreaElement | null>>());
  const focusRefFor = (key: string) => {
    let r = focusRefs.current.get(key);
    if (!r) {
      r = { current: null };
      focusRefs.current.set(key, r);
    }
    return r;
  };
  for (const key of [...focusRefs.current.keys()]) {
    if (!kept.some((s) => s.pulse.key === key)) focusRefs.current.delete(key);
  }

  // A seat with no session gets the short card (PEEK_PLAIN_HEIGHT).
  const maxHeightFor = (key: string) =>
    kept.find((s) => s.pulse.key === key)?.pulse.session_id ? PEEK_MAX_HEIGHT : PEEK_PLAIN_HEIGHT;
  const place = (key: string): Box | null => {
    const anchor = anchorOf(key);
    const row = railEl?.parentElement;
    if (!anchor || !railEl || !row) return null;
    return peekPlacement(
      anchor.getBoundingClientRect(),
      railEl.getBoundingClientRect(),
      row.getBoundingClientRect(),
      maxHeightFor(key),
    );
  };

  // PLACED, THEN SHOWN (header). Hidden → shown sets the box on this frame and
  // the class on the next; shown → shown (a swap) moves the box under the
  // class, which glides; shown → hidden drops the class and keeps the box.
  useLayoutEffect(() => {
    if (!shownKey) {
      setOn(false);
      return;
    }
    const next = place(shownKey);
    if (!next) {
      onNoRoom();
      return;
    }
    setBox(next);
    const id = requestAnimationFrame(() => setOn(true));
    return () => cancelAnimationFrame(id);
    // `place` reads the DOM; the key is what decides when to re-read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey]);

  // A WARM MOUNT NEEDS A REAL BOX: the first chat mounts before anything is
  // shown, and a chat laid out at 0×0 would do its first paint and its
  // scroll-to-end in a box it is about to leave. Place the hidden card for
  // the warmed ring, so the mount lays out at the size it will be shown at.
  const warmKey = kept[0]?.pulse.key ?? null;
  useLayoutEffect(() => {
    if (box || !warmKey) return;
    setBox(place(warmKey) ?? { left: -PEEK_WIDTH - 50, top: 0, width: PEEK_WIDTH, height: PEEK_MAX_HEIGHT });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [box, warmKey]);

  // FOLLOW THE RING while up: a window resize, the rail's own list scrolling
  // under the pointer on a short window, and the row changing shape (the
  // sidebar folding, a pane opening); plus every render, cheaply — the pulse
  // re-sorts the rail, and a ring that moved takes its card with it.
  useEffect(() => {
    if (!shownKey || !railEl) return;
    const re = () => {
      const next = place(shownKey);
      // The row shrank under the card (a window resized down to nothing):
      // the card leaves rather than standing clamped over the canvas.
      if (!next) {
        onNoRoom();
        return;
      }
      setBox((prev) =>
        next && prev && next.left === prev.left && next.top === prev.top && next.width === prev.width && next.height === prev.height
          ? prev
          : next,
      );
    };
    window.addEventListener("resize", re);
    const list = railEl.querySelector(".canvas-rail-chats");
    list?.addEventListener("scroll", re, { passive: true });
    const row = railEl.parentElement;
    const ro = row && typeof ResizeObserver !== "undefined" ? new ResizeObserver(re) : null;
    if (row && ro) ro.observe(row);
    return () => {
      window.removeEventListener("resize", re);
      list?.removeEventListener("scroll", re);
      ro?.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey, railEl]);
  useEffect(() => {
    if (!shownKey) return;
    const next = place(shownKey);
    if (next && box && (next.top !== box.top || next.left !== box.left || next.height !== box.height)) setBox(next);
  });

  // INERT WHILE HIDDEN (React 18 has no `inert` prop): the fading card and
  // the warm mounts behind it take no focus, no hit, and are out of the
  // accessibility tree — `aria-hidden` on a subtree that might still hold
  // focus is the thing this avoids.
  const visible = on && box !== null && shownKey !== null;
  useLayoutEffect(() => {
    cardRef.current?.toggleAttribute("inert", !visible);
  }, [visible]);

  useImperativeHandle(
    ref,
    () => ({
      focusChat() {
        if (!shownKey) return false;
        const box = focusRefs.current.get(shownKey)?.current;
        const target = box ?? cardRef.current?.querySelector<HTMLElement>("button");
        if (!target) return false;
        target.focus({ preventScroll: true });
        return true;
      },
    }),
    [shownKey],
  );

  // WHICH FOCUS HOLDS THE CARD. Typing (an editable), a popup of the chat's
  // (its ⋮ menu, a picker, a dialog), or focus that the KEYBOARD brought here
  // (Tab, an arrow — the ArrowLeft way in) — those are a reader using the
  // card, and it must not close under them. A plain button focused by a
  // click, or by a menu handing focus back to its trigger on Escape, is not:
  // a reader who closed the ⋮ and moved the pointer away expects the card to
  // follow the pointer rule again, not to stand until a second Escape.
  const lastKeyAt = useRef(0);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Tab" || e.key.startsWith("Arrow")) lastKeyAt.current = Date.now();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, []);
  const onFocusCapture = (e: ReactFocusEvent<HTMLDivElement>) => {
    const t = e.target as HTMLElement;
    const editable =
      t.matches("textarea, input, select, [contenteditable=''], [contenteditable='true']");
    // NOT the card itself, which is a `dialog` too: the nearest popup role
    // above the target has to be something INSIDE the card.
    const pop = t.closest("[role='menu'], [role='listbox'], [role='dialog'], [role='alertdialog']");
    const popup = !!pop && pop !== cardRef.current;
    const keyboard = Date.now() - lastKeyAt.current < 400;
    onHold(editable || popup || keyboard);
  };
  const onBlurCapture = (e: ReactFocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget as Node | null;
    if (next && e.currentTarget.contains(next)) return;
    onHold(false);
  };
  // Escape ON THE HEAD only. The chat hands its own Escape up through
  // `onEscape` once nothing of its own (a menu, the shot viewer) is open; a
  // card-wide handler would close the card under an open menu and, having
  // prevented default, stop the chat's handler from closing the menu itself.
  const onHeadKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onDismiss();
    }
  };

  const head = view ? seatFacts(view, dir) : null;

  // THE ⋮ IS THE CHAT'S OWN (owner, 2026-10-30: "it is not the same kebab
  // menu — fix that"): `@apps/claude/ui/Kebab`, the menu the pane's chat
  // wears — continue in terminal, copy the command, download the transcript,
  // archive, delete — with its own labels, refusals and erase dialog. Its
  // popup is a portal; the card HOLDS through it because the popup takes the
  // keyboard focus and React's focus events cross portals (`onFocusCapture`
  // → `onHold(true)`), and a press on one of its rows is "inside" by the
  // same route (`onPressInside`). Filing or erasing the task re-reads the
  // listing; the pulse then takes the ring, and the card with it (prune).
  const onFiled = () => onReload();

  return createPortal(
    <div
      ref={cardRef}
      id={id}
      className={"canvas-rail-peek" + (visible ? " is-on" : "")}
      role="dialog"
      aria-label={head ? `${head.label} — preview` : "Chat preview"}
      style={
        box
          ? { left: box.left, top: box.top, width: box.width, height: box.height }
          : undefined
      }
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
      onPointerDownCapture={onPressInside}
      onFocusCapture={onFocusCapture}
      onBlurCapture={onBlurCapture}
    >
      <div className="canvas-rail-peek-card">
        {head && (
          // THE HEAD (owner, 2026-10-30): the task's number where the Tasks
          // page prints it, its title, WHERE it lives (the canvas, or one
          // node — the pane's chats do not say, and a reader could not tell a
          // node's chat from the canvas's), then the door as a word, the ⋮,
          // and the close.
          <div className="canvas-rail-peek-head" onKeyDown={onHeadKeyDown}>
            {head.id && (
              <span className="canvas-rail-peek-id" title={head.words}>
                {head.id}
              </span>
            )}
            <span className="canvas-rail-peek-title" title={head.label}>
              {head.label}
            </span>
            <span
              className={"canvas-rail-peek-scope" + (head.scope.kind === "node" ? " is-node" : "")}
              title={
                head.scope.kind === "node"
                  ? `On the node ${head.scope.name} (${head.scope.file})`
                  : "On the canvas as a whole"
              }
            >
              {head.scope.kind === "node" ? head.scope.name : "Canvas"}
            </span>
            {head.href && (
              <button
                type="button"
                className="btn canvas-rail-peek-open"
                title={head.draft ? "Open the draft" : "Open this chat in the Claude panel"}
                onClick={() => onOpenChat(head.href!)}
              >
                Open
              </button>
            )}
            {head.t.session_id && (
              <span className="canvas-rail-peek-kebab">
                <Kebab
                  // Keyed per chat: the menu caches its labels per session.
                  key={head.t.key}
                  file={head.row?.target || head.t.target || head.t.project}
                  sessionId={head.t.session_id}
                  running={taskColumn(head.t) === "in_progress" || taskColumn(head.t) === "queued"}
                  asking={taskColumn(head.t) === "needs_attention"}
                  queued={taskColumn(head.t) === "queued"}
                  onErased={onFiled}
                  onArchived={onFiled}
                />
              </span>
            )}
            <button
              type="button"
              className="canvas-rail-peek-btn"
              title="Close the preview (Esc)"
              aria-label="Close the preview"
              onClick={onDismiss}
            >
              <X size={14} aria-hidden="true" />
            </button>
          </div>
        )}
        <div className="canvas-rail-peek-body">
          {kept.map((seat) => {
            const { pulse: t, row } = seat;
            const facts = seatFacts(seat, dir);
            return (
              <div
                key={t.key}
                className={"canvas-rail-peek-chat" + (t.key === viewKey ? " is-shown" : "")}
              >
                {t.session_id ? (
                  <ChatMount
                    file={row?.target || t.target || t.project}
                    sessionId={t.session_id}
                    // The task's own settings, a seed (TaskPeek passes the same).
                    {...(row?.model ? { model: row.model } : {})}
                    {...(row?.effort ? { effort: row.effort } : {})}
                    {...(row?.backend ?? t.backend ? { backend: (row?.backend ?? t.backend)! } : {})}
                    chatOnly
                    peek
                    // A hover must never take the keyboard: the reader is pointing,
                    // not typing, until they click into the card or ArrowLeft in.
                    noFocus
                    // No annotate seats: the card has no canvas of its own to mark.
                    noAnnTools
                    paramsSource="memory"
                    focusRef={focusRefFor(t.key)}
                    onEscape={onDismiss}
                    // A hop out of the chat (a Recent row, a link) is the ring's
                    // own navigation: the canvas page's hop reader does the rest.
                    onNavigate={onOpenChat}
                  />
                ) : (
                  // NO SESSION YET — a draft, or a scheduled task that has not
                  // run: there is no conversation to show, so the card says
                  // what the row is and where its press goes.
                  <div className="canvas-rail-peek-plain">
                    <p className="canvas-rail-peek-plain-title">
                      {facts.draft ? "Unsent draft" : "Not started yet"}
                    </p>
                    <p className="canvas-rail-peek-plain-words">
                      {facts.draft
                        ? (row && draftTag(row)?.title) || "Nothing written yet."
                        : (row && nextRunChip(row)?.title) || "Scheduled, no run yet."}
                    </p>
                    {facts.href && (
                      <button
                        type="button"
                        className="canvas-rail-peek-plain-open"
                        onClick={() => onOpenChat(facts.href!)}
                      >
                        {facts.draft ? "Open the draft" : "Open"}
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>,
    document.body,
  );
});
