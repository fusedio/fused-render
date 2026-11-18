// A SPECIALIST BREAKOUT CARD (PRODUCT §3.3.4–9, PLAN C.4): the floating
// breakout card's chrome (`FloatingCard`), holding an ordinary chat that runs
// in the specialist's home as a fresh session (never the research-only
// `breakout` posture). Header: the specialist's name, "Breakout from <task>",
// Discard and Close.
//
//   first open  the brief (`seed`) is sent once as the first message.
//   Close       POST close-prompt → send it into the breakout → when that turn
//               ends POST closed → the summary goes into the task's composer
//               as a labelled draft (appended below what was typed; saved to
//               the persisted draft when no task chat is mounted) → card closes.
//   Discard     confirm → POST discard → stop the run → card closes; no draft.
import { useCallback, useRef, useState, type ComponentProps, type ComponentType } from "react";
import { ChatMount } from "@apps/claude";
import type { Turn } from "@apps/claude/protocol/controller-api";
import {
  breakoutClosed,
  closePrompt,
  discardBreakout,
  errorText,
} from "@platform/lib/specialists-api";
import {
  closeSpecialistBreakoutCard,
  deliverComposerDraft,
  updateSpecialistBreakout,
  type SpecialistBreakoutCard as CardData,
} from "@platform/lib/specialist-breakouts";
import { FloatingCard } from "./BreakoutCards";

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
  confirm: (text: string) => boolean;
}

const DEFAULT_DEPS: SpecialistCardDeps = {
  closePrompt,
  breakoutClosed,
  discardBreakout,
  deliver: deliverComposerDraft,
  confirm: (t) => window.confirm(t),
};

export function SpecialistBreakoutCard({
  card,
  index,
  mount: Mount = ChatMount,
  deps = DEFAULT_DEPS,
}: {
  card: CardData;
  index: number;
  // Seams for tests.
  mount?: ComponentType<ComponentProps<typeof ChatMount>>;
  deps?: SpecialistCardDeps;
}) {
  const state = useRef<ChatState>({ sessionId: card.sessionId || "", turns: [] });
  const [closing, setClosing] = useState(false);
  const [error, setError] = useState("");
  /** The close-out turn in flight: whether the chat has been seen running yet
   *  (idle alone means the send has not landed). */
  const pend = useRef<{ saw: boolean } | null>(null);

  const finish = useCallback(async () => {
    try {
      const sid = state.current.sessionId || card.sessionId || "";
      const r = await deps.breakoutClosed(card.specialistId, card.breakoutId, sid);
      const ok = await deps.deliver({
        sessionId: card.parentSessionId,
        file: card.parentFile,
        text: r.draft,
        label: r.label || `Draft from the ${card.specialistName} specialist · not sent yet`,
      });
      if (!ok) {
        setError("Could not put the summary in the task's message box. The card stays open.");
        setClosing(false);
        return;
      }
      closeSpecialistBreakoutCard(card.id);
    } catch (err) {
      setError(errorText(err));
      setClosing(false);
    }
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
    if (closing) return;
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
    setClosing(true);
    setError("");
    try {
      const { message } = await deps.closePrompt(card.specialistId, card.breakoutId);
      pend.current = { saw: false };
      updateSpecialistBreakout(card.id, { closing: true });
      s.send(message);
    } catch (err) {
      setError(errorText(err));
      setClosing(false);
    }
  };

  const discard = async () => {
    if (!deps.confirm("Discard this breakout? No summary goes back to the task. The specialist still learns from it.")) return;
    setError("");
    try {
      await deps.discardBreakout(card.specialistId, card.breakoutId);
      pend.current = null;
      if (state.current.running) state.current.stop?.();
      closeSpecialistBreakoutCard(card.id);
    } catch (err) {
      setError(errorText(err));
    }
  };

  const from = card.parentTitle ? `Breakout from ${card.parentTitle}` : "Breakout from this task";
  return (
    <FloatingCard
      index={index}
      label={`Breakout with the ${card.specialistName} specialist`}
      title={`${card.specialistName} specialist`}
      note={closing ? "Writing the summary…" : from}
      noteHint={from}
      actions={
        <button type="button" className="breakout-btn" onClick={() => void discard()} disabled={closing}>
          Discard
        </button>
      }
      error={error}
      onClose={() => void close()}
    >
      <div className="claude-rail-peek-chat is-shown">
        <Mount
          file={card.home}
          backend="claude"
          {...(card.sessionId ? { sessionId: card.sessionId } : {})}
          extraParams={card.startParams}
          specialist={{ id: card.specialistId, kind: "breakout" }}
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
    </FloatingCard>
  );
}
