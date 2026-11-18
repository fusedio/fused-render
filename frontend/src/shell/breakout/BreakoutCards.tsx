// THE BREAKOUT CARDS — one floating chat per open breakout (breakouts.ts), the
// shell's own overlay so a card outlives whichever page, pane or rail card it
// was opened from. Chrome follows the rail peek card (claude-rail.css
// `.claude-rail-peek-card`, `.breakout-card` modifier); body is a plain
// `ChatMount` in breakout posture: a fresh session that opens with a chip for
// the parent messages it was seeded with (they ride on its first send),
// research-only on the server (permission bridge), no queue, no nesting.
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
  consumeBreakoutPrompt,
  sendBackBreakout,
  useBreakouts,
  type BreakoutCard,
} from "@platform/lib/breakouts";
import { useSpecialistBreakouts } from "@platform/lib/specialist-breakouts";
import { SpecialistBreakoutCard } from "./SpecialistBreakoutCard";

const OFFSET = 28;

/** What a breakout chat reports up (`onBreakoutState`). */
type BreakoutState = {
  sessionId: string;
  turns: readonly Turn[];
  running?: boolean;
  stop?: () => void;
  send?: (text: string) => void;
};

/** What Send back asks the breakout to write before anything reaches the main chat. */
export const HANDOFF_PROMPT =
  "Write a handoff note for the main conversation: what we found that matters to it, any decision or recommendation, and open questions. Be concise and self-contained, since the main chat has not seen this breakout. Reply with the note only.";

/** The shared floating-card chrome: the fixed box (draggable by the head row,
 *  clamped to the viewport, stacked by `index`), the head (title, note, spacer,
 *  `actions`, Close) and the body wrapper. Breakout and Agent peek both use it. */
