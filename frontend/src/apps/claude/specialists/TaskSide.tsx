// THE SPECIALIST SIDE OF ONE CHAT MOUNT (docs/specialists PLAN C.4), in one
// hook so ClaudeChat carries a handful of lines for the whole feature:
//
//   cards     the CardStack seam (context.tsx): the offer card in a task, the
//             services card's "Approve, don't ask again" in a main chat.
//   afterTurn what the transcript draws after a turn: the event chips placed
//             there by time, and "Send to a specialist" on a message.
//   tail      chips older than every timed turn.
//   dock      above the composer: the draft label and the Breakout control.
//   noteDraft the composer-draft listener's report of a labelled draft.
//
// Everything is null with the flag off, and inside a specialist session the
// task-side parts are null too (a breakout cannot open a breakout, P).
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useSpecialistsEnabled } from "@platform/lib/specialists-flag";
import type { Turn } from "../protocol/controller-api";
import type { PermissionRow } from "../protocol/types";
import type { CardActions } from "../ui/CardStack";
import { PermCard } from "../ui/PermCard";
import { BreakoutMenu } from "./BreakoutMenu";
import { OFFER_TOOL, type SpecialistCards } from "./context";
import { OfferCard } from "./OfferCard";
import { SendToSpecialist } from "./SendToSpecialist";
import { isServiceTool, ServiceCardExtra } from "./ServiceCardExtra";
import { placeEvents, TaskEventChips } from "./TaskEventChips";
import { useSpecialistList, useTaskEvents } from "./useTaskEvents";

export interface TaskSideOptions {
  /** A Claude chat (other agents get no specialist tools). */
  claude: boolean;
  sessionId: string;
  file: string;
  runId?: string;
  title?: string;
  turns: readonly Turn[];
  /** Set inside a specialist session (main chat or breakout card). */
  specialist?: { id: string; kind: "main" | "breakout" };
  /** Today's breakout pick mode, when the plain breakout is available here. */
  onCopyOfChat?: () => void;
}

export interface TaskSide {
  cards: SpecialistCards | null;
  afterTurn: ((turn: Turn) => ReactNode) | undefined;
  tail: ReactNode;
  dock: ReactNode;
  noteDraft: (label: string | undefined) => void;
}

const userCount = (turns: readonly Turn[]) => turns.filter((t) => t.role === "user").length;

export function DraftLabel({ label, onDismiss }: { label: string; onDismiss: () => void }) {
  return (
    <div className="spx-draft-label" role="status">
      <span className="spx-draft-label-text">{label}</span>
      <span className="spx-draft-label-hint">Edit it, add to it, then send</span>
      <button type="button" className="spx-linkbtn" onClick={onDismiss} aria-label="Hide the draft label">
        Hide
      </button>
    </div>
  );
}

export function useSpecialistTaskSide(o: TaskSideOptions): TaskSide {
  const on = useSpecialistsEnabled();
  const inSpecialist = !!o.specialist;
  const task = on && o.claude && !inSpecialist && !!o.file;
  const { events, refresh } = useTaskEvents(o.sessionId, task && !!o.sessionId);
  const specialists = useSpecialistList(task);

  // The draft label: shown from the draft's arrival until the next message
  // the reader sends (or Hide).
  const [draft, setDraft] = useState<{ label: string; users: number } | null>(null);
  const users = userCount(o.turns);
  useEffect(() => {
    if (draft && users > draft.users) setDraft(null);
  }, [draft, users]);
  const noteDraft = useCallback(
    (label: string | undefined) => {
      if (!on || !label) return;
      setDraft({ label, users });
    },
    [on, users],
  );

  const host = useMemo(
    () => ({
      parentSessionId: o.sessionId,
      parentFile: o.file,
      ...(o.title ? { parentTitle: o.title } : {}),
      ...(o.runId ? { runId: o.runId } : {}),
      refresh,
    }),
    [o.sessionId, o.file, o.title, o.runId, refresh],
  );

  const specialistId = o.specialist?.id || "";
  const specialistKind = o.specialist?.kind || "";
  const cards = useMemo<SpecialistCards | null>(() => {
    if (!on) return null;
    return {
      renderCard(row: PermissionRow, actions: CardActions, liveMode) {
        if (task && row.tool === OFFER_TOOL) {
          return <OfferCard key={row.id} row={row} actions={actions} host={host} />;
        }
        if (specialistKind === "main" && isServiceTool(row.tool) && !row.decision) {
          return (
            <div key={row.id} className="spx-service-card">
              <PermCard row={row} liveMode={liveMode} onDecide={actions.decidePermission} />
              <ServiceCardExtra specialistId={specialistId} requestId={row.id} tool={row.tool} actions={actions} />
            </div>
          );
        }
        return null;
      },
    };
  }, [on, task, host, specialistKind, specialistId]);

  const placed = useMemo(() => placeEvents(o.turns, events), [o.turns, events]);
  const afterTurn = useMemo(() => {
    if (!task) return undefined;
    return (turn: Turn): ReactNode => {
      const evs = placed.after.get(turn.key);
      const text = turn.role === "user" || turn.role === "assistant" ? turn.text?.trim() : "";
      const settled = turn.role === "user" ? !turn.pending : turn.role === "assistant" ? !turn.streaming : false;
      // Any settled message of a task with a session (a note can go to the
      // general inbox even before the user has a specialist).
      const canSend = !!text && settled && !!o.sessionId;
      if (!evs && !canSend) return null;
      return (
        <>
          {canSend ? (
            <div className={"spx-turn-acts" + (turn.role === "user" ? " is-user" : "")}>
              <SendToSpecialist
                text={text || ""}
                {...("uuid" in turn && turn.uuid ? { uuid: turn.uuid } : {})}
                sessionId={o.sessionId}
                {...(o.title ? { title: o.title } : {})}
                specialists={specialists}
                onSent={refresh}
              />
            </div>
          ) : null}
          {evs ? <TaskEventChips events={evs} host={host} /> : null}
        </>
      );
    };
  }, [task, placed, specialists, o.sessionId, o.title, host, refresh]);

  const tail = task && placed.tail.length ? <TaskEventChips events={placed.tail} host={host} /> : null;

  const showMenu = task && !!o.sessionId && (specialists.length > 0 || !!o.onCopyOfChat);
  const dock =
    on && (draft || (showMenu && specialists.length > 0)) ? (
      <div className="spx-dock">
        {draft ? <DraftLabel label={draft.label} onDismiss={() => setDraft(null)} /> : null}
        {showMenu && specialists.length > 0 ? (
          <BreakoutMenu specialists={specialists} host={host} {...(o.onCopyOfChat ? { onCopyOfChat: o.onCopyOfChat } : {})} />
        ) : null}
      </div>
    ) : null;

  return { cards, afterTurn, tail, dock, noteDraft };
}
