// THE QUIET CHIPS a task's transcript shows for its specialist events
// (docs/specialists.md §4 "Task events and chips"): "Guidance from the Canvas specialist
// was added · 2 entries · View", "Breakout closed · its full note is saved
// with the Canvas specialist · Reopen", "Sent to the Canvas specialist's inbox
// · Undo". Placed by time (`placeEvents`) after the turn they happened in.
import { useRef, useState } from "react";
import {
  confirmNote,
  deleteNote,
  errorText,
  getReceipt,
  isSpecialistDeleted,
  listBreakouts,
  reopenBreakout,
  type ReceiptView,
  type TaskEvent,
} from "@platform/lib/specialists-api";
import { chatAnchorOf, openSpecialistBreakout } from "@platform/lib/specialist-breakouts";
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

/** What a pull added, counted: "2 memory entries, 1 skill". */
export function pullCounts(ev: Pick<TaskEvent, "entries" | "skills" | "knowledge">): string {
  const parts: string[] = [];
  if (ev.entries) parts.push(plural(ev.entries, "memory entry", "memory entries"));
  if (ev.knowledge) parts.push(plural(ev.knowledge, "topic file", "topic files"));
  if (ev.skills) parts.push(plural(ev.skills, "skill", "skills"));
  return parts.join(", ");
}

/** The general inbox ("no specialist fits") is not a specialist. */
export function isGeneralInbox(ev: TaskEvent): boolean {
  const id = ev.specialist?.id || "";
  const name = (ev.specialist?.name || "").trim().toLowerCase();
  return id === "general" || !id || name === "general" || name === "general inbox";
}

/** One event per breakout, its latest state (the server sends one already; an older
 *  server sent one per transition, which drew three stacked chips). */
export function latestPerBreakout(events: readonly TaskEvent[]): TaskEvent[] {
  const last = new Map<string, TaskEvent>();
  for (const ev of events) {
    if (!ev.kind.startsWith("breakout_") || !ev.breakout_id) continue;
    const prev = last.get(ev.breakout_id);
    if (!prev || (ev.at || 0) >= (prev.at || 0)) last.set(ev.breakout_id, ev);
  }
  return events.filter((ev) => !ev.kind.startsWith("breakout_") || !ev.breakout_id || last.get(ev.breakout_id) === ev);
}

