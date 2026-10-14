import { describe, expect, test } from "bun:test";

import {
  agentModelLabel,
  durationWords,
  elapsedLabel,
  endActiveTasks,
  hasOutput,
  isLive,
  statusLabel,
  kindLabel,
  sortTasks,
  subagentChipModel,
  summarizeTasks,
  taskFor,
  taskIndex,
  taskVerb,
  usageLabel,
} from "./tasks";
import type { Segment, TaskRecord, ToolSegment } from "./types";

function rec(over: Partial<TaskRecord>): TaskRecord {
  return {
    id: "t",
    description: "",
    kind: "other",
    type: "",
    subagent_type: "",
    isolation: "",
    tool_use_id: "",
    status: "running",
    started_at: 100,
    ended_at: null,
    backgrounded: false,
    usage: null,
    error: "",
    summary: "",
    events: 0,
    ...over,
  };
}

function tool(over: Partial<ToolSegment>): ToolSegment {
  return { kind: "tool", id: "tu1", name: "Agent", input: {}, status: "running", output: null, images: [], ...over };
}

describe("summarizeTasks", () => {
  test("running by kind; done is not counted", () => {
    const line = summarizeTasks([
      rec({ id: "a", kind: "agent" }),
      rec({ id: "b", kind: "shell" }),
      rec({ id: "b2", kind: "shell" }),
      rec({ id: "c", kind: "monitor" }),
      rec({ id: "d", kind: "shell", status: "completed" }),
    ]);
    expect(line).toBe("1 agent · 2 shells · 1 monitor running");
  });
  test("one kind running; failures stay, done and stopped are dropped", () => {
    const line = summarizeTasks([
      rec({ id: "a", kind: "agent" }),
      rec({ id: "b", kind: "agent", status: "pending" }),
      rec({ id: "c", kind: "shell", status: "failed" }),
      rec({ id: "d", kind: "shell", status: "killed" }),
      rec({ id: "e", kind: "agent", status: "completed" }),
      rec({ id: "f", kind: "agent", status: "completed" }),
    ]);
    expect(line).toBe("2 agents running · 1 failed");
    expect(summarizeTasks([rec({ kind: "shell" }), rec({ id: "s2", kind: "shell" })])).toBe("2 shells running");
  });
  test("nothing running", () => {
    expect(summarizeTasks([rec({ status: "completed" })])).toBe("");
    expect(summarizeTasks([rec({ status: "killed" }), rec({ id: "f", status: "failed" })])).toBe("1 failed");
    expect(summarizeTasks([])).toBe("");
  });
});

describe("sortTasks", () => {
  test("live above ended, each by started_at, ties stable", () => {
    const out = sortTasks([
      rec({ id: "ended-early", status: "completed", started_at: 1 }),
      rec({ id: "run-late", started_at: 50 }),
      rec({ id: "paused", status: "paused", started_at: 10 }),
      rec({ id: "run-tie-1", started_at: 20 }),
      rec({ id: "run-tie-2", started_at: 20 }),
      rec({ id: "failed", status: "failed", started_at: 0 }),
    ]);
    expect(out.map((t) => t.id)).toEqual(["paused", "run-tie-1", "run-tie-2", "run-late", "failed", "ended-early"]);
  });
  test("agents, then shells, then other kinds; started_at within a kind", () => {
    const out = sortTasks([
      rec({ id: "other", kind: "other", started_at: 1 }),
      rec({ id: "shell-1", kind: "shell", started_at: 2 }),
      rec({ id: "mon", kind: "monitor", started_at: 3 }),
      rec({ id: "agent-late", kind: "agent", started_at: 9 }),
      rec({ id: "agent-early", kind: "agent", started_at: 4 }),
      rec({ id: "shell-0", kind: "shell", started_at: 1 }),
    ]);
    expect(out.map((t) => t.id)).toEqual(["agent-early", "agent-late", "shell-0", "shell-1", "mon", "other"]);
  });
  test("does not mutate the input", () => {
    const input = [rec({ id: "x", status: "completed" }), rec({ id: "y" })];
    sortTasks(input);
    expect(input.map((t) => t.id)).toEqual(["x", "y"]);
  });
});

