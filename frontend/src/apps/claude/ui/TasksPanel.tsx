// The run's background tasks — subagents, shells, monitors (SPEC T-5).
//
// A strip between the transcript and the composer (`.chat-tasks`), there
// whenever the run on screen has tasks, COLLAPSED by default to one line
// ("2 running (1 agent, 1 shell) · 1 done"). Opened, one row per task: kind
// icon, description (its tooltip names the subagent type and worktree), status
// pill, elapsed (live while running), and for a shell or a monitor an "Output"
// button that asks the live CLI for it (`task_output`).
//
// The words are `protocol/tasks.ts`'s; this file only lays them out. The
// open/closed bit is the host's (`ClaudeChat`), because `/tasks` toggles it.
import { Bot, ChevronRight, CircleDot, GitBranch, Radio, Terminal } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { cn } from "@platform/lib/utils";

import { runAgent } from "../protocol/agent";
import {
  elapsedLabel,
  hasOutput,
  isActive,
  isLive,
  kindLabel,
  sortTasks,
  statusLabel,
  summarizeTasks,
} from "../protocol/tasks";
import type { ErrorOnly, TaskKind, TaskOutputResponse, TaskRecord } from "../protocol/types";
import { CollapsiblePre } from "./CollapsiblePre";

import "../styles/tasks.css";

/**
 * WHAT AN OPEN PANEL BELONGS TO (review #5): the run and the conversation on
 * screen. Opened, the panel stays open for THIS scope only — a later run, a
 * restored session or a new chat starts collapsed again, rather than carrying
 * an empty "No background tasks" strip into every conversation after it.
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
  /** Test seam: the `task_output` call. */
  fetchOutput?: FetchTaskOutput;
  /** Test seam: the clock (ms). */
  now?: () => number;
}

/** One task's fetched output, as the row shows it. */
type OutputState =
  | { phase: "loading" }
  | { phase: "shown"; text: string; truncated: boolean }
  | { phase: "error"; message: string };

/** The title's tooltip: "Build fix — general-purpose · worktree". */
function rowTitle(t: TaskRecord): string {
  const base = t.description || kindLabel(t.kind);
  const extra = [t.subagent_type, t.isolation === "worktree" ? "worktree" : ""].filter(Boolean).join(" · ");
  return extra ? `${base} — ${extra}` : base;
}

function KindIcon({ kind }: { kind: TaskKind }) {
  const props = { size: 14, "aria-hidden": true } as const;
  if (kind === "agent") return <Bot {...props} />;
  if (kind === "shell") return <Terminal {...props} />;
  if (kind === "monitor") return <Radio {...props} />;
  return <CircleDot {...props} />;
}

export function TasksPanel({ tasks, runId, open, onToggle, fetchOutput = defaultFetch, now = Date.now }: TasksPanelProps) {
  const [outputs, setOutputs] = useState<Record<string, OutputState>>({});
  // Answers for a run that is no longer the panel's are dropped on arrival.
  const runRef = useRef(runId);
  runRef.current = runId;
  const [showEnded, setShowEnded] = useState(false);
  useEffect(() => {
    setOutputs({});
    setShowEnded(false);
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

  if (!open && !tasks.length) return null;
  const summary = summarizeTasks(tasks);
  const sorted = sortTasks(tasks);
  const rows = showEnded
    ? sorted
    : sorted.filter((t) => {
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
    <section className={cn("chat-tasks", open && "is-open")} aria-label="Background tasks">
      <button type="button" className="chat-tasks-head" aria-expanded={open} onClick={onToggle}>
        <ChevronRight className="chat-tasks-chev" size={14} aria-hidden="true" />
        <span className="chat-tasks-title">Background tasks</span>
        {summary ? <span className="chat-tasks-summary">{summary}</span> : null}
      </button>
      {open ? (
        <>
        {rows.length ? (
          <ul className="chat-tasks-list">
            {rows.map((t) => {
              const out = outputs[t.id];
              const elapsed = elapsedLabel(t, nowMs);
              return (
                <li key={t.id} className={cn("chat-task", "is-" + t.kind, isLive(t.status) ? "is-live" : "is-ended", t.status === "failed" && "is-failed")}>
                  <div className="chat-task-line">
                    <span className="chat-task-kind" title={kindLabel(t.kind)}>
                      <KindIcon kind={t.kind} />
                    </span>
                    <span className="chat-task-desc" title={rowTitle(t)}>
                      {t.description || kindLabel(t.kind)}
                    </span>
                    {t.isolation === "worktree" ? (
                      <GitBranch className="chat-task-wt" size={12} aria-label="in a worktree" />
                    ) : null}
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
                  </div>
                  {t.error ? (
                    <div className="chat-task-error is-clamped" title={t.error}>
                      {t.error}
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
          </ul>
        ) : (
          <div className="chat-tasks-empty">{hiddenCount ? "Nothing running." : "No background tasks in this run."}</div>
        )}
        {finishedToggle}
        </>
      ) : null}
    </section>
  );
}

export default TasksPanel;
