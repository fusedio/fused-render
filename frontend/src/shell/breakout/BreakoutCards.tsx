// THE BREAKOUT CARDS — one floating chat per open breakout (breakouts.ts), the
// shell's own overlay so a card outlives whichever page, pane or rail card it
// was opened from. Chrome follows the rail peek card (claude-rail.css
// `.claude-rail-peek-card`, `.breakout-card` modifier); body is a plain
// `ChatMount` in breakout posture: forked from the parent session on its first
// send, research-only on the server (permission bridge), no queue, no nesting.
import { Suspense, lazy, useCallback, useEffect, useRef, useState, type ComponentProps, type ComponentType, type PointerEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ChatMount } from "@apps/claude";
import type { Turn } from "@apps/claude/protocol/controller-api";
import type { ToolSegment } from "@apps/claude/protocol/types";
import { PEEK_MAX_HEIGHT, PEEK_WIDTH } from "@shell/claude-rail/rail-peek-lib";
import { closeAgentPeek, useAgentPeeks, useAgentSegment, type AgentPeek } from "@apps/claude/agent-peeks";
import {
  buildHandoffDraft,
  closeBreakout,
  ownTurns,
  sendBackBreakout,
  useBreakouts,
  type BreakoutCard,
} from "@platform/lib/breakouts";

const OFFSET = 28;

/** What a breakout chat reports up (`onBreakoutState`). */
type BreakoutState = { sessionId: string; turns: readonly Turn[]; forkClamped?: boolean; sends?: number; running?: boolean; stop?: () => void };

/** The shared floating-card chrome: the fixed box (draggable by the head row,
 *  clamped to the viewport, stacked by `index`), the head (title, note, spacer,
 *  `actions`, Close) and the body wrapper. Breakout and Agent peek both use it. */
