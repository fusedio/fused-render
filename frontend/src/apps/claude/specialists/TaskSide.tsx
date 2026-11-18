// THE SPECIALIST SIDE OF ONE CHAT MOUNT (docs/specialists PLAN C.4), in one
// hook so ClaudeChat carries a handful of lines for the whole feature:
//
//   cards       the CardStack seam (context.tsx): the offer card in a task, the
//               services card's "Approve, don't ask again" in a specialist chat.
//   afterTurn   what the transcript draws after a turn: the event chips placed
//               there by time, and "Send to a specialist" on a message.
//   tail        chips older than every timed turn.
//   dock        above the composer: the summary draft and the Breakout control.
//   replaceTurn inside a specialist session: app-written messages drawn as
//               product UI, never as their machine text (R1, protocol-view).
//   noteDraft   kept for the composer-draft listener (a plain draft has no label).
//
// Everything is null with the flag off, and inside a specialist session the
// task-side parts are null too (a breakout cannot open a breakout, P).
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSpecialistsEnabled } from "@platform/lib/specialists-flag";
import {
  clearSummaryDraft,
  summaryDraftText,
  useSpecialistBreakouts,
  useSummaryDraft,
  type SummaryDraftPart,
} from "@platform/lib/specialist-breakouts";
import type { TaskEvent } from "@platform/lib/specialists-api";
import type { Turn } from "../protocol/controller-api";
import type { PermissionRow } from "../protocol/types";
import type { CardActions } from "../ui/CardStack";
import { PermCard } from "../ui/PermCard";
import { BreakoutMenu } from "./BreakoutMenu";
import { OFFER_TOOL, type SpecialistCards, type SpecialistSessionProp } from "./context";
import { OfferCard } from "./OfferCard";
import { parseSummary, protocolTurnView, SummaryRows } from "./protocol-view";
import { SendToSpecialist } from "./SendToSpecialist";
import { isServiceTool, ServiceCardExtra } from "./ServiceCardExtra";
import { endedBreakouts, latestPerBreakout, placeEvents, TaskEventChips } from "./TaskEventChips";
import { useSpecialistList, useTaskEvents } from "./useTaskEvents";
import "./task.css";

export interface TaskSideOptions {
  /** A Claude chat (other agents get no specialist tools). */
  claude: boolean;
  sessionId: string;
  file: string;
  runId?: string;
  title?: string;
  turns: readonly Turn[];
  /** Set inside a specialist session (main chat or breakout card). */
  specialist?: SpecialistSessionProp;
  /** Today's breakout pick mode, when the plain breakout is available here. */
  onCopyOfChat?: () => void;
  /** A run is live in this chat: chips are re-read while it runs and when it ends. */
  running?: boolean;
  /** The task's working folder (triggers, file links in a breakout). */
  folder?: string;
  /** The composer's own send with `text` added below what the reader typed;
   *  false when it did not send. "Send to the task" on a summary draft. */
  submit?: (text: string) => boolean;
}

export interface TaskSide {
  cards: SpecialistCards | null;
  afterTurn: ((turn: Turn) => ReactNode) | undefined;
  tail: ReactNode;
  dock: ReactNode;
  replaceTurn: ((turn: Turn) => ReactNode | undefined) | undefined;
  noteDraft: (label: string | undefined) => void;
}

/** While a run is live the chips are re-read this often (a pull or a note made
 *  mid-turn shows without waiting for the 15 s poll). */
export const TASK_EVENTS_LIVE_MS = 4_000;

/** How many tool calls have finished: a change re-reads the chips (a pull or a
 *  note is a tool call). */
function finishedTools(turns: readonly Turn[]): number {
  let n = 0;
  for (const t of turns) {
    if (t.role !== "assistant") continue;
    for (const seg of t.segments ?? []) if (seg.kind === "tool" && seg.status !== "running") n += 1;
  }
  return n;
}

