// The run's agents and shells — subagents, shells, monitors (SPEC T-5).
//
// A strip between the transcript and the composer (`.chat-tasks`), there
// whenever the run on screen has tasks, COLLAPSED by default to one line
// ("1 agent · 2 shells running · 1 failed"). Opened, one row per RUNNING (or
// paused) task, finished ones are not listed: kind icon, description (its
// tooltip names the subagent type and worktree), model (agents), elapsed (live),
// stop. The row itself is the toggle: a shell or a monitor opens its output
// (asked of the live CLI with `task_output`), an agent opens its latest update —
// or, when the host passes `onPeekAgent` (every chat outside an embed), the row opens the
// subagent's live transcript in a floating card instead and is no toggle.
//
// The words are `protocol/tasks.ts`'s; this file only lays them out. The
// open/closed bit is the host's (`ClaudeChat`), because `/tasks` toggles it.
import { Bot, ChevronRight, CircleDot, GitBranch, Radio, Square, Terminal } from "lucide-react";
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
  summarizeTasks,
} from "../protocol/tasks";
import type { ErrorOnly, TaskKind, TaskOutputResponse, TaskRecord, TaskStopResponse } from "../protocol/types";
import { CollapsiblePre } from "./CollapsiblePre";

import "../styles/tasks.css";

/**
 * THE SCOPE an explicit open belongs to: the run and the conversation on
 * screen. The open/closed preference itself is global and remembered
 * (`useTasksOpen`), but an EMPTY strip only shows in the scope the user opened
 * it in — see below.
 */
export function tasksPanelScope(s: {
  lastRunId: string | null;
  tasksRunId: string | null;
  sessionId: string | null;
  transcriptGen: number;
}): string {
  return [s.lastRunId ?? "", s.tasksRunId ?? "", s.sessionId ?? "", s.transcriptGen].join("\u0001");
}

const TASKS_OPEN_KEY = "fused-render.claude.tasks-open";
const TASKS_OPEN_EVENT = "fused-render:tasks-open";

function readTasksOpen(): boolean {
  try {
    return typeof localStorage !== "undefined" && localStorage.getItem(TASKS_OPEN_KEY) === "1";
  } catch {
    return false;
  }
}

function writeTasksOpen(open: boolean): void {
  try {
    localStorage.setItem(TASKS_OPEN_KEY, open ? "1" : "0");
  } catch {
    /* private mode / quota: the preference just does not persist */
  }
}

/** The panel's open bit: a GLOBAL preference, remembered across runs, sessions,
 *  new chats, panes and reloads (localStorage, synced live to other panes and
 *  tabs). Remembered "open" never carries an EMPTY "Nothing running." strip into a later run or chat: with no tasks it shows only when the
 *  user opened it (head click or `/tasks`) in this very `scope`
 *  (`tasksPanelScope`). Derived, so no effect races. */
export function useTasksOpen(scope: string, hasTasks: boolean): [boolean, () => void] {
  const [pref, setPref] = useState<boolean>(readTasksOpen);
  const [toggledIn, setToggledIn] = useState<string | null>(null);
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === TASKS_OPEN_KEY) setPref(e.newValue === "1");
    };
    const onLocal = (e: Event) => setPref(Boolean((e as CustomEvent<boolean>).detail));
    window.addEventListener("storage", onStorage);
    window.addEventListener(TASKS_OPEN_EVENT, onLocal);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(TASKS_OPEN_EVENT, onLocal);
    };
  }, []);
  const open = pref && (hasTasks || toggledIn === scope);
  const toggle = useCallback(() => {
    const next = !open;
    setPref(next);
    setToggledIn(scope);
    writeTasksOpen(next);
    window.dispatchEvent(new CustomEvent(TASKS_OPEN_EVENT, { detail: next }));
  }, [open, scope]);
  return [open, toggle];
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
   *  the rows' output toggle: nothing live to ask. */
  runId: string | null;
  /** The conversation on screen. History-only sessions all have a null runId,
   *  so the panel's per-session state (open rows, stop
   *  errors) resets on this as well. */
  sessionId?: string | null;
  /** Expanded. The host owns it (`/tasks` toggles it). */
  open: boolean;
  onToggle: () => void;
  /** The chat's own model (`opus`...): what an agent that named none inherits. */
  parentModel?: string;
  /** Open this subagent's transcript card. Given, an agent row with a
   *  `tool_use_id` calls it on press instead of toggling its update note. */
  onPeekAgent?: (t: TaskRecord) => void;
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