/** The chip's words, without its buttons. Exported for tests. */
export function chipText(ev: TaskEvent): string {
  const name = ev.specialist?.name || "a";
  switch (ev.kind) {
    case "pull": {
      const n = pullCounts(ev);
      return `Guidance from the ${name} specialist was added${n ? ` · ${n}` : ""}`;
    }
    case "note":
      if (isGeneralInbox(ev)) return "Sent to the general inbox";
      if (ev.basis === "user-said" && ev.in_effect !== true) return `Note for the ${name} specialist · The task says you stated this`;
      return `Sent to the ${name} specialist's inbox`;
    case "breakout_open":
      return `Breakout open with the ${name} specialist`;
    case "breakout_closed":
      return `Breakout closed · its full note is saved with the ${name} specialist`;
    case "breakout_discarded":
      return `Breakout with the ${name} specialist discarded · no summary`;
    case "breakout_deleted":
      return `Breakout with the ${name} specialist · specialist deleted`;
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
  /** The task's folder, for file links in the breakout's replies. */
  parentFolder?: string;
  /** Re-read the events after an action here changed them. */
  refresh: () => void;
}

/** Breakouts that a later event closed or discarded: their earlier
 *  "Breakout open" chip stays as history, without its Open button (the closed
 *  chip carries Reopen). */
export function endedBreakouts(events: readonly TaskEvent[]): ReadonlySet<string> {
  const out = new Set<string>();
  for (const ev of events) {
    if (
      (ev.kind === "breakout_closed" || ev.kind === "breakout_discarded" || ev.kind === "breakout_deleted") &&
      ev.breakout_id
    )
      out.add(ev.breakout_id);
  }
  return out;
}

/** THE GUIDANCE A PULL ADDED, as a person reads it (the receipt's `view`): never the raw
 *  payload, its receipt line or its provenance braces. */
export function ReceiptSheet({ view }: { view: ReceiptView }) {
  const memory = view.memory ?? [];
  const said = view.said ?? [];
  const knowledge = view.knowledge ?? [];
  const skills = view.skills ?? [];
  const empty = !memory.length && !said.length && !knowledge.length && !skills.length;
  return (
    <div className="spx-receipt" role="region" aria-label="What the guidance added">
      {empty ? <div className="spx-receipt-empty">Nothing from memory matched this task.</div> : null}
      {memory.length ? (
        <section>
          <h4>From memory</h4>
          <ul>
            {memory.map((m, i) => (
              <li key={m.id || i}>{m.text}</li>
            ))}
          </ul>
        </section>
      ) : null}
      {said.length ? (
        <section>
          <h4>You said</h4>
          <ul>
            {said.map((m, i) => (
              <li key={i}>{m.text}</li>
            ))}
          </ul>
        </section>
      ) : null}
      {knowledge.length ? (
        <section>
          <h4>Topic knowledge</h4>
          <ul>
            {knowledge.map((k, i) => (
              <li key={i}>{k.title}</li>
            ))}
          </ul>
        </section>
      ) : null}
      {skills.length ? (
        <section>
          <h4>Skills</h4>
          <ul>
            {skills.map((k, i) => (
              <li key={i}>
                <span className="spx-receipt-name">{k.name}</span>
                {k.description ? ` · ${k.description}` : ""}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

/** A receipt answer with no `view` (an older server): the readable lines of
 *  its text only — no receipt line, no ids, no provenance braces. */
export function viewFromText(text: string): ReceiptView {
  const memory: { text: string }[] = [];
  for (const raw of (text || "").split("\n")) {
    const m = /^\s*-\s*(?:\[[^\]]*\]\s*)?(.+?)\s*(?:\{[^}]*\})?\s*$/.exec(raw);
    if (m && m[1]) memory.push({ text: m[1] });
  }
  return { memory };
}

export function TaskEventChip({
  ev,
  host,
  ended,
  reopen = reopenBreakout,
  records = listBreakouts,
  receipt = getReceipt,
  undoNote = deleteNote,
  useNow = confirmNote,
}: {
  ev: TaskEvent;
  host: ChipHost;
  /** `endedBreakouts` of the task's events. */
  ended?: ReadonlySet<string>;
  // Seams for tests.
  reopen?: typeof reopenBreakout;
  records?: typeof listBreakouts;
  receipt?: typeof getReceipt;
  undoNote?: typeof deleteNote;
  useNow?: typeof confirmNote;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [shown, setShown] = useState<ReceiptView | null>(null);
  const [gone, setGone] = useState(false);
  const self = useRef<HTMLDivElement>(null);
  const [undone, setUndone] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
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
      setShown(r.view && typeof r.view === "object" ? r.view : viewFromText(r.text || ""));
    } catch (err) {
      if (isSpecialistDeleted(err) || (err as { status?: number })?.status === 404) {
        setGone(true);
        setError(`The ${name || "specialist"} specialist was deleted, so its guidance can't be shown.`);
      } else setError(errorText(err));
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
      // The brief, for the thread's brief box (best effort: the seed's own
      // text is the fallback).
      const rec = await records(host.parentSessionId).then(
        (x) => x.breakouts?.find((b) => b.id === ev.breakout_id),
        () => undefined,
      );
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
        ...(rec?.brief ? { brief: rec.brief } : {}),
        ...(host.parentFolder ? { parentFolder: host.parentFolder } : {}),
        ...(chatAnchorOf(self.current) ? { anchor: chatAnchorOf(self.current)! } : {}),
      });
    } catch (err) {
      if (isSpecialistDeleted(err) || (err as { status?: number })?.status === 404) {
        setGone(true);
        setError(`The ${name || "specialist"} specialist was deleted. Its breakout can't be reopened.`);
      } else setError(errorText(err));
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

  const onUseNow = async () => {
    if (!ev.note_id) return;
    setBusy(true);
    setError("");
    try {
      await useNow(specId || "general", ev.note_id);
      setConfirmed(true);
      host.refresh();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  // A note the TASK says the user said waits for the user: Use now puts it in
  // effect; until then it is weighed by the nightly update like any other.
  const saidByUser = ev.kind === "note" && ev.basis === "user-said";
  const inEffect = confirmed || ev.in_effect === true;

  if (undone) {
    return (
      <div className="spx-chip is-quiet" role="status">
        <span>{isGeneralInbox(ev) ? "Note removed from the general inbox" : `Note removed from the ${name} specialist's inbox`}</span>
      </div>
    );
  }
  return (
    <div className={"spx-chip-wrap"} data-spx-event={ev.kind} ref={self}>
      <div className="spx-chip">
        <span className="spx-chip-dot" aria-hidden="true" />
        <span className="spx-chip-text">{chipText(saidByUser ? { ...ev, in_effect: inEffect } : ev)}</span>
        {ev.kind === "pull" && ev.receipt_id && !gone ? (
          <button type="button" className="spx-chip-btn" onClick={() => void onView()} disabled={busy} aria-expanded={shown !== null}>
            {shown !== null ? "Hide" : "View"}
          </button>
        ) : null}
        {(ev.kind === "breakout_closed" || (ev.kind === "breakout_open" && !ended?.has(ev.breakout_id || ""))) &&
        ev.breakout_id &&
        !gone ? (
          <button type="button" className="spx-chip-btn" onClick={() => void onReopen()} disabled={busy}>
            {ev.kind === "breakout_open" ? "Open" : "Reopen"}
          </button>
        ) : null}
        {saidByUser && inEffect ? <span className="spx-chip-said">It uses this from now on</span> : null}
        {saidByUser && !inEffect && ev.note_id ? (
          <button type="button" className="spx-chip-btn" onClick={() => void onUseNow()} disabled={busy}>
            Use now
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
      {shown !== null ? <ReceiptSheet view={shown} /> : null}
    </div>
  );
}

export function TaskEventChips({
  events,
  host,
  ended,
}: {
  events: readonly TaskEvent[];
  host: ChipHost;
  ended?: ReadonlySet<string>;
}) {
  if (!events.length) return null;
  return (
    <div className="spx-chips">
      {events.map((ev, i) => (
        <TaskEventChip
          key={ev.breakout_id ? `breakout:${ev.breakout_id}` : `${ev.kind}:${ev.at}:${ev.receipt_id || ev.note_id || i}`} ev={ev} host={host} {...(ended ? { ended } : {})} />
      ))}
    </div>
  );
}