/** THE SUMMARY DRAFT, its own block above the composer (TaskReturn mock):
 *  who it is from, the summary laid out, Clear draft and Send to the task. */
export function SummaryDraftBlock({
  parts,
  onClear,
  onSend,
}: {
  parts: readonly SummaryDraftPart[];
  onClear: () => void;
  onSend: () => void;
}) {
  if (!parts.length) return null;
  const names = [...new Set(parts.map((p) => p.name).filter(Boolean))];
  const label =
    names.length === 1
      ? `Draft from the ${names[0]} specialist · not sent yet`
      : names.length > 1
        ? `Draft from the ${names.join(" and ")} specialists · not sent yet`
        : parts[0]!.label;
  return (
    <section className="spx-draft" aria-label={label}>
      <div className="spx-draft-head">
        <span className="spx-draft-label-text">{label}</span>
        <span className="spx-draft-label-hint">Add your own words in the message box; they go first.</span>
      </div>
      <div className="spx-draft-body">
        {parts.map((p, i) => (
          <SummaryRows key={`${p.at}:${i}`} fields={parseSummary(p.text)} />
        ))}
      </div>
      <div className="spx-draft-actions">
        <button type="button" className="spx-btn is-primary" onClick={onSend}>
          Send to the task
        </button>
        <button type="button" className="spx-btn" onClick={onClear}>
          Clear draft
        </button>
      </div>
    </section>
  );
}

