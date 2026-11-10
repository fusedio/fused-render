// QUICK LOOK, THE CARD (owner, 2026-11-10; design.md "Space quick look"): hold
// Space, rest the pointer on a list ring, and its chat floats over everything;
// let Space go and it is gone. The state is quick-look-store's, the key
// quick-look-keys'; this draws it.
//
// MOUNTED ONCE, IN THE SHELL (App.tsx, beside TasksPeek), for TasksPeek's
// reason: the rail is a page's and comes and goes with it. It installs the key
// layer while the rail flag is on (`canvas_rail_peek_enabled`, the flag every
// rail card rides).
//
// WHAT IT IS: the Modal chassis' popover form — no scrim, no focus taken —
// beside the rail, level with the ring, WINDOW A'S SIZE (`canvasRailPeek`),
// and the chat TasksPeek frames (`ChatMount`: `peek`, `noFocus`, `chatOnly`,
// `smallCard`, `paramsSource="memory"`), read and scrolled only: its composer
// is not drawn (claude-rail.css `.claude-rail-quick-look`). The head is the
// ring's dot, the chat's name and a hint; no ×, no grips — the key is the way
// out. ABOVE EVERY CARD (claude-rail.css, 49): window A's (45–46), window B's
// (47) and a breakout (48); it stands CLEAR of the ones standing where it can
// (`quickLookPlace`), over them where it cannot.
//
// IT TOUCHES NOTHING (design.md): no window A or B store is read or written;
// the cards it keeps clear of are read off the DOM, their boxes only. An
// outside press does not close it (Modal `keepOnOutsidePress`) — Space up,
// Escape (quick-look-keys), and a ring's press (ClaudeRail) do.
import { useCallback, useEffect, useRef } from "react";
import { ChatMount } from "@apps/claude";
import { hideHint } from "@platform/lib/hints";
import { navigateUrl } from "@platform/lib/router";
import { getUiLayout } from "@platform/lib/ui-layout";
import { Modal } from "@platform/ui/modal/Modal";
import { StatusIcon } from "@shell/ScheduleTaskViews";
import { BOT_PEEK_ID } from "./BotPeek";
import { FAV_PEEK_ID } from "./FavouritePeek";
import { installQuickLookKeys } from "./quick-look-keys";
import { dismissQuickLook, enterQuickLookCard, leaveQuickLookCard, useQuickLook } from "./quick-look-store";
import { useCanvasRailPeek } from "./rail-peek-flag";
import { clampAnchor, placeClear, railCardSize, rowOf, type Box, type PeekWish } from "./rail-peek-lib";

const QUICK_LOOK_CLASS = "claude-rail-quick-look";

const boxOf = (el: Element): Box => {
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height };
};

const overlaps = (a: Box, b: Box): boolean =>
  a.left < b.left + b.width && b.left < a.left + a.width && a.top < b.top + b.height && b.top < a.top + a.height;

/** WHERE QUICK LOOK STANDS: beside the rail, level with the ring, clear of
 *  window B's card (`b`) by window A's own rule (rail-peek-lib `placeClear`:
 *  above it, else beside it) — and then stepped left past any of window A's
 *  cards (`a`) it would still cover. No room left of one: over it, which its
 *  z-index allows (header). */
export function quickLookPlace(anchor: Box, rail: Box, row: Box, b: Box | null, a: readonly Box[], wish: PeekWish): Box | null {
  let edge = rail;
  let box = placeClear(anchor, edge, row, b, wish);
  for (let i = 0; i <= a.length && box; i++) {
    const at = box;
    const hit = a.find((c) => overlaps(at, c));
    if (!hit || hit.left >= edge.left) return box;
    edge = { ...edge, left: hit.left };
    const next = placeClear(anchor, edge, row, b, wish);
    if (!next) return box;
    box = next;
  }
  return box;
}

/** The rail cards standing now (`.is-on`), and the +'s popover: window B's
 *  apart, window A's the rest. Never quick look itself. */
function standingCards(): { b: Box | null; a: Box[] } {
  const bEl = document.querySelector(`#${FAV_PEEK_ID}.is-on, #${BOT_PEEK_ID}.is-on`);
  const a = Array.from(document.querySelectorAll(".claude-rail-peek.is-on, .modal-popover:not(.closing) > .modal-dialog"))
    .filter((el) => el !== bEl && !el.classList.contains(QUICK_LOOK_CLASS))
    .map(boxOf);
  return { b: bEl ? boxOf(bEl) : null, a };
}

