// THE BREAKOUT CARDS — one floating chat per open breakout (breakouts.ts), the
// shell's own overlay so a card outlives whichever page, pane or rail card it
// was opened from. Chrome follows the rail peek card (claude-rail.css
// `.claude-rail-peek-card`, `.breakout-card` modifier); body is a plain
// `ChatMount` in breakout posture: forked from the parent session on its first
// send, research-only on the server (permission bridge), no queue, no nesting.
import { useCallback, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ChatMount } from "@apps/claude";
import type { Turn } from "@apps/claude/protocol/controller-api";
import { PEEK_MAX_HEIGHT, PEEK_WIDTH } from "@shell/claude-rail/rail-peek-lib";
import {
  buildHandoffDraft,
  closeBreakout,
  sendBackBreakout,
  useBreakouts,
  type BreakoutCard,
} from "@platform/lib/breakouts";

const OFFSET = 28;

function Card({ card, index }: { card: BreakoutCard; index: number }) {
  const state = useRef<{ sessionId: string; turns: readonly Turn[] }>({ sessionId: "", turns: [] });
  const [canSend, setCanSend] = useState(false);
  const onState = useCallback((s: { sessionId: string; turns: readonly Turn[] }) => {
    state.current = s;
    setCanSend(s.turns.some((t) => t.role === "assistant" && !!t.text.trim()));
  }, []);

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
    const text = buildHandoffDraft(state.current.turns);
    if (!text) return;
    sendBackBreakout(card.id, text);
  };

  return (
    <div
      className="breakout-card"
      role="dialog"
      aria-label="Breakout"
      style={{
        width: `min(${PEEK_WIDTH}px, calc(100vw - 16px))`,
        height: `min(${PEEK_MAX_HEIGHT}px, 70vh)`,
        right: 16 + index * OFFSET,
        bottom: 16 + index * OFFSET,
      }}
    >
      <div className="claude-rail-peek-card">
        <div className="claude-rail-peek-head">
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
