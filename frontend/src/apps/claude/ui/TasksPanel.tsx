// The run's agents and shells — subagents, shells, monitors (SPEC T-5).
//
// A strip between the transcript and the composer (`.chat-tasks`), there
// whenever the run on screen has tasks, COLLAPSED by default to one line
// ("1 agent · 2 shells running · 1 failed"). Opened, one row per task: kind
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

/** An ended, non-failed row stays this long (ms) before it leaves the open
 *  list; "Show N finished" brings it back. */
export const TASK_ENDED_GRACE_MS = 5000;

/** When (ms epoch) an ended, non-failed row leaves the list; `null` for a row
 *  that never auto-hides (live, paused, failed). No `ended_at` = long ended. */
function hideAfterMs(t: TaskRecord): number | null {
  if (isActive(t.status) || t.status === "failed") return null;
  const end = Number(t.ended_at) || 0;
  return end ? end * 1000 + TASK_ENDED_GRACE_MS : -Infinity;
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
   *  the Output buttons: nothing live to ask. */
  runId: string | null;
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

export function TasksPanel({ tasks, runId, open, onToggle, parentModel, fetchOutput = defaultFetch, stopTask = defaultStop, now = Date.now }: TasksPanelProps) {
  const [outputs, setOutputs] = useState<Record<string, OutputState>>({});
  // Answers for a run that is no longer the panel's are dropped on arrival.
  const runRef = useRef(runId);
  runRef.current = runId;
  const [showEnded, setShowEnded] = useState(false);
  // Rows the user dismissed (client-side only), per run; they count toward
  // "Show N finished". Stop requests in flight and their refusals, by task.
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(new Set());
  const [stopping, setStopping] = useState<Record<string, { phase: "pending" } | { phase: "error"; message: string }>>({});
  // Failed rows whose error line the user opened to its full text.
  const [errOpen, setErrOpen] = useState<ReadonlySet<string>>(new Set());
  useEffect(() => {
    setOutputs({});
    setShowEnded(false);
    setDismissed(new Set());
    setStopping({});
    setErrOpen(new Set());
  }, [runId]);

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
    const at = hideAfterMs(t);
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

  if (!open && !tasks.length) return null;
  const summary = summarizeTasks(tasks);
  const sorted = sortTasks(tasks);
  const rows = showEnded
    ? sorted
    : sorted.filter((t) => {
        if (dismissed.has(t.id)) return false;
        const at = hideAfterMs(t);
        return at == null || nowMs <= at;
      });
  const hiddenCount = sorted.length - rows.length;
  const finishedToggle =
    hiddenCount > 0 || showEnded ? (
      <button type="button" className="chat-tasks-more" onClick={() => setShowEnded((v) => !v)}>
        {showEnded ? "Hide finished" : `Show ${hiddenCount} finished`}
      </button>
    ) : null;

  return (
    <section className={cn("chat-tasks", open && "is-open")} aria-label="Agents & shells">
      <button type="button" className="chat-tasks-head" aria-expanded={open} onClick={onToggle}>
        <ChevronRight className="chat-tasks-chev" size={14} aria-hidden="true" />
        <span className="chat-tasks-title">Agents &amp; shells</span>
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
                <li key={t.id} className={cn("chat-task", "is-" + t.kind, isLive(t.status) ? "is-live" : "is-ended", t.status === "failed" && "is-failed")}>
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
                    {hasOutput(t) && runId ? (
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
                    {isLive(t.status) && runId ? (
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
