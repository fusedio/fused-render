// THE QUIET CHIPS a task's transcript shows for its specialist events
// (PRODUCT §3.2, §3.3.7, §4; PLAN B.3): "Guidance from the Canvas specialist
// was added · 2 entries · View", "Breakout closed · its full note is saved
// with the Canvas specialist · Reopen", "Sent to the Canvas specialist's inbox
// · Undo". Placed by time (`placeEvents`) after the turn they happened in.
import { useState } from "react";
import {
  deleteNote,
  errorText,
  getReceipt,
  reopenBreakout,
  type TaskEvent,
} from "@platform/lib/specialists-api";
import { openSpecialistBreakout } from "@platform/lib/specialist-breakouts";
import type { Turn } from "../protocol/controller-api";

/** The time a turn happened, epoch seconds, or null when it carries none:
 *  a user turn's `ts`; a reply's end (`endedAt`, or its last segment clock). */
export function turnTime(turn: Turn): number | null {
  if (turn.role === "user") return typeof turn.ts === "number" && turn.ts > 0 ? turn.ts : null;
  if (turn.role === "assistant") {
    if (typeof turn.endedAt === "number" && turn.endedAt > 0) return turn.endedAt;
    let last: number | null = null;
    for (const seg of turn.segments ?? []) {
      const t = (seg as { ended?: number; ts?: number }).ended ?? (seg as { ts?: number }).ts;
      if (typeof t === "number" && t > 0) last = t;
    }
    return last;
  }
  return null;
}

/**
 * WHERE EACH EVENT GOES: after the last turn whose time is at or before the
 * event's. Turns with no clock are skipped as anchors (a live reply that has
 * not ended yet), so a pull made at the top of an exchange lands right under
 * the user's message rather than under a reply that came after it. Events
 * older than every timed turn, or a transcript with no timed turn at all, go to
 * `tail` (drawn after the log). Each list keeps the events' time order.
 */
export function placeEvents(
  turns: readonly Turn[],
  events: readonly TaskEvent[],
): { after: Map<string, TaskEvent[]>; tail: TaskEvent[] } {
  const timed: { key: string; t: number }[] = [];
  for (const turn of turns) {
    const t = turnTime(turn);
    if (t !== null) timed.push({ key: turn.key, t });
  }
  const after = new Map<string, TaskEvent[]>();
  const tail: TaskEvent[] = [];
  const sorted = [...events].sort((a, b) => (a.at || 0) - (b.at || 0));
  for (const ev of sorted) {
    let anchor: string | null = null;
    for (const x of timed) if (x.t <= (ev.at || 0)) anchor = x.key;
    if (!anchor) {
      tail.push(ev);
      continue;
    }
    const list = after.get(anchor) ?? [];
    list.push(ev);
    after.set(anchor, list);
  }
  return { after, tail };
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** The chip's words, without its buttons. Exported for tests. */
export function chipText(ev: TaskEvent): string {
  const name = ev.specialist?.name || "a";
  switch (ev.kind) {
    case "pull":
      return `Guidance from the ${name} specialist was added · ${plural(ev.entries ?? 0, "entry", "entries")}`;
    case "note":
      return `Sent to the ${name} specialist's inbox`;
    case "breakout_open":
      return `Breakout open with the ${name} specialist`;
    case "breakout_closed":
      return `Breakout closed · its full note is saved with the ${name} specialist`;
    case "breakout_discarded":
      return `Breakout with the ${name} specialist discarded · no summary`;
    case "stuck":
      return "The same step kept failing · the task was told it may offer a specialist a breakout";
    default:
      return `${name} specialist`;
  }
}

export interface ChipHost {
  parentSessionId: string;
  parentFile: string;
  parentTitle?: string;
  /** Re-read the events after an action here changed them. */
  refresh: () => void;
}

export function TaskEventChip({
  ev,
  host,
  reopen = reopenBreakout,
  receipt = getReceipt,
  undoNote = deleteNote,
}: {
  ev: TaskEvent;
  host: ChipHost;
  // Seams for tests.
  reopen?: typeof reopenBreakout;
  receipt?: typeof getReceipt;
  undoNote?: typeof deleteNote;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [shown, setShown] = useState<string | null>(null);
  const [undone, setUndone] = useState(false);
  const specId = ev.specialist?.id || "";
  const name = ev.specialist?.name || "";

  const onView = async () => {
    if (shown !== null) {
      setShown(null);
      return;
    }
    if (!ev.receipt_id) return;
    setBusy(true);
    setError("");
    try {
      const r = await receipt(specId, ev.receipt_id);
      setShown(r.text || "");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const onReopen = async () => {
    if (!ev.breakout_id) return;
    setBusy(true);
    setError("");
    try {
      const r = await reopen(specId, ev.breakout_id);
      openSpecialistBreakout({
        breakoutId: ev.breakout_id,
        specialistId: specId,
        specialistName: name,
        home: r.home,
        parentSessionId: host.parentSessionId,
        parentFile: host.parentFile,
        ...(host.parentTitle ? { parentTitle: host.parentTitle } : {}),
        ...(r.session_id ? { sessionId: r.session_id } : {}),
        startParams: r.start_params || {},
      });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const onUndo = async () => {
    if (!ev.note_id) return;
    setBusy(true);
    setError("");
    try {
      await undoNote(specId || "general", ev.note_id);
      setUndone(true);
      host.refresh();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (undone) {
    return (
      <div className="spx-chip is-quiet" role="status">
        <span>Note removed from the {name || "general"} inbox</span>
      </div>
    );
  }
  return (
    <div className={"spx-chip-wrap"} data-spx-event={ev.kind}>
      <div className="spx-chip">
        <span className="spx-chip-dot" aria-hidden="true" />
        <span>{chipText(ev)}</span>
        {ev.kind === "pull" && ev.receipt_id ? (
          <button type="button" className="spx-chip-btn" onClick={() => void onView()} disabled={busy} aria-expanded={shown !== null}>
            {shown !== null ? "Hide" : "View"}
          </button>
        ) : null}
        {(ev.kind === "breakout_closed" || ev.kind === "breakout_open") && ev.breakout_id ? (
          <button type="button" className="spx-chip-btn" onClick={() => void onReopen()} disabled={busy}>
            {ev.kind === "breakout_open" ? "Open" : "Reopen"}
          </button>
        ) : null}
        {ev.kind === "note" && ev.note_id && ev.undoable !== false ? (
          <button type="button" className="spx-chip-btn" onClick={() => void onUndo()} disabled={busy}>
            Undo
          </button>
        ) : null}
      </div>
      {error ? (
        <div className="spx-chip-error" role="alert">
          {error}
        </div>
      ) : null}
      {shown !== null ? <pre className="spx-chip-view">{shown || "(empty)"}</pre> : null}
    </div>
  );
}

export function TaskEventChips({ events, host }: { events: readonly TaskEvent[]; host: ChipHost }) {
  if (!events.length) return null;
  return (
    <div className="spx-chips">
      {events.map((ev, i) => (
        <TaskEventChip key={`${ev.kind}:${ev.at}:${ev.receipt_id || ev.note_id || ev.breakout_id || i}`} ev={ev} host={host} />
      ))}
    </div>
  );
}
