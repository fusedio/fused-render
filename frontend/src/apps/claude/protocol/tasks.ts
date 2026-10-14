// Background tasks — subagents, shells, monitors (SPEC T-5). Pure: the words
// the tasks panel, the working line and the agent tool chip say about the
// poll's `tasks` registry (agent.py, SPEC T-1). No React, no DOM.
import type { TaskKind, TaskRecord, TaskStatus, TaskUsage, ToolSegment } from "./types";

/** A task that is still MOVING: counted as "running", its clock ticks, a
 *  Monitor says "watching". `paused` is not — it is held, not working. */
export function isLive(status: TaskStatus | string): boolean {
  return status === "running" || status === "pending";
}

/** Not finished: live or paused. Sorted above the ended rows, and what a run
 *  that ends downgrades to `unknown` (`endActiveTasks`). */
export function isActive(status: TaskStatus | string): boolean {
  return isLive(status) || status === "paused";
}

/**
 * THE OWNING RUN IS GONE: every still-active record becomes `unknown` with no
 * end time — its real outcome was never reported, and a dead host can neither
 * be watched nor asked for output. `null` when nothing changes.
 */
export function endActiveTasks(tasks: readonly TaskRecord[]): TaskRecord[] | null {
  if (!tasks.some((t) => isActive(t.status))) return null;
  return tasks.map((t) => (isActive(t.status) ? { ...t, status: "unknown", ended_at: null } : t));
}

/** The summary's and the panel's kind order (SPEC T-5: "1 agent, 1 shell, 1 monitor"). */
const SUMMARY_KINDS: TaskKind[] = ["agent", "shell", "monitor", "other"];

const KIND_NOUN: Record<TaskKind, [string, string]> = {
  agent: ["agent", "agents"],
  shell: ["shell", "shells"],
  monitor: ["monitor", "monitors"],
  other: ["task", "tasks"],
};

function asKind(kind: unknown): TaskKind {
  return kind === "agent" || kind === "shell" || kind === "monitor" ? kind : "other";
}

function plural(n: number, [one, many]: [string, string]): string {
  return n + " " + (n === 1 ? one : many);
}

/** The row's kind word ("Subagent", "Shell", "Monitor", "Task"). */
export function kindLabel(kind: TaskKind | string): string {
  switch (asKind(kind)) {
    case "agent":
      return "Subagent";
    case "shell":
      return "Shell";
    case "monitor":
      return "Monitor";
    default:
      return "Task";
  }
}

/** The status pill's word. `killed` is something the reader (or Claude) did,
 *  so it says "stopped" rather than a word that sounds like a crash. */
export function statusLabel(status: TaskStatus | string): string {
  switch (status) {
    case "running":
      return "running";
    case "pending":
      return "starting";
    case "paused":
      return "paused";
    case "completed":
      return "done";
    case "failed":
      return "failed";
    case "killed":
      return "stopped";
    case "unknown":
      return "ended";
    default:
      return String(status || "running");
  }
}

/**
 * The collapsed panel's one line: "2 running (1 agent, 1 shell) · 1 done".
 * Paused tasks are their own count. Ended tasks are counted by outcome ("done",
 * "failed", "stopped", and "ended" for an `unknown` one whose run died) so a
 * failure is never folded into a reassuring "done". "" for an empty list.
 */
export function summarizeTasks(tasks: readonly TaskRecord[]): string {
  const live = tasks.filter((t) => isLive(t.status));
  const parts: string[] = [];
  if (live.length) {
    const kinds = SUMMARY_KINDS.map((k) => {
      const n = live.filter((t) => asKind(t.kind) === k).length;
      return n ? plural(n, KIND_NOUN[k]) : "";
    }).filter(Boolean);
    parts.push(live.length + " running (" + kinds.join(", ") + ")");
  }
  const ended = (status: TaskStatus, word: string) => {
    const n = tasks.filter((t) => t.status === status).length;
    if (n) parts.push(n + " " + word);
  };
  ended("paused", "paused");
  ended("completed", "done");
  ended("failed", "failed");
  ended("killed", "stopped");
  ended("unknown", "ended");
  return parts.join(" · ");
}

/** Active (live or paused) first, then ended; each group by `started_at`, ties kept in arrival
 *  order. A new array — the input is the published state. */