export function TasksPanel({ tasks, runId, sessionId = null, open, onToggle, parentModel, onPeekAgent, fetchOutput = defaultFetch, stopTask = defaultStop, now = Date.now }: TasksPanelProps) {
  const [outputs, setOutputs] = useState<Record<string, OutputState>>({});
  // Answers for a run that is no longer the panel's are dropped on arrival.
  const runRef = useRef(runId);
  runRef.current = runId;
  // Stop requests in flight and their refusals, by task.
  const [stopping, setStopping] = useState<Record<string, { phase: "pending" } | { phase: "error"; message: string }>>({});
  // Agent rows the user opened to their latest update.
  const [updOpen, setUpdOpen] = useState<ReadonlySet<string>>(new Set());
  useEffect(() => {
    setOutputs({});
    setStopping({});
    setUpdOpen(new Set());
    pollSeq.current.clear();
  }, [runId, sessionId]);

  // THE ELAPSED CLOCK TICKS ONLY WHILE SOMETHING IS LIVE AND THE ROWS SHOW: a
  // collapsed strip says no times, and an ended task's time is fixed.
  const anyLive = tasks.some((t) => isLive(t.status));
  const [, tick] = useState(0);
  useEffect(() => {
    if (!open || !anyLive) return;
    const timer = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [open, anyLive]);

  const nowMs = now();

  // A SHOWN OUTPUT STAYS FRESH: the CLI wants the host to poll `task_output`
  // while the task runs. A poll replaces the shown text in place (never back
  // to "Loading…") and a failed poll keeps the last good text.
  const outputsRef = useRef(outputs);
  outputsRef.current = outputs;
  // Per task: the newest request's sequence number and whether it is still out.
  // A forced read may overlap a poll; only the newest answer may land.
  const pollSeq = useRef<Map<string, { seq: number; inFlight: boolean }>>(new Map());
  const poll = useCallback(
    (taskId: string, force = false) => {
      const asked = runRef.current;
      const prev = pollSeq.current.get(taskId);
      if (!asked || (!force && prev?.inFlight)) return;
      const mine = (prev?.seq ?? 0) + 1;
      pollSeq.current.set(taskId, { seq: mine, inFlight: true });
      const land = (state: OutputState | null) => {
        const entry = pollSeq.current.get(taskId);
        if (!entry || entry.seq !== mine) return; // a newer request superseded this one
        entry.inFlight = false;
        if (!state || runRef.current !== asked) return;
        setOutputs((o) => {
          const cur = o[taskId];
          if (!cur) return o; // collapsed meanwhile
          if (state.phase === "error" && cur.phase === "shown") return o;
          return { ...o, [taskId]: state };
        });
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
    [fetchOutput],
  );
  const pollKey = tasks
    .filter((t) => isLive(t.status) && (outputs[t.id]?.phase === "shown" || outputs[t.id]?.phase === "error"))
    .map((t) => t.id)
    .sort()
    .join("\n");
  useEffect(() => {
    if (!open || !runId || !pollKey) return;
    const ids = pollKey.split("\n");
    const timer = window.setInterval(() => ids.forEach((id) => poll(id)), 2000);
    return () => window.clearInterval(timer);
  }, [open, runId, pollKey, poll]);
  // Live → ended while expanded: one last read (the CLI allows it).
  const wasLive = useRef<Set<string>>(new Set());
  useEffect(() => {
    const live = new Set(tasks.filter((t) => isLive(t.status)).map((t) => t.id));
    if (open && runId) {
      for (const t of tasks) {
        const o = outputsRef.current[t.id];
        if (!live.has(t.id) && wasLive.current.has(t.id) && o && o.phase !== "loading") poll(t.id, true);
      }
    }
    wasLive.current = live;
  }, [tasks, open, runId, poll]);


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
  const toggleUpdate = (taskId: string) =>
    setUpdOpen((s) => {
      const next = new Set(s);
      if (!next.delete(taskId)) next.add(taskId);
      return next;
    });

  const summary = summarizeTasks(tasks);
  if (!open && !summary) return null;
  const rows = sortTasks(tasks).filter((t) => isActive(t.status));

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
              const isAgent = t.kind === "agent";
              const canOut = hasOutput(t) && !!runId;
              const peeks = isAgent && !!t.tool_use_id && !!onPeekAgent;
              const toggles = canOut || isAgent;
              const expanded = canOut ? !!out : updOpen.has(t.id);
              const toggle = () => (peeks ? onPeekAgent(t) : canOut ? toggleOutput(t.id) : toggleUpdate(t.id));
              return (
                <li key={t.id} className={cn("chat-task", "is-" + t.kind, isLive(t.status) ? "is-live" : "is-paused")}>
                  <div
                    className="chat-task-line"
                    {...(toggles
                      ? {
                          role: "button",
                          tabIndex: 0,
                          ...(peeks ? {} : { "aria-expanded": expanded }),
                          onClick: toggle,
                          onKeyDown: (ev: React.KeyboardEvent) => {
                            if (ev.target !== ev.currentTarget) return;
                            if (ev.key === "Enter" || ev.key === " ") {
                              ev.preventDefault();
                              toggle();
                            }
                          },
                        }
                      : {})}
                  >
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
                    {elapsed ? <span className="chat-task-time">{elapsed}</span> : null}
                    {isLive(t.status) && runId ? (
                      <button
                        type="button"
                        className="chat-task-icon-btn"
                        aria-label="Stop task"
                        title="Stop"
                        disabled={stopping[t.id]?.phase === "pending"}
                        onClick={(ev) => {
                          ev.stopPropagation();
                          stop(t.id);
                        }}
                      >
                        <Square size={12} aria-hidden="true" />
                      </button>
                    ) : null}
                  </div>
                  {stopping[t.id]?.phase === "error" ? (
                    <div className="chat-task-error is-clamped" title={(stopping[t.id] as { message: string }).message}>
                      {(stopping[t.id] as { message: string }).message}
                    </div>
                  ) : null}
                  {isAgent && !canOut && !peeks && updOpen.has(t.id) ? (
                    <div className="chat-task-note">{t.summary || "No update yet."}</div>
                  ) : null}
                  {out?.phase === "loading" ? (
                    <div className="chat-task-note">Loading…</div>
                  ) : out?.phase === "shown" ? (
                    <div className="chat-task-out">
                      {out.truncated ? <div className="chat-task-note">Showing the end of the output.</div> : null}
                      {out.text ? (
                        <CollapsiblePre text={out.text} what="output" />
                      ) : (
                        <div className="chat-task-note">{isLive(t.status) ? "No output yet." : "No output."}</div>
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
          <div className="chat-tasks-empty">Nothing running.</div>
        )}
        </>
      ) : null}
    </section>
  );
}

export default TasksPanel;