describe("elapsed / labels", () => {
  test("durationWords", () => {
    expect(durationWords(0)).toBe("0s");
    expect(durationWords(59.9)).toBe("59s");
    expect(durationWords(185)).toBe("3m 5s");
    expect(durationWords(3720)).toBe("1h 2m");
  });
  test("live against now, ended against ended_at", () => {
    expect(elapsedLabel(rec({ started_at: 100 }), 112_000)).toBe("12s");
    expect(elapsedLabel(rec({ started_at: 100, ended_at: 130, status: "completed" }), 999_000)).toBe("30s");
    expect(elapsedLabel(rec({ started_at: 0 }), 5000)).toBe("");
  });
  test("kindLabel / usageLabel", () => {
    expect(kindLabel("agent")).toBe("Subagent");
    expect(kindLabel("shell")).toBe("Shell");
    expect(kindLabel("monitor")).toBe("Monitor");
    expect(kindLabel("weird")).toBe("Task");
    expect(usageLabel({ total_tokens: 12345, tool_uses: 4, duration_ms: 1 })).toBe("12.3k tokens · 4 tool uses");
    expect(usageLabel({ total_tokens: 800, tool_uses: 1, duration_ms: 1 })).toBe("800 tokens · 1 tool use");
    expect(usageLabel(null)).toBe("");
  });
});

describe("agentModelLabel", () => {
  test("short names for aliases and full ids; effort appended", () => {
    expect(agentModelLabel({ model: "sonnet", effort: "medium" })).toBe("Sonnet · medium");
    expect(agentModelLabel({ model: "claude-sonnet-5-5", effort: "" })).toBe("Sonnet");
    expect(agentModelLabel({ model: "claude-opus-4-1" })).toBe("Opus");
    expect(agentModelLabel({ model: "haiku" })).toBe("Haiku");
    expect(agentModelLabel({ model: "fable", effort: "max" })).toBe("Fable · max");
    expect(agentModelLabel({ model: "gpt-x" })).toBe("gpt-x");
  });
  test("no model: the parent's, marked inherited", () => {
    expect(agentModelLabel({ model: "", effort: "" }, "opus")).toBe("Opus (inherited)");
    expect(agentModelLabel({ model: "", effort: "high" }, "opus")).toBe("Opus (inherited) · high");
    expect(agentModelLabel({ model: "" }, "")).toBe("");
    expect(agentModelLabel({}, undefined)).toBe("");
  });
  test("taskVerb names an explicit model for one agent only", () => {
    const a = rec({ id: "a", kind: "agent", subagent_type: "Explore", model: "sonnet", effort: "medium" });
    expect(taskVerb([a])).toBe("Running Explore subagent (Sonnet · medium)");
    expect(taskVerb([rec({ id: "b", kind: "agent" })])).toBe("Running a subagent");
    expect(taskVerb([a, rec({ id: "c", kind: "agent", model: "haiku" })])).toBe("Running 2 subagents: 1 Explore, 1 other");
  });
});