export function sortTasks(tasks: readonly TaskRecord[]): TaskRecord[] {
  return tasks
    .map((t, i) => ({ t, i }))
    .sort((a, b) => {
      const la = isActive(a.t.status) ? 0 : 1;
      const lb = isActive(b.t.status) ? 0 : 1;
      if (la !== lb) return la - lb;
      const sa = Number(a.t.started_at) || 0;
      const sb = Number(b.t.started_at) || 0;
      if (sa !== sb) return sa - sb;
      return a.i - b.i;
    })
    .map(({ t }) => t);
}

/** "12s", "3m 5s", "1h 2m". */
export function durationWords(secs: number): string {
  const s = Math.max(0, Math.floor(secs));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
}

/** How long the task has been running (live, against `nowMs`) or ran (ended,
 *  to `ended_at`). `started_at`/`ended_at` are epoch SECONDS; `nowMs` is
 *  `Date.now()`'s unit. "" when the record carries no start, or is not live
 *  and has no end (paused, `unknown`) — the clock is never run against now
 *  for a task that is not moving. */
export function elapsedLabel(rec: Pick<TaskRecord, "started_at" | "ended_at" | "status">, nowMs: number): string {
  const start = Number(rec.started_at) || 0;
  if (!start) return "";
  if (isLive(rec.status)) return durationWords(nowMs / 1000 - start);
  const end = rec.ended_at == null ? 0 : Number(rec.ended_at) || 0;
  return end ? durationWords(end - start) : "";
}

/** "12.3k tokens · 4 tool uses"; "" when there is no usage yet. */
export function usageLabel(usage: TaskUsage | null | undefined): string {
  if (!usage) return "";
  const parts: string[] = [];
  const tok = Number(usage.total_tokens) || 0;
  if (tok) parts.push((tok >= 1000 ? (tok / 1000).toFixed(tok >= 100000 ? 0 : 1) + "k" : String(tok)) + " tokens");
  const uses = Number(usage.tool_uses) || 0;
  if (uses) parts.push(plural(uses, ["tool use", "tool uses"]));
  return parts.join(" · ");
}

/** "3 events" for a monitor; "" for none. */
export function eventsLabel(events: number | null | undefined): string {
  const n = Number(events) || 0;
  return n ? plural(n, ["event", "events"]) : "";
}

/** Shell and monitor tasks have output the CLI can hand back (`get_task_output`)
 *  — not once their run has died (`unknown`): there is no host to ask. */
export function hasOutput(rec: Pick<TaskRecord, "kind"> & Partial<Pick<TaskRecord, "status">>): boolean {
  return (rec.kind === "shell" || rec.kind === "monitor") && rec.status !== "unknown";
}

const MODEL_FAMILIES = ["sonnet", "opus", "haiku", "fable"];

/** "sonnet" / "claude-sonnet-5-5" -> "Sonnet"; anything else verbatim. */
export function modelName(model: string | null | undefined): string {
  const m = (model || "").trim();
  if (!m) return "";
  const lower = m.toLowerCase();
  const fam = MODEL_FAMILIES.find(
    (f) => lower === f || lower.startsWith(f + "-") || lower.startsWith("claude-" + f) || lower.includes("-" + f + "-") || lower.endsWith("-" + f),
  );
  return fam ? fam[0].toUpperCase() + fam.slice(1) : m;
}

/** "Sonnet · medium" for what an Agent call stated; "" when it stated neither
 *  (the subagent then inherits, which is not worth saying on the working line). */
export function explicitModelLabel(model?: string | null, effort?: string | null): string {
  const name = modelName(model);
  const e = (effort || "").trim();
  return [name, e].filter(Boolean).join(" · ");
}

/** The row's model words: the explicit label, or the parent's model with
 *  " (inherited)" when the call set none. "" when nothing is known. */
export function agentModelLabel(rec: Pick<TaskRecord, "model" | "effort">, parentModel?: string | null): string {
  const name = modelName(rec.model);
  const e = (rec.effort || "").trim();
  if (name) return e ? name + " · " + e : name;
  const parent = modelName(parentModel);
  if (!parent) return e;
  return parent + " (inherited)" + (e ? " · " + e : "");
}

/**
 * The working line's verb while background work is live (SPEC T-5): agents
 * first, then monitors, then shells. "" when nothing it can name is running.
 */