export function useSpecialistTaskSide(o: TaskSideOptions): TaskSide {
  const on = useSpecialistsEnabled();
  const inSpecialist = !!o.specialist;
  const task = on && o.claude && !inSpecialist && !!o.file;
  const { events: rawEvents, refresh } = useTaskEvents(o.sessionId, task && !!o.sessionId);
  const specialists = useSpecialistList(task);

  // This task's breakout cards live in the shell, not in this chat: when one
  // opens, closes or is discarded the server has a new event for this task,
  // so read the events again then rather than on the next 15 s poll.
  const openCards = useSpecialistBreakouts();
  const cardSig = task
    ? openCards
        .filter((c) => c.parentSessionId === o.sessionId)
        .map((c) => c.breakoutId)
        .sort()
        .join(",")
    : "";
  const lastSig = useRef(cardSig);
  useEffect(() => {
    if (lastSig.current === cardSig) return;
    lastSig.current = cardSig;
    refresh();
  }, [cardSig, refresh]);

  // …and when a turn ends, when a tool call finishes, and every few seconds
  // while the task runs (QA: the pull and stuck chips came only on reload).
  const running = !!o.running;
  const toolsDone = task ? finishedTools(o.turns) : 0;
  const wasRunning = useRef(running);
  const lastTools = useRef(toolsDone);
  useEffect(() => {
    if (!task) return;
    const ended = wasRunning.current && !running;
    const moved = toolsDone !== lastTools.current;
    wasRunning.current = running;
    lastTools.current = toolsDone;
    if (ended || moved) refresh();
  }, [task, running, toolsDone, refresh]);
  useEffect(() => {
    if (!task || !running) return;
    const t = setInterval(refresh, TASK_EVENTS_LIVE_MS);
    return () => clearInterval(t);
  }, [task, running, refresh]);

  // Notes sent from a message on this page: their own line under the message
  // carries the one Undo, so their chip is not drawn twice.
  const [sentHere, setSentHere] = useState<ReadonlySet<string>>(() => new Set());
  const onNoteSent = useCallback(
    (noteId: string, sent: boolean) => {
      setSentHere((prev) => {
        const next = new Set(prev);
        if (sent) next.add(noteId);
        else next.delete(noteId);
        return next;
      });
      refresh();
    },
    [refresh],
  );
  const events = useMemo<readonly TaskEvent[]>(() => {
    const one = latestPerBreakout(rawEvents);
    return sentHere.size ? one.filter((e) => !(e.kind === "note" && e.note_id && sentHere.has(e.note_id))) : one;
  }, [rawEvents, sentHere]);

  // THE SUMMARY DRAFT of a closed breakout, kept until sent or cleared.
  const draftParts = useSummaryDraft(task ? o.sessionId : "", o.file);
  const [draftError, setDraftError] = useState("");
  const { submit } = o;
  const sendDraft = useCallback(() => {
    const text = summaryDraftText(draftParts);
    if (!text) return;
    if (submit?.(text)) {
      clearSummaryDraft(o.sessionId, o.file);
      setDraftError("");
    } else {
      setDraftError("The message box can't send right now. Try again in a moment.");
    }
  }, [draftParts, submit, o.sessionId, o.file]);
  const clearDraft = useCallback(() => {
    clearSummaryDraft(o.sessionId, o.file);
    setDraftError("");
  }, [o.sessionId, o.file]);

  const noteDraft = useCallback((_label: string | undefined) => {
    // A specialist's summary no longer travels through the composer (it is
    // the summary draft above); a plain Send back has no label.
  }, []);

  const host = useMemo(
    () => ({
      parentSessionId: o.sessionId,
      parentFile: o.file,
      ...(o.title ? { parentTitle: o.title } : {}),
      ...(o.runId ? { runId: o.runId } : {}),
      ...(o.folder ? { parentFolder: o.folder } : {}),
      refresh,
    }),
    [o.sessionId, o.file, o.title, o.runId, o.folder, refresh],
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
        if ((specialistKind === "main" || specialistKind === "breakout") && isServiceTool(row.tool) && !row.decision) {
          return (
            <ServiceCardExtra
              key={row.id}
              specialistId={specialistId}
              requestId={row.id}
              tool={row.tool}
              actions={actions}
              kind={specialistKind}
              card={(more) => <PermCard row={row} liveMode={liveMode} onDecide={actions.decidePermission} {...(more ? { moreActions: more } : {})} />}
            />
          );
        }
        return null;
      },
    };
  }, [on, task, host, specialistKind, specialistId]);

  const placed = useMemo(() => placeEvents(o.turns, events), [o.turns, events]);
  const ended = useMemo(() => endedBreakouts(events), [events]);
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
                onSent={onNoteSent}
              />
            </div>
          ) : null}
          {evs ? <TaskEventChips events={evs} host={host} ended={ended} /> : null}
        </>
      );
    };
  }, [task, placed, ended, specialists, o.sessionId, o.title, host, onNoteSent]);

  const tail = task && placed.tail.length ? <TaskEventChips events={placed.tail} host={host} ended={ended} /> : null;

  // R1: inside a specialist session, the app's own messages as product UI.
  const brief = o.specialist?.brief;
  const taskTitle = o.specialist?.taskTitle;
  const replaceTurn = useMemo(() => {
    if (!inSpecialist) return undefined;
    return (turn: Turn) => protocolTurnView(turn, { brief, taskTitle });
  }, [inSpecialist, brief, taskTitle]);

  const showMenu = task && !!o.sessionId && specialists.length > 0;
  const hasDraft = task && draftParts.length > 0;
  const dock =
    on && (hasDraft || showMenu) ? (
      <div className="spx-dock">
        {showMenu ? (
          <BreakoutMenu
            specialists={specialists}
            host={host}
            turns={o.turns}
            {...(o.folder ? { folder: o.folder } : {})}
            {...(o.onCopyOfChat ? { onCopyOfChat: o.onCopyOfChat } : {})}
          />
        ) : null}
        {hasDraft ? <SummaryDraftBlock parts={draftParts} onClear={clearDraft} onSend={sendDraft} /> : null}
        {hasDraft && draftError ? (
          <div className="spx-chip-error" role="alert">
            {draftError}
          </div>
        ) : null}
      </div>
    ) : null;

  return { cards, afterTurn, tail, dock, replaceTurn, noteDraft };
}
