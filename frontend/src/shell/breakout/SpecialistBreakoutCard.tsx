// A SPECIALIST BREAKOUT CARD (PRODUCT §3.3.4–9, PLAN C.4): the floating
// breakout card's chrome (`FloatingCard`), holding an ordinary chat that runs
// in the breakout's workroom as a fresh session (never the research-only
// `breakout` posture). Header: the specialist's face and name, "Breakout from
// <task>" on a second line, Discard and Close. Tinted, draggable, resizable,
// and it opens BESIDE the task's chat (over the middle pane), not on it; a
// second card cascades clearly below the first one's head.
//
//   first open  the brief (`seed`) is sent once as the first message; the
//               thread draws it as the brief box (R1, protocol-view).
//   Close       POST close-prompt → send it into the breakout (the thread
//               hides it; the head says "Writing the summary…") → when that
//               turn ends POST closed → the summary becomes the task's summary
//               draft → card closes. Cannot be pressed twice.
//   Discard     the card's own confirm → POST discard → stop the run → closes.
//   Deleted     the specialist was deleted (410 `reason: "deleted"`): the card
//               says so and can be dismissed.
import { useCallback, useEffect, useRef, useState, type ComponentProps, type ComponentType } from "react";
import { ChatMount } from "@apps/claude";
import type { Turn } from "@apps/claude/protocol/controller-api";
import {
  breakoutClosed,
  closePrompt,
  discardBreakout,
  errorText,
  getSpecialist,
  isSpecialistDeleted,
  specialistFaceColor,
  type SpecialistFace,
} from "@platform/lib/specialists-api";
import {
  closeSpecialistBreakoutCard,
  deliverComposerDraft,
  updateSpecialistBreakout,
  type SpecialistBreakoutCard as CardData,
} from "@platform/lib/specialist-breakouts";
import { FloatingCard } from "./BreakoutCards";
import "./specialist-card.css";

type ChatState = {
  sessionId: string;
  turns: readonly Turn[];
  running?: boolean;
  stop?: () => void;
  send?: (text: string) => void;
};

export interface SpecialistCardDeps {
  closePrompt: typeof closePrompt;
  breakoutClosed: typeof breakoutClosed;
  discardBreakout: typeof discardBreakout;
  deliver: typeof deliverComposerDraft;
  /** The specialist's facts (model, face); a 404/410 means it was deleted. */
  detail: (id: string) => Promise<{ model?: string; face?: SpecialistFace }>;
}

const DEFAULT_DEPS: SpecialistCardDeps = {
  closePrompt,
  breakoutClosed,
  discardBreakout,
  deliver: deliverComposerDraft,
  detail: getSpecialist,
};

/** How long the chat waits for the specialist's model before it starts
 *  anyway (on the composer's own default). */
const DETAIL_WAIT_MS = 1500;
/** How often an open card checks that its specialist still exists. */
export const DELETED_POLL_MS = 10_000;

const CARD_W = 440;
const CARD_H = 620;
const GAP = 12;
/** A second card's step: more than the head row, so the first one's Discard
 *  and Close stay in view. */
const CASCADE_Y = 64;
const CASCADE_X = 28;

/** Where a card opens: beside the task's chat when there is room (to its
 *  left, over the middle pane; else to its right), otherwise at the window's
 *  left edge; each further card steps down and in. Viewport px. */
export function cardBox(
  anchor: CardData["anchor"] | undefined,
  cascade: number,
  view: { w: number; h: number },
): { x: number; y: number; w: number; h: number } {
  const w = Math.min(CARD_W, view.w - 32);
  const h = Math.min(CARD_H, Math.round(view.h * 0.8));
  let x: number;
  if (anchor && anchor.left - GAP - w >= 8) x = anchor.left - GAP - w;
  else if (anchor && view.w - anchor.right - GAP - w >= 8) x = anchor.right + GAP;
  else if (anchor) x = 16;
  // No anchor: the task's chat is most often the right-hand pane.
  else x = Math.max(16, view.w - w - 16 - 480);
  let y = anchor ? Math.max(16, Math.min(anchor.top + 8, view.h - h - 40)) : Math.max(16, view.h - h - 40);
  y += cascade * CASCADE_Y;
  x = anchor && x > anchor.right ? x + cascade * CASCADE_X : x - cascade * CASCADE_X;
  return {
    x: Math.max(0, Math.min(x, view.w - w)),
    y: Math.max(0, Math.min(y, view.h - h)),
    w,
    h,
  };
}