export function FloatingCard({
  index,
  label,
  title,
  note,
  noteTitle,
  actions,
  error,
  onClose,
  children,
}: {
  index: number;
  label: string;
  title: string;
  note: string;
  noteTitle?: string;
  actions?: ReactNode;
  /** A one-line failure shown under the head. */
  error?: string;
  onClose: () => void;
  children: ReactNode;
}) {
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
  // The default slot steps up and left by `index`, but never so far that the
  // card's top or left edge leaves the screen (many cards, a short window).
  const cardH = Math.min(PEEK_MAX_HEIGHT, 0.7 * window.innerHeight);
  const cardW = Math.min(PEEK_WIDTH, window.innerWidth - 16);
  const slotY = Math.max(0, Math.min(index * OFFSET, window.innerHeight - 16 - cardH));
  const slotX = Math.max(0, Math.min(index * OFFSET, window.innerWidth - 16 - cardW));
  const hasPos = pos !== null;
  useEffect(() => {
    if (!hasPos) return;
    const onResize = () => setPos((p) => (p ? clamp(p.x, p.y) : p));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [hasPos]);

  return (
    <div
      ref={cardRef}
      className="breakout-card"
      role="dialog"
      aria-label={label}
      style={{
        width: `min(${PEEK_WIDTH}px, calc(100vw - 16px))`,
        height: `min(${PEEK_MAX_HEIGHT}px, 70vh)`,
        ...(pos
          ? { left: pos.x, top: pos.y }
          : { right: 16 + slotX, bottom: 16 + slotY }),
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
            <span className="breakout-title">{title}</span>
            <span className="breakout-note" {...(noteTitle ? { title: noteTitle } : {})}>
              {note}
            </span>
            <span className="breakout-spacer" />
            {actions}
            <button type="button" className="breakout-btn" onClick={onClose}>
              Close
            </button>
          </div>
        </div>
        {error ? (
          <div className="breakout-error claude-rail-peek-rename-error" role="alert">
            {error}
          </div>
        ) : null}
        <div className="claude-rail-peek-body">{children}</div>
      </div>
    </div>
  );
}

export function Card({
  card,
  index,
  mount: Mount = ChatMount,
}: {
  card: BreakoutCard;
  index: number;
  // Seam for tests to stub the chat without a process-wide mock.module.
  mount?: ComponentType<ComponentProps<typeof ChatMount>>;
}) {
  const state = useRef<BreakoutState>({ sessionId: "", turns: [] });
  const [canSend, setCanSend] = useState(false);
  const [clamped, setClamped] = useState(false);
  const [error, setError] = useState("");
  const onState = useCallback((s: BreakoutState) => {
    state.current = s;
    setClamped(!!s.forkClamped);
    // Only the breakout's own turns: the parent's restored history is not
    // something to send back to the parent.
    setCanSend(ownTurns(s.turns, s.sends ?? 0).some((t) => t.role === "assistant" && !!t.text.trim()));
  }, []);

  const close = () => {
    // `sends` counts this card's own sends; the parent's restored history is not a reason to confirm.
    const used = (state.current.sends ?? 0) > 0;
    if (used && !window.confirm("Discard this breakout? The chat is not sent back.")) return;
    // The card's own research run would otherwise outlive its only view.
    if (state.current.running) state.current.stop?.();
    closeBreakout(card.id);
  };
  const sendBack = () => {
    const text = buildHandoffDraft(ownTurns(state.current.turns, state.current.sends ?? 0));
    if (!text) return;
    setError("");
    void sendBackBreakout(card.id, text).then((ok) => {
      if (!ok) setError("Could not hand the summary back. The card stays open.");
    });
  };

  return (
    <FloatingCard
      index={index}
      label="Breakout"
      title="Breakout"
      note={clamped ? "research only · forked at the last compaction" : "research only"}
      {...(clamped
        ? {
            noteTitle:
              "The turn you chose is older than this chat's last compaction, so the breakout starts from the compaction summary instead.",
          }
        : {})}
      actions={
        <button
          type="button"
          className="breakout-btn"
          onClick={sendBack}
          disabled={!canSend}
          data-hint="Draft a summary into the chat's composer"
        >
          Send back
        </button>
      }
      error={error}
      onClose={close}
    >
      <div className="claude-rail-peek-chat is-shown">
        <Mount
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
    </FloatingCard>
  );
}

// Lazy, like ChatMount: the transcript renderer (markdown stack and all) must
// stay out of the shell's entry chunk.
const AgentPeekLog = lazy(() => import("@apps/claude/ui/AgentPeekLog"));
const peekLog = (seg: ToolSegment): ReactNode => (
  <Suspense fallback={null}>
    <AgentPeekLog seg={seg} />
  </Suspense>
);

/** A read-only chat of one subagent, live: the prompt it was given, then its
 *  own rows as the reply. */
export function AgentPeekCard({
  peek,
  index,
  renderLog = peekLog,
}: {
  peek: AgentPeek;
  index: number;
  // Seam for tests to see what would be drawn without the real renderer.
  renderLog?: (seg: ToolSegment) => ReactNode;
}) {
  const seg = useAgentSegment(peek.sessionId, peek.toolUseId);
  return (
    <FloatingCard
      index={index}
      label="Agent peek"
      title={peek.title}
      note={peek.subtitle}
      onClose={() => closeAgentPeek(peek.id)}
    >
      <div className="chat-root chat-peek agent-peek-body">
        {seg ? renderLog(seg) : <div className="agent-peek-empty">No transcript yet.</div>}
      </div>
    </FloatingCard>
  );
}

export default function BreakoutCards() {
  const cards = useBreakouts();
  const peeks = useAgentPeeks();
  if (!cards.length && !peeks.length) return null;
  return createPortal(
    <>
      {cards.map((c, i) => (
        <Card key={c.id} card={c} index={i} />
      ))}
      {peeks.map((p, i) => (
        <AgentPeekCard key={p.id} peek={p} index={cards.length + i} />
      ))}
    </>,
    document.body,
  );
}
