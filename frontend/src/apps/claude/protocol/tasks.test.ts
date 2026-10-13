import { describe, expect, test } from "bun:test";

import {
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
  test("running by kind, then ended by outcome", () => {
    const line = summarizeTasks([
      rec({ id: "a", kind: "agent" }),
      rec({ id: "b", kind: "shell" }),
      rec({ id: "c", kind: "monitor" }),
      rec({ id: "d", kind: "shell", status: "completed" }),
    ]);
    expect(line).toBe("3 running (1 agent, 1 shell, 1 monitor) · 1 done");
  });
  test("plurals, and failures are never folded into done", () => {
    const line = summarizeTasks([
      rec({ id: "a", kind: "agent" }),
      rec({ id: "b", kind: "agent", status: "pending" }),
      rec({ id: "c", kind: "shell", status: "failed" }),
      rec({ id: "d", kind: "shell", status: "killed" }),
      rec({ id: "e", kind: "agent", status: "completed" }),
      rec({ id: "f", kind: "agent", status: "completed" }),
    ]);
    expect(line).toBe("2 running (2 agents) · 2 done · 1 failed · 1 stopped");
  });
  test("nothing running", () => {
    expect(summarizeTasks([rec({ status: "completed" })])).toBe("1 done");
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
      "1 running (1 task) · 1 done · 1 ended",
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
      "1 running (1 agent) · 1 paused",
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