export function QuickLook() {
  const on = useCanvasRailPeek();
  useEffect(() => (on ? installQuickLookKeys() : undefined), [on]);
  const look = useQuickLook();
  const up = on && look.held && look.key !== null && look.chat !== null;

  // The ring, and the rail it stands in, for the chassis (`popover.anchor`,
  // `beside`) — refs, read as it measures.
  const ringRef = useRef<HTMLElement | null>(null);
  ringRef.current = look.ring;
  const railRef = useRef<HTMLElement | null>(null);
  railRef.current = look.ring?.closest<HTMLElement>(".claude-rail") ?? null;
  const anchorRef = useRef(look.anchor);
  anchorRef.current = look.anchor;

  // The ring's own caption steps aside, as it does for every rail card: the
  // card is the caption (platform/lib/hints sits above every layer).
  useEffect(() => {
    if (up) hideHint();
  }, [up, look.key]);

  // THE RING GOING (review, 2026-11-10): a pulse dropping it, the rail
  // re-keying, a page hop — no pointerleave comes, and a card left standing
  // on a detached anchor never closes. While up, any removal in the
  // document is checked against the ring.
  const ring = up ? look.ring : null;
  useEffect(() => {
    if (!ring) return;
    if (!ring.isConnected) {
      dismissQuickLook();
      return;
    }
    const mo = new MutationObserver(() => {
      if (!ring.isConnected) dismissQuickLook();
    });
    mo.observe(document.body, { childList: true, subtree: true });
    return () => mo.disconnect();
  }, [ring]);

  const size = railCardSize(getUiLayout().canvasRailPeek ?? null);
  const place = useCallback(
    (want: { width: number; height: number }) => {
      const rail = railRef.current;
      const row = rail && rowOf(rail);
      const ring = ringRef.current;
      const at = ring?.isConnected ? boxOf(ring) : anchorRef.current;
      if (!rail || !row || !at) return null;
      const scroller = ring?.closest(".claude-rail-chats");
      const anchor = clampAnchor(at, scroller ? boxOf(scroller) : null);
      const { a, b } = standingCards();
      return quickLookPlace(anchor, boxOf(rail), boxOf(row), b, a, { width: want.width, maxHeight: want.height });
    },
    [],
  );

  if (!up || !look.chat) return null;
  const chat = look.chat;
  return (
    <Modal
      title={chat.title}
      ariaLabel={`${chat.title} — quick look`}
      titleLead={
        <span className="claude-rail-quick-look-dot" aria-hidden="true">
          <StatusIcon status={chat.status} label="" />
        </span>
      }
      headActions={
        // Said to a screen reader as what to do, not as a glyph.
        <span className="claude-rail-quick-look-hint" role="note" aria-label="Release Space to close">
          Space <span aria-hidden="true">↑</span> to close
        </span>
      }
      hideClose
      plainBody
      dialogClassName={QUICK_LOOK_CLASS}
      onClose={dismissQuickLook}
      popover={{
        anchor: ringRef,
        beside: railRef,
        size,
        place,
        // A swap re-places it level with the new ring.
        placeKey: look.key,
        watch: () => rowOf(railRef.current),
        pinned: false,
        keepOnOutsidePress: true,
        onPointerEnter: enterQuickLookCard,
        onPointerLeave: leaveQuickLookCard,
      }}
    >
      <div className="claude-rail-quick-look-chat">
        {chat.sessionId ? (
          <ChatMount
            // One mount per chat: a swap is a new chat, not a new card.
            key={look.key}
            file={chat.file}
            sessionId={chat.sessionId}
            {...(chat.model ? { model: chat.model } : {})}
            {...(chat.effort ? { effort: chat.effort } : {})}
            {...(chat.backend ? { backend: chat.backend } : {})}
            chatOnly
            peek
            noFocus
            noAnnTools
            paramsSource="memory"
            onNavigate={(href) => void navigateUrl(href)}
            smallCard
            smallCardShown
          />
        ) : (
          <p className="claude-rail-peek-note">This task has no conversation yet.</p>
        )}
      </div>
    </Modal>
  );
}