describe("taskVerb", () => {
  test("agents, then monitors, then shells", () => {
    const shell = rec({ id: "s", kind: "shell" });
    const mon = rec({ id: "m", kind: "monitor" });
    const agent = rec({ id: "a", kind: "agent" });
    expect(taskVerb([shell, mon, agent])).toBe("Running a subagent");
    expect(taskVerb([agent, rec({ id: "a2", kind: "agent" })])).toBe("Running 2 subagents");
    expect(taskVerb([shell, mon])).toBe("Watching a monitor");
    expect(taskVerb([shell])).toBe("Running a background command");
    expect(taskVerb([shell, rec({ id: "s2", kind: "shell" })])).toBe("Running 2 background commands");
  });
  test("typed subagents are named, grouped by type", () => {
    const a = (id: string, t = "") => rec({ id, kind: "agent", subagent_type: t });
    expect(taskVerb([a("1", "Explore")])).toBe("Running Explore subagent");
    expect(taskVerb([a("1", "Explore"), a("2", "Explore")])).toBe("Running 2 subagents: 2 Explore");
    expect(taskVerb([a("1", "Explore"), a("2", "general-purpose"), a("3", "Explore")])).toBe(
      "Running 3 subagents: 2 Explore, 1 general-purpose",
    );
    expect(taskVerb([a("1", "Explore"), a("2")])).toBe("Running 2 subagents: 1 Explore, 1 other");
    expect(taskVerb([a("1"), a("2")])).toBe("Running 2 subagents");
  });
  test("ended and unnamed tasks say nothing", () => {
    expect(taskVerb([rec({ kind: "agent", status: "completed" })])).toBe("");
    expect(taskVerb([rec({ kind: "other" })])).toBe("");
    expect(taskVerb(undefined)).toBe("");
  });
});

describe("chip expand model", () => {
  const child: Segment = { kind: "text", text: "hi" };
  test("a tool with children is expandable and names the subagent type", () => {
    const r = rec({ id: "T1", kind: "agent", subagent_type: "Explore", usage: { total_tokens: 2000, tool_uses: 3, duration_ms: 0 } });
    const seg = tool({ task_id: "T1", children: [child, child] });
    const m = subagentChipModel(seg, taskFor(seg, taskIndex([r])));
    expect(m).toEqual({
      expandable: true,
      childCount: 2,
      title: "Subagent: Explore",
      status: "running",
      statusKey: "running",
      usage: "2.0k tokens · 3 tool uses",
      monitor: "",
      agentType: "",
      model: "",
      hint: "",
    });
  });
  test("no children → not expandable; type falls back to the tool input", () => {
    const m = subagentChipModel(tool({ name: "Task", input: { subagent_type: "general" } }), null);
    expect(m.expandable).toBe(false);
    expect(m.title).toBe("Subagent: general");
    expect(m.status).toBe("");
  });
  test("an orphan parent says it started earlier", () => {
    const m = subagentChipModel(tool({ orphan: true, children: [child] }), null);
    expect(m.title).toBe("Subagent (started earlier)");
    expect(m.expandable).toBe(true);
  });
  test("an orphan with a description is titled by it, with type, model and hint", () => {
    const r = rec({ kind: "agent", description: "Fix the build", subagent_type: "Explore", model: "sonnet", effort: "medium" });
    const m = subagentChipModel(tool({ orphan: true, children: [child] }), r);
    expect(m.title).toBe("Fix the build");
    expect(m.agentType).toBe("Explore");
    expect(m.model).toBe("Sonnet · medium");
    expect(m.hint).toBe("Started in an earlier turn");
  });
  test("an orphan with only a type is titled by it", () => {
    const m = subagentChipModel(tool({ orphan: true, children: [child] }), rec({ kind: "agent", subagent_type: "Explore" }));
    expect(m.title).toBe("Subagent: Explore");
    expect(m.agentType).toBe("");
    expect(m.hint).toBe("Started in an earlier turn");
  });
  test("an orphan with no record keeps the old text and no extras", () => {
    const m = subagentChipModel(tool({ orphan: true, children: [child] }), null);
    expect(m.title).toBe("Subagent (started earlier)");
    expect(m.model).toBe("");
    expect(m.hint).toBe("");
  });
  test("a non-orphan agent chip shows the model too", () => {
    const m = subagentChipModel(tool({ children: [child] }), rec({ kind: "agent", model: "opus", effort: "high" }));
    expect(m.model).toBe("Opus · high");
    expect(m.hint).toBe("");
  });
  test("a plain tool has no header extras", () => {
    const m = subagentChipModel(tool({ name: "Read", input: { file_path: "/a" } }), null);
    expect(m.title).toBe("");
    expect(m.monitor).toBe("");
  });
  test("a Monitor says watching while its task runs, plus events", () => {
    const seg = tool({ name: "Monitor", id: "tuM" });
    const live = rec({ id: "M", kind: "monitor", tool_use_id: "tuM", events: 3 });
    expect(subagentChipModel(seg, taskFor(seg, taskIndex([live]))).monitor).toBe("watching · 3 events");
    const done = { ...live, status: "completed" as const };
    expect(subagentChipModel(seg, taskFor(seg, taskIndex([done]))).monitor).toBe("3 events");
  });
});

