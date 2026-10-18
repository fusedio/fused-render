// The run's agents and shells — subagents, shells, monitors (SPEC T-5).
//
// A strip between the transcript and the composer (`.chat-tasks`), there
// whenever the run on screen has tasks, COLLAPSED by default to one line
// ("1 agent · 2 shells · 1 failed" while the turn is live, else "N finished"). Opened, one row per task: kind
// icon, description (its tooltip names the subagent type and worktree), status
// pill, elapsed (live while running), and for a shell or a monitor an "Output"
// button that asks the live CLI for it (`task_output`).
//
// The words are `protocol/tasks.ts`'s; this file only lays them out. The
// open/closed bit is the host's (`ClaudeChat`), because `/tasks` toggles it.
import { Bot, ChevronRight, CircleDot, GitBranch, Radio, Square, Terminal, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { cn } from "@platform/lib/utils";

import { runAgent } from "../protocol/agent";
import {
  agentModelLabel,
  elapsedLabel,
  hasOutput,
  isActive,
  isLive,
  kindLabel,
  sortTasks,
  statusLabel,
  summarizeTasks,
} from "../protocol/tasks";
import type { ErrorOnly, TaskKind, TaskOutputResponse, TaskRecord, TaskStopResponse } from "../protocol/types";
import { CollapsiblePre } from "./CollapsiblePre";

import "../styles/tasks.css";

/**
 * WHAT AN OPEN PANEL BELONGS TO (review #5): the run and the conversation on
 * screen. Opened, the panel stays open for THIS scope only — a later run, a
 * restored session or a new chat starts collapsed again, rather than carrying
 * an empty "No agents or shells" strip into every conversation after it.
 */
export function tasksPanelScope(s: {
  lastRunId: string | null;
  tasksRunId: string | null;
  sessionId: string | null;
  transcriptGen: number;
}): string {
  return [s.lastRunId ?? "", s.tasksRunId ?? "", s.sessionId ?? "", s.transcriptGen].join("\u0001");
}

/** The panel's open bit, scoped (`tasksPanelScope`): open only while the scope
 *  it was opened in is still the current one. Derived, so no effect races. */
export function useTasksOpen(scope: string): [boolean, () => void] {
  const [st, setSt] = useState<{ open: boolean; scope: string }>({ open: false, scope });
  const open = st.open && st.scope === scope;
  const toggle = useCallback(() => {
    setSt((prev) => ({ open: !(prev.open && prev.scope === scope), scope }));
  }, [scope]);
  return [open, toggle];
}

/** An ended row stays this long (ms) before it leaves the open list; "Show N
 *  finished" brings it back. */
export const TASK_ENDED_GRACE_MS = 5000;

/**
 * When (ms epoch) an ended row leaves the list; `null` for a row that does not
 * auto-hide yet. No `ended_at` = long ended.
 *
 * A FAILED ROW STAYS WHILE ITS TURN IS LIVE — the model may still be acting on
 * it — and then leaves on the same grace as the others, counted from whichever
 * came later: the failure, or the moment the turn ended (`endedAt`, 0 when
 * this panel never saw it end). Held means the row is THE LIVE RUN'S: the list
 * is owned by the run in flight (`owned`: `runId` is the live run) and the row
 * is not `carried`. Ownership, not clocks — the last run's rows are only
 * carried at the next run's first poll, so a send must not bring them back red
 * for that lap, while a reload mid-turn lands the live run's list from history
 * before the page goes live and must still hold its failures.
 * Not pinned past that (D1362 addendum): under a reply that says "done", a red
 * row that never leaves reads as "task broken".
 */
function hideAfterMs(t: TaskRecord, turnLive: boolean, owned: boolean, endedAt: number): number | null {
  if (isActive(t.status)) return null;
  const end = (Number(t.ended_at) || 0) * 1000;
  if (t.status === "failed" && !t.carried) {
    if (turnLive && owned) return null;
    const from = Math.max(end, turnLive ? 0 : endedAt);
    return from ? from + TASK_ENDED_GRACE_MS : -Infinity;
  }
  return end ? end + TASK_ENDED_GRACE_MS : -Infinity;
}

export type FetchTaskOutput = (runId: string, taskId: string) => Promise<TaskOutputResponse | ErrorOnly>;

export type StopTask = (runId: string, taskId: string) => Promise<TaskStopResponse | ErrorOnly>;

const defaultStop: StopTask = (runId, taskId) => runAgent("task_stop", { run_id: runId, task_id: taskId });

const defaultFetch: FetchTaskOutput = (runId, taskId) =>
  runAgent("task_output", { run_id: runId, task_id: taskId });

export interface TasksPanelProps {
  /** `ChatState.tasks` — any order; the panel sorts (live first). */
  tasks: readonly TaskRecord[];
  /** `ChatState.tasksRunId` — the run `task_output` is asked of. `null` hides
   *  the Output buttons: nothing live to ask (and so does a `carried` row). */
  runId: string | null;
  /** The chat's turn is in flight. A failed row is held, and the header says
   *  "N failed", only while it is (see `hideAfterMs`). Default true. */
  turnLive?: boolean;
  /** `ChatState.runId` — the run in flight. The list is the live run's only
   *  when `runId` is it; `undefined` (not told) counts as the live run's. */
  liveRunId?: string | null;
  /** The conversation on screen. History-only sessions all have a null runId,
   *  so the panel's per-session state (dismissed rows, Show finished, stop
   *  errors) resets on this as well. */
  sessionId?: string | null;
  /** Expanded. The host owns it (`/tasks` toggles it). */
  open: boolean;
  onToggle: () => void;
  /** The chat's own model (`opus`...): what an agent that named none inherits. */
  parentModel?: string;
  /** Test seam: the `task_output` call. */
  fetchOutput?: FetchTaskOutput;
  /** Test seam: the `task_stop` call. */
  stopTask?: StopTask;
  /** Test seam: the clock (ms). */
  now?: () => number;
}

/** One task's fetched output, as the row shows it. */
type OutputState =
  | { phase: "loading" }
  | { phase: "shown"; text: string; truncated: boolean }
  | { phase: "error"; message: string };

/** The title's tooltip: "Build fix — general-purpose · worktree". */
function rowTitle(t: TaskRecord, modelLabel = ""): string {
  const base = t.description || kindLabel(t.kind);
  const extra = [t.subagent_type, modelLabel, t.isolation === "worktree" ? "worktree" : ""].filter(Boolean).join(" · ");
  return extra ? `${base} — ${extra}` : base;
}

function KindIcon({ kind }: { kind: TaskKind }) {
  const props = { size: 14, "aria-hidden": true } as const;
  if (kind === "agent") return <Bot {...props} />;
  if (kind === "shell") return <Terminal {...props} />;
  if (kind === "monitor") return <Radio {...props} />;
  return <CircleDot {...props} />;
}

export function TasksPanel({ tasks, runId, turnLive = true, liveRunId, sessionId = null, open, onToggle, parentModel, fetchOutput = defaultFetch, stopTask = defaultStop, now = Date.now }: TasksPanelProps) {
  const [outputs, setOutputs] = useState<Record<string, OutputState>>({});
  // Answers for a run that is no longer the panel's are dropped on arrival.
  const runRef = useRef(runId);
  runRef.current = runId;
  const [showEnded, setShowEnded] = useState(false);
  // Rows the user dismissed (client-side only), per conversation; they count toward
  // "Show N finished". Stop requests in flight and their refusals, by task.
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(new Set());
  const [stopping, setStopping] = useState<Record<string, { phase: "pending" } | { phase: "error"; message: string }>>({});
  // Failed rows whose error line the user opened to its full text.
  const [errOpen, setErrOpen] = useState<ReadonlySet<string>>(new Set());
  // WHEN THE TURN LAST ENDED, as this panel saw it (0 = never saw it end):
  // where a failed row's grace starts. Derived during render, React's "adjust
  // state on a prop change" shape, so the hide is right on first paint.
  const [turnEdge, setTurnEdge] = useState({ live: turnLive, endedAt: 0 });
  if (turnEdge.live !== turnLive) setTurnEdge({ live: turnLive, endedAt: turnLive ? 0 : now() });
  const endedAt = turnEdge.live === turnLive ? turnEdge.endedAt : turnLive ? 0 : now();
  const owned = liveRunId === undefined || (!!runId && runId === liveRunId);
  useEffect(() => {
    setOutputs({});
    setShowEnded(false);
    setStopping({});
    setErrOpen(new Set());
  }, [runId, sessionId]);
  // Dismissals outlive a run handover — the rows do too (`carried`) — and go
  // with the conversation.
  useEffect(() => {
    setDismissed(new Set());
  }, [sessionId]);

  // THE ELAPSED CLOCK TICKS ONLY WHILE SOMETHING IS LIVE AND THE ROWS SHOW: a
  // collapsed strip says no times, and an ended task's time is fixed.
  const anyLive = tasks.some((t) => isLive(t.status));
  const [, tick] = useState(0);
  useEffect(() => {
    if (!open || !anyLive) return;
    const timer = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [open, anyLive]);

  // FINISHED ROWS LEAVE ON TIME, LIVE TASK OR NOT: one timeout for the next
  // row's hide moment, re-armed whenever it changes.
  const nowMs = now();
  let nextHide = Infinity;
  for (const t of tasks) {
    const at = hideAfterMs(t, turnLive, owned, endedAt);
    if (at != null && at >= nowMs && at - nowMs < nextHide) nextHide = at - nowMs;
  }
  const hideDelay = open && !showEnded && Number.isFinite(nextHide) ? nextHide + 1 : null;
  useEffect(() => {
    if (hideDelay == null) return;
    const timer = window.setTimeout(() => tick((n) => n + 1), hideDelay);
    return () => window.clearTimeout(timer);
  }, [hideDelay]);

  const toggleOutput = useCallback(
    (taskId: string) => {
      const current = outputs[taskId];
      if (current && current.phase !== "loading") {
        // Shown (or failed) → hide; the next press asks again, fresh.
        setOutputs((o) => {
          const next = { ...o };
          delete next[taskId];
          return next;
        });
        return;
      }
      if (current || !runId) return;
      const asked = runId;
      setOutputs((o) => ({ ...o, [taskId]: { phase: "loading" } }));
      const land = (state: OutputState) => {
        if (runRef.current !== asked) return;
        setOutputs((o) => ({ ...o, [taskId]: state }));
      };
      fetchOutput(asked, taskId).then(
        (res) => {
          if (!res || (typeof res.error === "string" && res.error)) {
            land({ phase: "error", message: (res && res.error) || "No output." });
            return;
          }
          const ok = res as TaskOutputResponse;
          land({ phase: "shown", text: typeof ok.output === "string" ? ok.output : "", truncated: !!ok.truncated });
        },
        (err: unknown) => land({ phase: "error", message: err instanceof Error ? err.message : String(err) }),
      );
    },
    [outputs, runId, fetchOutput],
  );

  const stop = (taskId: string) => {
    if (!runId || stopping[taskId]?.phase === "pending") return;
    const asked = runId;
    setStopping((m) => ({ ...m, [taskId]: { phase: "pending" } }));
    const land = (message: string) => {
      if (runRef.current !== asked) return;
      setStopping((m) => {
        const next = { ...m };
        if (message) next[taskId] = { phase: "error", message };
        else delete next[taskId];
        return next;
      });
    };
    stopTask(asked, taskId).then(
      (res) => land(res && typeof res.error === "string" && res.error ? res.error : ""),
      (err: unknown) => land(err instanceof Error ? err.message : String(err)),
    );
  };
  const toggleErr = (taskId: string) =>
    setErrOpen((s) => {
      const next = new Set(s);
      if (!next.delete(taskId)) next.add(taskId);
      return next;
    });
  const dismiss = (taskId: string) => setDismissed((s) => new Set(s).add(taskId));

  // NOTHING ACTIVE, BUT ROWS ON THE LIST (an ended turn, or the earlier runs a
  // "continue" carried over): the strip stays as a quiet "N finished", so the
  // rows are one click away rather than gone with the red word (D1362 addendum).
  // Dismissing every row takes the strip down with them.
  const kept = tasks.filter((t) => !dismissed.has(t.id)).length;
  // The header's "N failed" names exactly the failed rows being held open.
  const held = tasks.filter((t) => !dismissed.has(t.id) && (t.status !== "failed" || hideAfterMs(t, turnLive, owned, endedAt) == null));
  const summary = summarizeTasks(held, { live: turnLive }) || (kept ? `${kept} finished` : "");
  if (!open && !summary) return null;
  const sorted = sortTasks(tasks);
  const rows = showEnded
    ? sorted
    : sorted.filter((t) => {
        if (dismissed.has(t.id)) return false;
        const at = hideAfterMs(t, turnLive, owned, endedAt);
        return at == null || nowMs <= at;
      });
  const hiddenCount = sorted.length - rows.length;
  // A failure folded away is one click from sight: muted, on the toggle.
  const hiddenFailed = showEnded ? 0 : sorted.filter((t) => t.status === "failed" && !rows.includes(t)).length;
  const finishedToggle =
    hiddenCount > 0 || showEnded ? (
      <button type="button" className="chat-tasks-more" onClick={() => setShowEnded((v) => !v)}>
        {showEnded ? "Hide finished" : `Show ${hiddenCount} finished` + (hiddenFailed ? ` · ${hiddenFailed} failed` : "")}
      </button>
    ) : null;

  return (
    <section className={cn("chat-tasks", open && "is-open")} aria-label="Agents & shells">
      <button type="button" className="chat-tasks-head" aria-expanded={open} onClick={onToggle}>
        <ChevronRight className="chat-tasks-chev" size={14} aria-hidden="true" />
        {summary ? <span className="chat-tasks-summary">{summary}</span> : null}
      </button>
      {open ? (
        <>
        {rows.length ? (
          <ul className="chat-tasks-list">
            {rows.map((t) => {
              const out = outputs[t.id];
              const elapsed = elapsedLabel(t, nowMs);
              const modelLabel = t.kind === "agent" ? agentModelLabel(t, parentModel) : "";
              return (
                <li key={t.id} className={cn("chat-task", "is-" + t.kind, isLive(t.status) ? "is-live" : t.status === "paused" ? "is-paused" : "is-ended", t.status === "failed" && "is-failed")}>
                  <div className="chat-task-line">
                    <span className="chat-task-kind" title={kindLabel(t.kind)}>
                      <KindIcon kind={t.kind} />
                    </span>
                    <span className="chat-task-desc" title={rowTitle(t, modelLabel)}>
                      {t.description || kindLabel(t.kind)}
                    </span>
                    {t.isolation === "worktree" ? (
                      <GitBranch className="chat-task-wt" size={12} aria-label="in a worktree" />
                    ) : null}
                    {modelLabel ? <span className="chat-task-model">{modelLabel}</span> : null}
                    <span className={"chat-task-status is-" + t.status}>{statusLabel(t.status)}</span>
                    {elapsed ? <span className="chat-task-time">{elapsed}</span> : null}
                    {hasOutput(t) && runId && !t.carried ? (
                      <button
                        type="button"
                        className="chat-task-out-btn"
                        aria-expanded={!!out && out.phase !== "loading"}
                        disabled={out?.phase === "loading"}
                        onClick={() => toggleOutput(t.id)}
                      >
                        {out?.phase === "loading" ? "Loading…" : out ? "Hide output" : "Output"}
                      </button>
                    ) : null}
                    {isLive(t.status) && runId && !t.carried ? (
                      <button
                        type="button"
                        className="chat-task-icon-btn"
                        aria-label="Stop task"
                        title="Stop"
                        disabled={stopping[t.id]?.phase === "pending"}
                        onClick={() => stop(t.id)}
                      >
                        <Square size={12} aria-hidden="true" />
                      </button>
                    ) : null}
                    {!isActive(t.status) ? (
                      <button
                        type="button"
                        className="chat-task-icon-btn"
                        aria-label="Dismiss"
                        title="Dismiss"
                        onClick={() => dismiss(t.id)}
                      >
                        <X size={12} aria-hidden="true" />
                      </button>
                    ) : null}
                  </div>
                  {t.error ? (
                    <div
                      className={cn("chat-task-error", "is-toggle", !errOpen.has(t.id) && "is-clamped")}
                      title={t.error}
                      role="button"
                      tabIndex={0}
                      aria-expanded={errOpen.has(t.id)}
                      onClick={() => toggleErr(t.id)}
                      onKeyDown={(ev) => {
                        if (ev.key === "Enter" || ev.key === " ") {
                          ev.preventDefault();
                          toggleErr(t.id);
                        }
                      }}
                    >
                      {t.error}
                    </div>
                  ) : null}
                  {stopping[t.id]?.phase === "error" ? (
                    <div className="chat-task-error is-clamped" title={(stopping[t.id] as { message: string }).message}>
                      {(stopping[t.id] as { message: string }).message}
                    </div>
                  ) : null}
                  {out?.phase === "shown" ? (
                    <div className="chat-task-out">
                      {out.truncated ? <div className="chat-task-note">Showing the last 64 KB.</div> : null}
                      {out.text ? (
                        <CollapsiblePre text={out.text} what="output" />
                      ) : (
                        <div className="chat-task-note">No output yet.</div>
                      )}
                    </div>
                  ) : out?.phase === "error" ? (
                    <div className="chat-task-error">{out.message}</div>
                  ) : null}
                </li>
              );
            })}
            {finishedToggle ? <li className="chat-tasks-more-row">{finishedToggle}</li> : null}
          </ul>
        ) : (
          <div className="chat-tasks-empty">
            {hiddenCount ? "Nothing running." : "No agents or shells in this run."}
            {finishedToggle ? <div className="chat-tasks-more-row">{finishedToggle}</div> : null}
          </div>
        )}
        </>
      ) : null}
    </section>
  );
}

export default TasksPanel;
