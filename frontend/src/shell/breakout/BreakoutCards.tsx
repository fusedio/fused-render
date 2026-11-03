// THE BREAKOUT CARDS — one floating chat per open breakout (breakouts.ts), the
// shell's own overlay so a card outlives whichever page, pane or rail card it
// was opened from. Chrome follows the rail peek card (claude-rail.css
// `.claude-rail-peek-card`, `.breakout-card` modifier); body is a plain
// `ChatMount` in breakout posture: forked from the parent session on its first
// send, research-only on the server (permission bridge), no queue, no nesting.
import { useCallback, useEffect, useRef, useState, type PointerEvent } from "react";
import { createPortal } from "react-dom";
import { ChatMount } from "@apps/claude";
import type { Turn } from "@apps/claude/protocol/controller-api";
import { PEEK_MAX_HEIGHT, PEEK_WIDTH } from "@shell/claude-rail/rail-peek-lib";
import {
  buildHandoffDraft,
  closeBreakout,
  ownTurns,
  sendBackBreakout,
  useBreakouts,
  type BreakoutCard,
} from "@platform/lib/breakouts";

const OFFSET = 28;

export function Card({ card, index }: { card: BreakoutCard; index: number }) {
  const state = useRef<{ sessionId: string; turns: readonly Turn[] }>({ sessionId: "", turns: [] });
  const [canSend, setCanSend] = useState(false);
  const onState = useCallback((s: { sessionId: string; turns: readonly Turn[] }) => {
    state.current = s;
    // Only the breakout's own turns: the parent's restored history is not
    // something to send back to the parent.
    setCanSend(ownTurns(s.turns).some((t) => t.role === "assistant" && !!t.text.trim()));
  }, []);

  // Drag by the head row. `pos` null = the default bottom-right slot; once
  // dragged the card is placed by left/top. Moves are coalesced to one
  // `setPos` per animation frame (same as TerminalDrawer's drag resize).
  const cardRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const drag = useRef<{ sx: number; sy: number; ox: number; oy: number } | null>(null);
  const latest = useRef<{ x: number; y: number } | null>(null);
  const raf = useRef<number | null>(null);

  const clamp = (x: number, y: number) => {
    const r = cardRef.current?.getBoundingClientRect();
    const w = r?.width ?? 0;
    const h = r?.height ?? 0;
    return {
      x: Math.max(0, Math.min(x, window.innerWidth - w)),
      y: Math.max(0, Math.min(y, window.innerHeight - h)),
    };
  };
  const onHeadDown = (e: PointerEvent<HTMLDivElement>) => {
    if ((e.target as Element).closest?.("button")) return;
    const r = cardRef.current?.getBoundingClientRect();
    if (!r) return;
    drag.current = { sx: e.clientX, sy: e.clientY, ox: r.left, oy: r.top };
    latest.current = null;
    setDragging(true);
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const onHeadMove = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    latest.current = clamp(d.ox + e.clientX - d.sx, d.oy + e.clientY - d.sy);
    if (raf.current === null) {
      raf.current = requestAnimationFrame(() => {
        raf.current = null;
        if (latest.current) setPos(latest.current);
      });
    }
  };
  const onHeadUp = (e: PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return;
    drag.current = null;
    if (raf.current !== null) {
      cancelAnimationFrame(raf.current);
      raf.current = null;
    }
    if (latest.current) setPos(latest.current);
    setDragging(false);
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  };
  const hasPos = pos !== null;
  useEffect(() => {
    if (!hasPos) return;
    const onResize = () => setPos((p) => (p ? clamp(p.x, p.y) : p));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [hasPos]);

  const close = () => {
    // A live send's key is `u:<seq>` (controller-api UserTurn.key); restored
    // parent turns carry history uuids, so only the breakout's own count.
    const used = state.current.turns.some(
      (t) => t.role === "user" && t.key.startsWith("u:"),
    );
    if (used && !window.confirm("Discard this breakout? The chat is not sent back.")) return;
    closeBreakout(card.id);
  };
  const sendBack = () => {
    const text = buildHandoffDraft(ownTurns(state.current.turns));
    if (!text) return;
    sendBackBreakout(card.id, text);
  };

  return (
    <div
      ref={cardRef}
      className="breakout-card"
      role="dialog"
      aria-label="Breakout"
      style={{
        width: `min(${PEEK_WIDTH}px, calc(100vw - 16px))`,
        height: `min(${PEEK_MAX_HEIGHT}px, 70vh)`,
        ...(pos
          ? { left: pos.x, top: pos.y }
          : { right: 16 + index * OFFSET, bottom: 16 + index * OFFSET }),
      }}
    >
      <div className="claude-rail-peek-card">
        <div
          className={`claude-rail-peek-head breakout-head${dragging ? " is-dragging" : ""}`}
          onPointerDown={onHeadDown}
          onPointerMove={onHeadMove}
          onPointerUp={onHeadUp}
          onPointerCancel={onHeadUp}
        >
          <div className="claude-rail-peek-row">
            <span className="breakout-title">Breakout</span>
            <span className="breakout-note">research only</span>
            <span className="breakout-spacer" />
            <button
              type="button"
              className="breakout-btn"
              onClick={sendBack}
              disabled={!canSend}
              data-hint="Draft a summary into the chat's composer"
            >
              Send back
            </button>
            <button type="button" className="breakout-btn" onClick={close}>
              Close
            </button>
          </div>
        </div>
        <div className="claude-rail-peek-body">
          <div className="claude-rail-peek-chat is-shown">
          <ChatMount
            file={card.file}
            sessionId={card.parentSessionId}
            breakout
            {...(card.forkAt ? { breakoutAt: card.forkAt } : {})}
            onBreakoutState={onState}
            chatOnly
            peek
            noAnnTools
            paramsSource="memory"
          />
          </div>
        </div>
      </div>
    </div>
  );
}

export default function BreakoutCards() {
  const cards = useBreakouts();
  if (!cards.length) return null;
  return createPortal(
    <>
      {cards.map((c, i) => (
        <Card key={c.id} card={c} index={i} />
      ))}
    </>,
    document.body,
  );
}