describe("review #1: unknown (the owning run died)", () => {
  test("labelled ended, counted with the ended group", () => {
    expect(statusLabel("unknown")).toBe("ended");
    expect(summarizeTasks([rec({ id: "a" }), rec({ id: "u", status: "unknown" }), rec({ id: "d", status: "completed" })])).toBe(
      "1 task running",
    );
  });
  test("no clock against now when not live and ended_at is null", () => {
    expect(elapsedLabel(rec({ status: "unknown", started_at: 100, ended_at: null }), 999_000)).toBe("");
    expect(elapsedLabel(rec({ status: "completed", started_at: 100, ended_at: null }), 999_000)).toBe("");
  });
  test("sorts with the ended rows, offers no Output", () => {
    expect(sortTasks([rec({ id: "u", status: "unknown", started_at: 1 }), rec({ id: "r", started_at: 9 })]).map((t) => t.id)).toEqual(["r", "u"]);
    expect(hasOutput(rec({ kind: "shell", status: "unknown" }))).toBe(false);
    expect(hasOutput(rec({ kind: "shell", status: "completed" }))).toBe(true);
    expect(hasOutput(rec({ kind: "monitor", status: "completed" }))).toBe(true);
    expect(hasOutput(rec({ kind: "monitor", status: "running" }))).toBe(true);
    expect(hasOutput(rec({ kind: "agent", status: "completed" }))).toBe(false);
  });
  test("endActiveTasks downgrades running/pending/paused, leaves ended alone", () => {
    const out = endActiveTasks([
      rec({ id: "r" }),
      rec({ id: "p", status: "paused" }),
      rec({ id: "q", status: "pending" }),
      rec({ id: "d", status: "completed", ended_at: 5 }),
    ])!;
    expect(out.map((t) => [t.id, t.status, t.ended_at])).toEqual([
      ["r", "unknown", null],
      ["p", "unknown", null],
      ["q", "unknown", null],
      ["d", "completed", 5],
    ]);
    expect(endActiveTasks([rec({ status: "completed" })])).toBe(null);
  });
});

describe("review #7: paused", () => {
  test("its own word in the summary, not running", () => {
    expect(summarizeTasks([rec({ id: "a", kind: "agent" }), rec({ id: "p", kind: "shell", status: "paused" })])).toBe(
      "1 agent running · 1 paused",
    );
    expect(isLive("paused")).toBe(false);
  });
  test("no live clock, still sorted above the ended rows", () => {
    expect(elapsedLabel(rec({ status: "paused", started_at: 100 }), 999_000)).toBe("");
    expect(sortTasks([rec({ id: "d", status: "completed", started_at: 1 }), rec({ id: "p", status: "paused", started_at: 9 })]).map((t) => t.id)).toEqual(["p", "d"]);
  });
  test("a paused Monitor is not watching", () => {
    const seg = tool({ name: "Monitor", id: "tuM" });
    const paused = rec({ id: "M", kind: "monitor", tool_use_id: "tuM", events: 2, status: "paused" });
    expect(subagentChipModel(seg, taskFor(seg, taskIndex([paused]))).monitor).toBe("2 events");
  });
});
