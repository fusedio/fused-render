// The run's background tasks — subagents, shells, monitors (SPEC T-5).
//
// A strip between the transcript and the composer (`.chat-tasks`), there
// whenever the run on screen has tasks, COLLAPSED by default to one line
// ("2 running (1 agent, 1 shell) · 1 done"). Opened, one row per task: kind
// icon, description, status pill, elapsed (live while running), usage for an
// agent, the events count for a monitor, the last summary, and for a shell or a
// monitor an "Output" button that asks the live CLI for it (`task_output`).
//
// The words are `protocol/tasks.ts`'s; this file only lays them out. The
// open/closed bit is the host's (`ClaudeChat`), because `/tasks` toggles it.
import { Bot, ChevronRight, CircleDot, Radio, Terminal } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { cn } from "@platform/lib/utils";

import { runAgent } from "../protocol/agent";
import {
  elapsedLabel,
  eventsLabel,
  hasOutput,
  isLive,
  kindLabel,
  sortTasks,
  statusLabel,
  summarizeTasks,
  usageLabel,
} from "../protocol/tasks";
import type { ErrorOnly, TaskKind, TaskOutputResponse, TaskRecord } from "../protocol/types";
import { CollapsiblePre } from "./CollapsiblePre";

import "../styles/tasks.css";

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
  useEffect(() => setOutputs({}), [runId]);

  // THE ELAPSED CLOCK TICKS ONLY WHILE SOMETHING IS LIVE AND THE ROWS SHOW: a
  // collapsed strip says no times, and an ended task's time is fixed.
  const anyLive = tasks.some((t) => isLive(t.status));
  const [, tick] = useState(0);
  useEffect(() => {
    if (!open || !anyLive) return;
    const timer = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [open, anyLive]);

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
  const rows = sortTasks(tasks);
  const nowMs = now();

  return (
    <section className={cn("chat-tasks", open && "is-open")} aria-label="Background tasks">
      <button type="button" className="chat-tasks-head" aria-expanded={open} onClick={onToggle}>
        <ChevronRight className="chat-tasks-chev" size={14} aria-hidden="true" />
        <span className="chat-tasks-title">Background tasks</span>
        {summary ? <span className="chat-tasks-summary">{summary}</span> : null}
      </button>
      {open ? (
        rows.length ? (
          <ul className="chat-tasks-list">
            {rows.map((t) => {
              const meta =
                t.kind === "agent" ? usageLabel(t.usage) : t.kind === "monitor" ? eventsLabel(t.events) : "";
              const out = outputs[t.id];
              const elapsed = elapsedLabel(t, nowMs);
              return (
                <li key={t.id} className={cn("chat-task", "is-" + t.kind, isLive(t.status) ? "is-live" : "is-ended")}>
                  <div className="chat-task-line">
                    <span className="chat-task-kind" title={kindLabel(t.kind)}>
                      <KindIcon kind={t.kind} />
                    </span>
                    <span className="chat-task-desc" title={t.description || kindLabel(t.kind)}>
                      {t.description || kindLabel(t.kind)}
                    </span>
                    <span className={"chat-task-status is-" + t.status}>{statusLabel(t.status)}</span>
                    {elapsed ? <span className="chat-task-time">{elapsed}</span> : null}
                    {meta ? <span className="chat-task-meta">{meta}</span> : null}
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
                  {t.subagent_type ? <div className="chat-task-sub">{t.subagent_type}</div> : null}
                  {t.summary ? <div className="chat-task-summary">{t.summary}</div> : null}
                  {t.error ? <div className="chat-task-error">{t.error}</div> : null}
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
          <div className="chat-tasks-empty">No background tasks in this run.</div>
        )
      ) : null}
    </section>
  );
}

export default TasksPanel;