export function FloatingCard({
  index,
  label,
  title,
  note,
  noteTitle,
  noteHint,
  actions,
  error,
  onClose,
  children,
  head,
  className,
  initialBox,
  resizable,
  closeDisabled,
}: {
  index: number;
  label: string;
  title: string;
  note: string;
  noteTitle?: string;
  /** The note's full text and facts, on the app's instant tooltip (`data-hint`). */
  noteHint?: string;
  actions?: ReactNode;
  /** A one-line failure shown under the head. */
  error?: string;
  onClose: () => void;
  children: ReactNode;
  /** SPECIALISTS: the head's identity (face, name, a second line) in place of
   *  the title and note spans. */
  head?: ReactNode;
  /** SPECIALISTS: a modifier class beside `breakout-card`. */
  className?: string;
  /** SPECIALISTS: where the card opens and how big (viewport px), in place of
   *  the bottom-right slot. Kept inside the window. */
  initialBox?: { x: number; y: number; w: number; h: number };
  /** SPECIALISTS: a corner grip resizes the card. */
  resizable?: boolean;
  /** SPECIALISTS: Close is busy (the summary is being written). */
  closeDisabled?: boolean;
}) {
  // Drag by the head row. `pos` null = the default bottom-right slot; once
  // dragged the card is placed by left/top. Moves are coalesced to one
  // `setPos` per animation frame (same as TerminalDrawer's drag resize).
  const cardRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(() =>
    initialBox ? clampBox(initialBox.x, initialBox.y, initialBox.w, initialBox.h) : null,
  );
  const [size, setSize] = useState<{ w: number; h: number } | null>(() =>
    initialBox ? { w: initialBox.w, h: initialBox.h } : null,
  );
  const resize = useRef<{ sx: number; sy: number; w: number; h: number } | null>(null);
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

  // RESIZE by the corner grip (specialist cards): the card grows right and
  // down from where it sits, never past the window.
  const onGripDown = (e: PointerEvent<HTMLDivElement>) => {
    const r = cardRef.current?.getBoundingClientRect();
    if (!r) return;
    e.stopPropagation();
    resize.current = { sx: e.clientX, sy: e.clientY, w: r.width, h: r.height };
    if (!pos) setPos({ x: r.left, y: r.top });
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const onGripMove = (e: PointerEvent<HTMLDivElement>) => {
    const d = resize.current;
    if (!d) return;
    const r = cardRef.current?.getBoundingClientRect();
    const left = r?.left ?? 0;
    const top = r?.top ?? 0;
    setSize({
      w: Math.max(MIN_W, Math.min(d.w + e.clientX - d.sx, window.innerWidth - left)),
      h: Math.max(MIN_H, Math.min(d.h + e.clientY - d.sy, window.innerHeight - top)),
    });
  };
  const onGripUp = (e: PointerEvent<HTMLDivElement>) => {
    if (!resize.current) return;
    resize.current = null;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  };

  return (
    <div
      ref={cardRef}
      className={className ? `breakout-card ${className}` : "breakout-card"}
      role="dialog"
      aria-label={label}
      style={{
        width: size ? `min(${Math.round(size.w)}px, 100vw)` : `min(${PEEK_WIDTH}px, calc(100vw - 16px))`,
        height: size ? `min(${Math.round(size.h)}px, 100vh)` : `min(${PEEK_MAX_HEIGHT}px, 70vh)`,
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
            {head ?? (
              <>
                <span className="breakout-title">{title}</span>
                <span
                  className="breakout-note"
                  {...(noteTitle ? { title: noteTitle } : {})}
                  {...(noteHint ? { "data-hint": noteHint } : {})}
                >
                  {note}
                </span>
              </>
            )}
            <span className="breakout-spacer" />
            {actions}
            <button type="button" className="breakout-btn" onClick={onClose} {...(closeDisabled ? { disabled: true } : {})}>
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
      {resizable ? (
        <div
          className="breakout-resize"
          role="separator"
          aria-label="Resize"
          onPointerDown={onGripDown}
          onPointerMove={onGripMove}
          onPointerUp={onGripUp}
          onPointerCancel={onGripUp}
        />
      ) : null}
    </div>
  );
}

const MIN_W = 300;
const MIN_H = 240;

/** A box's top-left kept inside the window (its size as given). */
export function clampBox(x: number, y: number, w: number, h: number): { x: number; y: number } {
  const W = typeof window !== "undefined" ? window.innerWidth : 1280;
  const H = typeof window !== "undefined" ? window.innerHeight : 800;
  return { x: Math.max(0, Math.min(x, W - w)), y: Math.max(0, Math.min(y, H - h)) };
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
  const [running, setRunning] = useState(false);
  const [writing, setWriting] = useState(false);
  const [error, setError] = useState("");
  /** The handoff run in flight: where its turns start, and whether the chat has
   *  been seen running yet (idle alone means the send has not landed). */
  const pend = useRef<{ from: number; saw: boolean } | null>(null);

  const finish = useCallback(
    (turns: readonly Turn[], from: number) => {
      const slice = turns.slice(from);
      const reply = [...slice].reverse().find((t) => t.role === "assistant" && !!t.text.trim());
      setWriting(false);
      if (!reply) {
        setError("Couldn't write a handoff. Try again");
        return;
      }
      void sendBackBreakout(card.id, buildHandoffDraft(slice)).then((ok) => {
        if (!ok) setError("Could not hand the summary back. The card stays open.");
      });
    },
    [card.id],
  );
  const onState = useCallback(
    (s: BreakoutState) => {
      state.current = s;
      setCanSend(s.turns.some((t) => t.role === "assistant" && !!t.text.trim()));
      setRunning(!!s.running);
      const p = pend.current;
      if (!p) return;
      if (s.running) p.saw = true;
      else if (p.saw) {
        pend.current = null;
        finish(s.turns, p.from);
      }
    },
    [finish],
  );

  const close = () => {
    const used = state.current.turns.some((t) => t.role === "user");
    if (used && !window.confirm("Discard this breakout? The chat is not sent back.")) return;
    pend.current = null;
    // The card's own research run would otherwise outlive its only view.
    if (state.current.running) state.current.stop?.();
    closeBreakout(card.id);
  };
  // SEND BACK asks the breakout for a handoff note as a new message; the draft
  // is built from THAT reply when the run ends (see `onState`).
  const sendBack = () => {
    if (pend.current || state.current.running || !state.current.send) return;
    setError("");
    pend.current = { from: state.current.turns.length, saw: false };
    setWriting(true);
    state.current.send(HANDOFF_PROMPT);
  };
  const cancelWriting = () => {
    pend.current = null;
    setWriting(false);
    state.current.stop?.();
  };

  return (
    <FloatingCard
      index={index}
      label="Breakout"
      title="Breakout"
      note="research only"
      actions={
        <>
          {writing ? (
            <button type="button" className="breakout-btn" onClick={cancelWriting}>
              Cancel
            </button>
          ) : null}
          <button
            type="button"
            className="breakout-btn"
            onClick={sendBack}
            disabled={!canSend || running || writing}
            data-hint={
              running && !writing ? "Wait for the breakout to finish" : "Have the breakout write a handoff note for the chat's composer"
            }
          >
            {writing ? "Writing handoff…" : "Send back"}
          </button>
        </>
      }
      error={error}
      onClose={close}
    >
      <div className="claude-rail-peek-chat is-shown">
        <Mount
          file={card.cwd || card.file}
          breakout
          backend="claude"
          breakoutContext={card.context}
          {...(card.prompt ? { breakoutPrompt: card.prompt, onBreakoutPromptSent: () => consumeBreakoutPrompt(card.id) } : {})}
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
      title="Agent"
      note={peek.title}
      noteHint={`${peek.title} — ${peek.subtitle}`}
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
  // SPECIALIST BREAKOUTS (docs/specialists): the same chrome, stacked after the
  // plain ones. The store is empty unless the flag is on (only the task side,
  // itself flag-gated, opens one).
  const specialistCards = useSpecialistBreakouts();
  if (!cards.length && !peeks.length && !specialistCards.length) return null;
  return createPortal(
    <>
      {cards.map((c, i) => (
        <Card key={c.id} card={c} index={i} />
      ))}
      {peeks.map((p, i) => (
        <AgentPeekCard key={p.id} peek={p} index={cards.length + i} />
      ))}
      {specialistCards.map((c, i) => (
        <SpecialistBreakoutCard key={c.id} card={c} index={cards.length + peeks.length + i} cascade={i} />
      ))}
    </>,
    document.body,
  );
}