export function taskVerb(tasks: readonly TaskRecord[] | null | undefined): string {
  const live = (tasks ?? []).filter((t) => t.status === "running" || t.status === "pending");
  const n = (k: TaskKind) => live.filter((t) => asKind(t.kind) === k).length;
  const liveAgents = live.filter((t) => asKind(t.kind) === "agent");
  const agents = liveAgents.length;
  if (agents) {
    const groups = new Map<string, number>();
    let untyped = 0;
    for (const t of liveAgents) {
      const ty = (t.subagent_type || "").trim();
      if (ty) groups.set(ty, (groups.get(ty) ?? 0) + 1);
      else untyped++;
    }
    if (agents === 1) {
      const ml = explicitModelLabel(liveAgents[0].model, liveAgents[0].effort);
      const tail = ml ? " (" + ml + ")" : "";
      return (groups.size ? "Running " + [...groups.keys()][0] + " subagent" : "Running a subagent") + tail;
    }
    let out = "Running " + agents + " subagents";
    if (groups.size) {
      const parts = [...groups.entries()].sort((a, b) => b[1] - a[1]).map(([ty, c]) => c + " " + ty);
      if (untyped) parts.push(untyped + " other");
      out += ": " + parts.join(", ");
    }
    return out;
  }
  const monitors = n("monitor");
  if (monitors) return monitors === 1 ? "Watching a monitor" : "Watching " + monitors + " monitors";
  const shells = n("shell");
  if (shells) return shells === 1 ? "Running a background command" : "Running " + shells + " background commands";
  return "";
}

/** id → record, for the chips that link to one by `task_id`. */
export function taskIndex(tasks: readonly TaskRecord[] | null | undefined): Map<string, TaskRecord> {
  const out = new Map<string, TaskRecord>();
  for (const t of tasks ?? []) if (t && t.id) out.set(t.id, t);
  return out;
}

/** The record a tool segment started: by `task_id` (the registry's own link),
 *  else by `tool_use_id` against the segment's id. */
export function taskFor(
  seg: Pick<ToolSegment, "id" | "task_id">,
  index: ReadonlyMap<string, TaskRecord> | null | undefined,
): TaskRecord | null {
  if (!index || !index.size) return null;
  if (seg.task_id && index.has(seg.task_id)) return index.get(seg.task_id)!;
  if (seg.id) for (const t of index.values()) if (t.tool_use_id === seg.id) return t;
  return null;
}

/** Tools that start a subagent. `Task` is the CLI's older name for `Agent`. */
export function isAgentTool(name: string): boolean {
  return name === "Agent" || name === "Task";
}

export interface SubagentChipModel {
  /** The chip opens to the subagent's own transcript. */
  expandable: boolean;
  /** How many segments the subagent's transcript holds. */
  childCount: number;
  /** The header's name: the subagent type, or "Subagent (started earlier)" for
   *  an orphan parent. "" for a tool that is neither. */
  title: string;
  /** The linked task's status word, "" when no record is linked. */
  status: string;
  /** Raw status, for the pill's class. */
  statusKey: string;
  /** Tokens · tool uses, "" when none. */
  usage: string;
  /** A Monitor's "watching · 3 events" / "3 events". */
  monitor: string;
}

/**
 * What the tool chip's header says about the background work behind it (SPEC
 * T-5): a subagent's type, live status and usage; an orphan's "Subagent
 * (started earlier)"; a Monitor's "watching" plus its events count. Pure, so
 * the expand model is tested without a renderer.
 */
export function subagentChipModel(
  seg: Pick<ToolSegment, "name" | "input" | "children" | "orphan">,
  rec: TaskRecord | null,
): SubagentChipModel {
  const childCount = Array.isArray(seg.children) ? seg.children.length : 0;
  const inp = seg.input && typeof seg.input === "object" ? (seg.input as Record<string, unknown>) : {};
  const agent = isAgentTool(String(seg.name)) || !!seg.orphan || rec?.kind === "agent";
  let title = "";
  if (seg.orphan) title = "Subagent (started earlier)";
  else if (agent) {
    const type = rec?.subagent_type || (typeof inp.subagent_type === "string" ? inp.subagent_type : "");
    title = type ? "Subagent: " + type : childCount ? "Subagent" : "";
  }
  let monitor = "";
  if (seg.name === "Monitor" || rec?.kind === "monitor") {
    const ev = eventsLabel(rec?.events);
    const watching = !!rec && isLive(rec.status);
    monitor = [watching ? "watching" : "", ev].filter(Boolean).join(" · ");
  }
  return {
    expandable: childCount > 0,
    childCount,
    title,
    status: rec ? statusLabel(rec.status) : "",
    statusKey: rec ? String(rec.status) : "",
    usage: agent && rec ? usageLabel(rec.usage) : "",
    monitor,
  };
}