export function SpecialistBreakoutCard({
  card,
  index,
  cascade = 0,
  mount: Mount = ChatMount,
  deps = DEFAULT_DEPS,
}: {
  card: CardData;
  index: number;
  /** This card's place among the open specialist cards (0 = first). */
  cascade?: number;
  // Seams for tests.
  mount?: ComponentType<ComponentProps<typeof ChatMount>>;
  deps?: SpecialistCardDeps;
}) {
  const state = useRef<ChatState>({ sessionId: card.sessionId || "", turns: [] });
  const [closing, setClosing] = useState(false);
  const closingRef = useRef(false);
  const [error, setError] = useState("");
  const [deleted, setDeleted] = useState(false);
  const [asking, setAsking] = useState(false);
  const [facts, setFacts] = useState<{ model?: string; face?: SpecialistFace } | null>(null);
  const [ready, setReady] = useState(false);
  /** The close-out turn in flight: whether the chat has been seen running yet
   *  (idle alone means the send has not landed). */
  const pend = useRef<{ saw: boolean } | null>(null);
  const [box] = useState(() =>
    cardBox(card.anchor, cascade, {
      w: typeof window !== "undefined" ? window.innerWidth : 1280,
      h: typeof window !== "undefined" ? window.innerHeight : 800,
    }),
  );

  // The specialist's model is the chat's default (R7); a deleted specialist
  // has no chat to open.
  const detail = deps.detail;
  useEffect(() => {
    let live = true;
    const t = setTimeout(() => live && setReady(true), DETAIL_WAIT_MS);
    detail(card.specialistId).then(
      (d) => {
        if (!live) return;
        setFacts({ ...(d?.model ? { model: d.model } : {}), ...(d?.face !== undefined ? { face: d.face } : {}) });
        setReady(true);
      },
      (err) => {
        if (!live) return;
        const status = (err as { status?: number })?.status;
        if (isSpecialistDeleted(err) || status === 404) setDeleted(true);
        setReady(true);
      },
    );
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [card.specialistId, detail]);

  // A specialist deleted while its card is open (in another tab or window):
  // seen live, the card went on chatting as if nothing happened. Ask again
  // now and then; the card turns into "This specialist was deleted" + Dismiss.
  useEffect(() => {
    if (deleted) return;
    let live = true;
    const t = setInterval(() => {
      detail(card.specialistId).then(
        () => {},
        (err) => {
          const status = (err as { status?: number })?.status;
          if (live && (isSpecialistDeleted(err) || status === 404)) setDeleted(true);
        },
      );
    }, DELETED_POLL_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [card.specialistId, detail, deleted]);

  const gone = (err: unknown): boolean => {
    if (!isSpecialistDeleted(err)) return false;
    setDeleted(true);
    setError("");
    return true;
  };

  const finish = useCallback(async () => {
    try {
      const sid = state.current.sessionId || card.sessionId || "";
      const r = await deps.breakoutClosed(card.specialistId, card.breakoutId, sid);
      const ok = await deps.deliver({
        sessionId: card.parentSessionId,
        file: card.parentFile,
        text: r.draft,
        label: r.label || `Draft from the ${card.specialistName} specialist · not sent yet`,
        name: card.specialistName,
        breakoutId: card.breakoutId,
      });
      if (!ok) {
        setError("Could not put the summary in the task's message box. The card stays open.");
        setClosing(false);
        closingRef.current = false;
        return;
      }
      closeSpecialistBreakoutCard(card.id);
    } catch (err) {
      if (!gone(err)) setError(errorText(err));
      setClosing(false);
      closingRef.current = false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card, deps]);

  const onState = useCallback(
    (s: ChatState) => {
      state.current = s;
      if (s.sessionId && s.sessionId !== card.sessionId) updateSpecialistBreakout(card.id, { sessionId: s.sessionId });
      const p = pend.current;
      if (!p) return;
      if (s.running) p.saw = true;
      else if (p.saw) {
        pend.current = null;
        void finish();
      }
    },
    [card.id, card.sessionId, finish],
  );

  const close = async () => {
    if (deleted) {
      closeSpecialistBreakoutCard(card.id);
      return;
    }
    // Once only: a second press while the summary is written does nothing.
    if (closingRef.current) return;
    const s = state.current;
    // Nothing happened in it yet: nothing to sum up.
    if (!s.sessionId && !s.turns.some((t) => t.role === "user")) {
      closeSpecialistBreakoutCard(card.id);
      return;
    }
    if (s.running) {
      setError("Wait for the specialist to finish its reply, then close.");
      return;
    }
    if (!s.send) return;
    closingRef.current = true;
    setClosing(true);
    setError("");
    try {
      const { message } = await deps.closePrompt(card.specialistId, card.breakoutId);
      pend.current = { saw: false };
      updateSpecialistBreakout(card.id, { closing: true });
      s.send(message);
    } catch (err) {
      if (!gone(err)) setError(errorText(err));
      setClosing(false);
      closingRef.current = false;
    }
  };

  const discard = async () => {
    setAsking(false);
    setError("");
    try {
      await deps.discardBreakout(card.specialistId, card.breakoutId);
      pend.current = null;
      if (state.current.running) state.current.stop?.();
      closeSpecialistBreakoutCard(card.id);
    } catch (err) {
      if (!gone(err)) setError(errorText(err));
    }
  };

  const name = card.specialistName;
  const from = card.parentTitle ? `Breakout from ${card.parentTitle}` : "Breakout from this task";
  const second = deleted ? "This specialist was deleted" : closing ? "Writing the summary…" : from;
  const head = (
    <span className="spx-card-who">
      <span
        className="spx-card-face"
        aria-hidden="true"
        style={{ background: specialistFaceColor(facts?.face, card.specialistId) }}
      >
        {(name.trim()[0] || "?").toUpperCase()}
      </span>
      <span className="spx-card-lines">
        <span className="spx-card-name">{name} specialist</span>
        <span className={"spx-card-from" + (closing ? " is-busy" : "")} data-hint={from} role={closing ? "status" : undefined}>
          {second}
        </span>
      </span>
    </span>
  );

  return (
    <FloatingCard
      index={index}
      label={`Breakout with the ${name} specialist`}
      title={`${name} specialist`}
      note={second}
      noteHint={from}
      head={head}
      className="is-specialist"
      initialBox={box}
      resizable
      closeDisabled={closing && !deleted}
      actions={
        deleted ? null : (
          <button type="button" className="breakout-btn" onClick={() => setAsking(true)} disabled={closing || asking}>
            Discard
          </button>
        )
      }
      error={error}
      onClose={() => void close()}
    >
      {asking ? (
        <div className="spx-card-confirm" role="alertdialog" aria-label="Discard this breakout?">
          <div className="spx-card-confirm-title">Discard this breakout?</div>
          <div className="spx-card-confirm-text">
            No summary goes back to the task. The specialist still learns from the conversation.
          </div>
          <div className="spx-card-confirm-actions">
            <button type="button" className="breakout-btn is-danger" onClick={() => void discard()}>
              Discard breakout
            </button>
            <button type="button" className="breakout-btn" onClick={() => setAsking(false)}>
              Keep it
            </button>
          </div>
        </div>
      ) : null}
      {deleted ? (
        <div className="spx-card-gone" role="status">
          <p>This specialist was deleted. Its breakout can't continue.</p>
          <button type="button" className="breakout-btn" onClick={() => closeSpecialistBreakoutCard(card.id)}>
            Dismiss
          </button>
        </div>
      ) : ready ? (
        <div className="claude-rail-peek-chat is-shown">
          <Mount
            file={card.home}
            backend="claude"
            {...(card.sessionId ? { sessionId: card.sessionId } : {})}
            {...(facts?.model ? { model: facts.model } : {})}
            extraParams={card.startParams}
            specialist={{
              id: card.specialistId,
              kind: "breakout",
              name,
              ...(card.brief ? { brief: card.brief } : {}),
              ...(card.parentTitle ? { taskTitle: card.parentTitle } : {}),
              ...(card.parentFolder ? { folder: card.parentFolder } : {}),
            }}
            placeholder={`Reply to the ${name} specialist`}
            {...(card.seed
              ? { breakoutPrompt: card.seed, onBreakoutPromptSent: () => updateSpecialistBreakout(card.id, { seed: undefined }) }
              : {})}
            onBreakoutState={onState}
            chatOnly
            peek
            noAnnTools
            paramsSource="memory"
          />
        </div>
      ) : (
        <div className="spx-card-wait" role="status">
          Opening the breakout…
        </div>
      )}
    </FloatingCard>
  );
}
