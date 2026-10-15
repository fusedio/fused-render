// SPEC T-5/T-6: the tasks panel, the agent chip's subagent block and the
// working line's task verbs. react-test-renderer, no DOM: buttons are driven
// through their `onClick` props.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

const { TasksPanel, useTasksOpen, tasksPanelScope } = await import("./TasksPanel");
const { ToolChip } = await import("./ToolChip");
const { SegmentView } = await import("./SegmentView");
const { TasksContext } = await import("./tasksContext");
const { CardPolicyProvider, createCardPolicy } = await import("./cardPolicy");
const { activityVerb } = await import("./WorkingLine");
const { taskIndex } = await import("../protocol/tasks");
import type { Segment, TaskOutputResponse, TaskRecord, ToolSegment, ErrorOnly } from "../protocol/types";

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
    backgrounded: true,
    usage: null,
    error: "",
    summary: "",
    events: 0,
    ...over,
  };
}

const mounted: ReactTestRenderer[] = [];
function mount(el: React.ReactElement): ReactTestRenderer {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

const text = (n: ReactTestInstance): string =>
  n.children.map((c) => (typeof c === "string" ? c : text(c))).join("");
const byClass = (root: ReactTestInstance, cls: string) =>
  root.findAll((n) => typeof n.type === "string" && String(n.props.className ?? "").split(" ").includes(cls));

const TASKS = [
  rec({ id: "done", kind: "shell", description: "npm test", status: "completed", started_at: 38, ended_at: 68 }),
  rec({ id: "ag", kind: "agent", description: "Explore the repo", subagent_type: "Explore", started_at: 50, usage: { total_tokens: 4200, tool_uses: 7, duration_ms: 0 } }),
  rec({ id: "mon", kind: "monitor", description: "tail logs", started_at: 60, events: 3, summary: "line 3" }),
];

describe("TasksPanel", () => {
  test("nothing at all while closed with no tasks", () => {
    const r = mount(<TasksPanel tasks={[]} runId="r1" open={false} onToggle={() => {}} />);
    expect(r.toJSON()).toBeNull();
  });

  test("closed with only completed tasks renders nothing", () => {
    const tasks = [rec({ id: "d", kind: "shell", status: "completed" })];
    const r = mount(<TasksPanel tasks={tasks} runId="r1" open={false} onToggle={() => {}} />);
    expect(r.toJSON()).toBeNull();
  });

  test("collapsed summary counts live work and appends failures", () => {
    const tasks = [rec({ id: "a", kind: "agent" }), rec({ id: "f", kind: "agent", status: "failed" })];
    const r = mount(<TasksPanel tasks={tasks} runId="r1" open={false} onToggle={() => {}} />);
    expect(text(byClass(r.root, "chat-tasks-summary")[0]!)).toBe("1 agent · 1 failed");
  });

  test("collapsed: one line with the summary, no rows", () => {
    let toggles = 0;
    const r = mount(<TasksPanel tasks={TASKS} runId="r1" open={false} onToggle={() => toggles++} />);
    const root = r.root;
    expect(byClass(root, "chat-tasks").length).toBe(1);
    expect(text(byClass(root, "chat-tasks-summary")[0]!)).toBe("1 agent · 1 monitor");
    expect(byClass(root, "chat-tasks-title").length).toBe(0);
    expect(byClass(root, "chat-task").length).toBe(0);
    const head = byClass(root, "chat-tasks-head")[0]!;
    expect(head.props["aria-expanded"]).toBe(false);
    act(() => head.props.onClick());
    expect(toggles).toBe(1);
  });

  test("open: live rows first, usage for agents, events for monitors, Output for shell/monitor only", () => {
    const r = mount(<TasksPanel tasks={TASKS} runId="r1" open onToggle={() => {}} now={() => 70_000} />);
    const rows = byClass(r.root, "chat-task");
    expect(rows.map((n) => text(byClass(n, "chat-task-desc")[0]!))).toEqual(["Explore the repo", "tail logs", "npm test"]);
    // One line per row: no usage/events meta, no summary, no sub-line.
    for (const cls of ["chat-task-meta", "chat-task-summary", "chat-task-sub"]) {
      expect(rows.map((n) => byClass(n, cls).length)).toEqual([0, 0, 0]);
    }
    expect(byClass(rows[0]!, "chat-task-desc")[0]!.props.title).toBe("Explore the repo — Explore");
    expect(text(byClass(rows[0]!, "chat-task-time")[0]!)).toBe("20s");
    expect(text(byClass(rows[2]!, "chat-task-time")[0]!)).toBe("30s");
    expect(text(byClass(rows[2]!, "chat-task-status")[0]!)).toBe("done");
    expect(rows.map((n) => byClass(n, "chat-task-out-btn").length)).toEqual([0, 1, 1]);
  });

  test("an agent row shows its model; a shell row none; inherited names the parent", () => {
    const ag = rec({ id: "a", kind: "agent", description: "A", model: "sonnet", effort: "medium" });
    const inh = rec({ id: "i", kind: "agent", description: "I" });
    const sh = rec({ id: "s", kind: "shell", description: "S" });
    const r = mount(<TasksPanel tasks={[ag, inh, sh]} runId="r1" open onToggle={() => {}} parentModel="opus" />);
    const rows = byClass(r.root, "chat-task");
    const labels = rows.map((n) => byClass(n, "chat-task-model").map(text));
    expect(labels).toEqual([["Sonnet · medium"], ["Opus (inherited)"], []]);
    expect(byClass(rows[0]!, "chat-task-desc")[0]!.props.title).toBe("A — Sonnet · medium");
  });

  test("Show N finished sits inside the panel's list", () => {
    const old = rec({ id: "o", kind: "shell", description: "old", status: "completed", ended_at: 1 });
    const r = mount(<TasksPanel tasks={[old]} runId="r1" open onToggle={() => {}} now={() => 100_000} />);
    const empty = byClass(r.root, "chat-tasks-empty")[0]!;
    expect(byClass(empty, "chat-tasks-more").length).toBe(1);
    const live = rec({ id: "l", kind: "shell", description: "live" });
    const r2 = mount(<TasksPanel tasks={[old, live]} runId="r1" open onToggle={() => {}} now={() => 100_000} />);
    expect(byClass(byClass(r2.root, "chat-tasks-list")[0]!, "chat-tasks-more").length).toBe(1);
  });

  test("a worktree-isolated agent gets an inline marker and a tooltip, not a sub-line", () => {
    const iso = rec({ id: "w", kind: "agent", description: "Build fix", subagent_type: "general-purpose", isolation: "worktree" });
    const r = mount(<TasksPanel tasks={[iso, rec({ id: "n", kind: "agent", description: "Plain" })]} runId="r1" open onToggle={() => {}} />);
    const rows = byClass(r.root, "chat-task");
    const marked = rows[0]!.findAll((n) => n.props["aria-label"] === "in a worktree");
    expect(marked.length).toBeGreaterThan(0);
    expect(byClass(rows[0]!, "chat-task-desc")[0]!.props.title).toBe("Build fix — general-purpose · worktree");
    expect(rows[1]!.findAll((n) => n.props["aria-label"] === "in a worktree").length).toBe(0);
    expect(byClass(rows[1]!, "chat-task-desc")[0]!.props.title).toBe("Plain");
  });

  test("an error stays on one clamped line with the full text in title", () => {
    const r = mount(<TasksPanel tasks={[rec({ id: "e", kind: "shell", status: "failed", error: "boom\nlong" })]} runId="r1" open onToggle={() => {}} />);
    const err = byClass(r.root, "chat-task-error")[0]!;
    expect(err.props.title).toBe("boom\nlong");
  });

  describe("stop and dismiss", () => {
    const stopBtn = (row: ReactTestInstance) => row.findAll((n) => n.props["aria-label"] === "Stop task" && typeof n.type === "string");
    const dismissBtn = (row: ReactTestInstance) => row.findAll((n) => n.props["aria-label"] === "Dismiss" && typeof n.type === "string");

    test("Stop shows only for live rows with a runId", () => {
      const r = mount(<TasksPanel tasks={TASKS} runId="r1" open onToggle={() => {}} now={() => 70_000} />);
      expect(byClass(r.root, "chat-task").map((n) => stopBtn(n).length)).toEqual([1, 1, 0]);
      expect(stopBtn(byClass(r.root, "chat-task")[0]!)[0]!.props.title).toBe("Stop");
      const none = mount(<TasksPanel tasks={TASKS} runId={null} open onToggle={() => {}} now={() => 70_000} />);
      expect(stopBtn(none.root).length).toBe(0);
    });

    test("clicking Stop calls the seam, disables while in flight, and shows an error", async () => {
      const asked: Array<[string, string]> = [];
      let resolve!: (v: { error: string }) => void;
      const stopTask = (run: string, id: string) => {
        asked.push([run, id]);
        return new Promise<{ error: string }>((res) => (resolve = res));
      };
      const r = mount(<TasksPanel tasks={TASKS} runId="r1" open onToggle={() => {}} stopTask={stopTask} now={() => 70_000} />);
      const btn = () => stopBtn(byClass(r.root, "chat-task")[0]!)[0]!;
      act(() => btn().props.onClick());
      expect(asked).toEqual([["r1", "ag"]]);
      expect(btn().props.disabled).toBe(true);
      await act(async () => {
        resolve({ error: "No task found" });
      });
      expect(btn().props.disabled).toBe(false);
      expect(text(byClass(byClass(r.root, "chat-task")[0]!, "chat-task-error")[0]!)).toBe("No task found");
    });

    test("Dismiss hides an ended row and raises the Show N finished count", () => {
      const failed = rec({ id: "f", kind: "shell", description: "bad", status: "failed", error: "boom", started_at: 1, ended_at: 2 });
      const live = rec({ id: "l", kind: "shell", description: "live" });
      const r = mount(<TasksPanel tasks={[failed, live]} runId="r1" open onToggle={() => {}} now={() => 100_000} />);
      expect(stopBtn(byClass(r.root, "chat-task")[0]!).length).toBe(1);
      expect(dismissBtn(byClass(r.root, "chat-task")[0]!).length).toBe(0);
      expect(byClass(r.root, "chat-tasks-more").length).toBe(0);
      const row = () => byClass(r.root, "chat-task").find((n) => text(byClass(n, "chat-task-desc")[0]!) === "bad")!;
      act(() => dismissBtn(row())[0]!.props.onClick());
      expect(byClass(r.root, "chat-task").map((n) => text(byClass(n, "chat-task-desc")[0]!))).toEqual(["live"]);
      expect(text(byClass(r.root, "chat-tasks-more")[0]!)).toBe("Show 1 finished");
      act(() => byClass(r.root, "chat-tasks-more")[0]!.props.onClick());
      expect(byClass(r.root, "chat-task").length).toBe(2);
    });

    test("dismissals reset when the run changes", () => {
      const failed = rec({ id: "f", kind: "shell", description: "bad", status: "failed", error: "boom" });
      const r = mount(<TasksPanel tasks={[failed]} runId="r1" open onToggle={() => {}} now={() => 100_000} />);
      act(() => dismissBtn(r.root)[0]!.props.onClick());
      expect(byClass(r.root, "chat-task").length).toBe(0);
      act(() => r.update(<TasksPanel tasks={[failed]} runId="r2" open onToggle={() => {}} now={() => 100_000} />));
      expect(byClass(r.root, "chat-task").length).toBe(1);
    });

    test("state resets when the session changes even with runId null", () => {
      const failed = rec({ id: "f", kind: "shell", description: "bad", status: "failed", error: "boom" });
      const el = (sid: string) => (
        <TasksPanel tasks={[failed]} runId={null} sessionId={sid} open onToggle={() => {}} now={() => 100_000} />
      );
      const r = mount(el("s1"));
      act(() => dismissBtn(r.root)[0]!.props.onClick());
      act(() => byClass(r.root, "chat-tasks-more")[0]!.props.onClick());
      expect(byClass(r.root, "chat-task").length).toBe(1);
      act(() => r.update(el("s2")));
      expect(byClass(r.root, "chat-task").length).toBe(1);
      expect(byClass(r.root, "chat-tasks-more").length).toBe(0);
    });

    test("a paused row is marked is-paused, not faded as finished", () => {
      const paused = rec({ id: "p", kind: "agent", description: "held", status: "paused" });
      const r = mount(<TasksPanel tasks={[paused]} runId="r1" open onToggle={() => {}} now={() => 100_000} />);
      expect(byClass(r.root, "chat-task")[0]!.props.className).toContain("is-paused");
    });

    test("clicking a failed row's error line expands it", () => {
      const failed = rec({ id: "f", kind: "shell", status: "failed", error: "boom\nlong reason" });
      const r = mount(<TasksPanel tasks={[failed]} runId="r1" open onToggle={() => {}} />);
      const err = () => byClass(r.root, "chat-task-error")[0]!;
      expect(err().props.className).toContain("is-clamped");
      act(() => err().props.onClick());
      expect(err().props.className).not.toContain("is-clamped");
      act(() => err().props.onClick());
      expect(err().props.className).toContain("is-clamped");
    });
  });

  test("no run to ask → no Output buttons", () => {
    const r = mount(<TasksPanel tasks={TASKS} runId={null} open onToggle={() => {}} />);
    expect(byClass(r.root, "chat-task-out-btn").length).toBe(0);
  });

  test("open with no tasks says so", () => {
    const r = mount(<TasksPanel tasks={[]} runId="r1" open onToggle={() => {}} />);
    expect(text(byClass(r.root, "chat-tasks-empty")[0]!)).toBe("No agents or shells in this run.");
  });

  describe("finished rows", () => {
    const NOW = 1_000_000; // ms
    const ended = (id: string, agoS: number, status = "completed") =>
      rec({ id, kind: "shell", description: id, status: status as TaskRecord["status"], started_at: 1, ended_at: NOW / 1000 - agoS });
    const mk = (tasks: TaskRecord[], now: () => number = () => NOW) =>
      mount(<TasksPanel tasks={tasks} runId="r1" open onToggle={() => {}} now={now} />);
    const descs = (r: ReactTestRenderer) => byClass(r.root, "chat-task-desc").map((n) => text(n));

    test("an ended row 2 s old is shown, marked is-ended", () => {
      const r = mk([ended("a", 2)]);
      expect(descs(r)).toEqual(["a"]);
      expect(byClass(r.root, "chat-task")[0]!.props.className).toContain("is-ended");
      expect(byClass(r.root, "chat-tasks-more").length).toBe(0);
    });

    test("an ended row 6 s old is hidden; Show 1 finished reveals it, Hide finished hides it", () => {
      const r = mk([ended("a", 6)]);
      expect(descs(r)).toEqual([]);
      const more = () => byClass(r.root, "chat-tasks-more")[0]!;
      expect(text(more())).toBe("Show 1 finished");
      act(() => more().props.onClick());
      expect(descs(r)).toEqual(["a"]);
      expect(text(more())).toBe("Hide finished");
      act(() => more().props.onClick());
      expect(descs(r)).toEqual([]);
    });

    test("a failed row 60 s old is still shown", () => {
      const r = mk([ended("f", 60, "failed")]);
      expect(descs(r)).toEqual(["f"]);
    });

    test("every row hidden says Nothing running. with the toggle", () => {
      const r = mk([ended("a", 30), ended("b", 40)]);
      // The toggle now lives inside the box, so its words ride in the text.
      expect(text(byClass(r.root, "chat-tasks-empty")[0]!)).toBe("Nothing running.Show 2 finished");
      expect(text(byClass(r.root, "chat-tasks-more")[0]!)).toBe("Show 2 finished");
    });

    test("a timer hides a row once the clock passes the grace period, no live task needed", () => {
      const armed: Array<{ fn: () => void; ms: number }> = [];
      const realSet = window.setTimeout;
      const realClear = window.clearTimeout;
      window.setTimeout = ((fn: () => void, ms: number) => (armed.push({ fn, ms }), armed.length)) as unknown as typeof window.setTimeout;
      window.clearTimeout = (() => {}) as typeof window.clearTimeout;
      try {
        let clock = NOW;
        const r = mk([ended("a", 2)], () => clock);
        expect(descs(r)).toEqual(["a"]);
        const t = armed[armed.length - 1]!;
        expect(t.ms).toBe(3001);
        clock += t.ms;
        act(() => t.fn());
        expect(descs(r)).toEqual([]);
        expect(text(byClass(r.root, "chat-tasks-more")[0]!)).toBe("Show 1 finished");
      } finally {
        window.setTimeout = realSet;
        window.clearTimeout = realClear;
      }
    });
  });

  test("Output asks task_output and shows it in a CollapsiblePre; a second press hides it", async () => {
    const asked: Array<[string, string]> = [];
    let resolve!: (v: TaskOutputResponse | ErrorOnly) => void;
    const fetchOutput = (run: string, id: string) => {
      asked.push([run, id]);
      return new Promise<TaskOutputResponse | ErrorOnly>((res) => (resolve = res));
    };
    const r = mount(<TasksPanel tasks={TASKS} runId="r1" open onToggle={() => {}} fetchOutput={fetchOutput} now={() => 70_000} />);
    const btn = () => byClass(byClass(r.root, "chat-task")[2]!, "chat-task-out-btn")[0]!;
    act(() => btn().props.onClick());
    expect(asked).toEqual([["r1", "done"]]);
    expect(text(btn())).toBe("Loading…");
    await act(async () => {
      resolve({ output: "ok\nall green", status: "completed", truncated: true });
    });
    const pre = r.root.findAll((n) => n.type === "pre");
    expect(pre.length).toBe(1);
    expect(text(pre[0]!)).toBe("ok\nall green");
    expect(text(byClass(r.root, "chat-task-note")[0]!)).toBe("Showing the last 64 KB.");
    expect(text(btn())).toBe("Hide output");
    act(() => btn().props.onClick());
    expect(r.root.findAll((n) => n.type === "pre").length).toBe(0);
  });

  test("an error answer is shown in the row", async () => {
    const r = mount(
      <TasksPanel
        tasks={TASKS}
        runId="r1"
        open
        onToggle={() => {}}
        fetchOutput={async () => ({ error: "no live session" })}
        now={() => 70_000}
      />,
    );
    const btn = byClass(byClass(r.root, "chat-task")[1]!, "chat-task-out-btn")[0]!;
    await act(async () => {
      btn.props.onClick();
    });
    expect(text(byClass(byClass(r.root, "chat-task")[1]!, "chat-task-error")[0]!)).toBe("no live session");
  });
});

describe("agent chip", () => {
  const child: Segment = { kind: "tool", id: "c1", name: "Read", input: { file_path: "/a.ts" }, status: "ok", output: "x", images: [] };
  function chip(seg: Partial<ToolSegment>, tasks: TaskRecord[], opened = true) {
    const key = "k" + Math.random();
    const policy = createCardPolicy();
    if (opened) policy.overrides.set(key, true);
    const full = { kind: "tool", id: "tuA", name: "Agent", input: {}, status: "running", output: null, images: [], ...seg } as ToolSegment;
    return mount(
      <CardPolicyProvider value={policy}>
        <TasksContext.Provider value={taskIndex(tasks)}>
          <ToolChip
            seg={full}
            cardKey={key}
            nested={(segs, live) => <SegmentView segments={segs} live={live} />}
          />
        </TasksContext.Provider>
      </CardPolicyProvider>,
    );
  }

  test("children render through the segment renderer in a .chat-subagent block", () => {
    const r = chip({ children: [child], task_id: "T1" }, [rec({ id: "T1", kind: "agent", subagent_type: "Explore", usage: { total_tokens: 900, tool_uses: 2, duration_ms: 0 } })]);
    const block = byClass(r.root, "chat-subagent");
    expect(block.length).toBe(1);
    // The nested Read chip is a real ToolChip.
    expect(byClass(block[0]!, "toolchip").length).toBe(1);
    expect(text(byClass(r.root, "chip-task-type")[0]!)).toBe("Explore");
    expect(text(byClass(r.root, "chip-task-status")[0]!)).toBe("running");
    expect(byClass(r.root, "chip-task-meta").map(text)).toEqual(["900 tokens · 2 tool uses"]);
  });

  test("closed: no subagent block mounted", () => {
    const r = chip({ children: [child] }, [], false);
    expect(byClass(r.root, "chat-subagent").length).toBe(0);
  });

  test("an orphan parent is named as started earlier", () => {
    const r = chip({ children: [child], orphan: true }, []);
    expect(text(byClass(r.root, "chip-name")[0]!)).toBe("Subagent (started earlier)");
  });

  test("an orphan parent with a record is titled by its description, with type, model and a tooltip", () => {
    const r = chip({ children: [child], orphan: true, task_id: "T1" }, [
      rec({ id: "T1", kind: "agent", description: "Fix the build", subagent_type: "Explore", model: "sonnet", effort: "medium" }),
    ]);
    expect(text(byClass(r.root, "chip-name")[0]!)).toBe("Fix the build");
    expect(text(byClass(r.root, "chip-task-type")[0]!)).toBe("Explore");
    expect(byClass(r.root, "chip-task-meta").map(text)).toContain("Sonnet · medium");
    expect(byClass(r.root, "chip-summary")[0]!.props.title).toBe("Started in an earlier turn");
  });

  test("the memoized ToolChip body does not subscribe to the task index", async () => {
    const src = await Bun.file(new URL("./ToolChip.tsx", import.meta.url)).text();
    const start = src.indexOf("export const ToolChip = memo(");
    expect(start).toBeGreaterThan(0);
    expect(src.slice(start)).not.toContain("useTaskIndex");
    // Only the linked header reads it; the unlinked path never calls the hook.
    const linkedStart = src.indexOf("function LinkedChipHeader(");
    expect(src.match(/useTaskIndex\(\)/g)?.length).toBe(1);
    expect(src.indexOf("useTaskIndex()")).toBeGreaterThan(linkedStart);
    expect(src).toContain("if (props.linked) return <LinkedChipHeader");
  });

  test("a Monitor chip says watching and the events count while its task runs", () => {
    const r = chip({ name: "Monitor", id: "tuM", input: { command: "tail -f x" } }, [rec({ id: "M", kind: "monitor", tool_use_id: "tuM", events: 4 })]);
    expect(byClass(r.root, "chip-task-meta").map(text)).toEqual(["watching · 4 events"]);
  });
});

describe("working line verbs", () => {
  const agent = rec({ id: "a", kind: "agent" });
  const shell = rec({ id: "s", kind: "shell" });
  test("tasks name the verb in the model's own turn", () => {
    expect(activityVerb({ phase: "thinking", activity: null, tasks: [shell, agent] }, "Mulling", 0)).toBe("Running a subagent");
    expect(activityVerb({ phase: "thinking", activity: null, tasks: [shell] }, "Mulling", 0)).toBe("Running a background command");
    expect(activityVerb({ phase: "thinking", activity: null, tasks: [] }, "Mulling", 0)).toBe("Mulling");
  });
  test("an Agent tool call counts the running subagents", () => {
    const activity = { tool: { id: "x", name: "Agent", detail: "" }, tools_open: 1, tool_input_bytes: 0, thinking_tokens: 0, hook: "", tasks: [], agent_rows: 0 };
    expect(activityVerb({ phase: "tooling", activity, tasks: [agent, rec({ id: "b", kind: "agent" })] }, "x", 0)).toBe("Running 2 subagents");
    expect(activityVerb({ phase: "tooling", activity, tasks: [] }, "x", 0)).toBe("Starting a subagent");
  });
  test("an explicit model rides the verb; inherited never does", () => {
    const base = { tools_open: 1, thinking_tokens: 0, hook: "", tasks: [], agent_rows: 0, tool_input_bytes: 1 };
    const brief = { ...base, tool: { id: "x", name: "Agent", detail: "", streaming: true, model: "haiku" } };
    expect(activityVerb({ phase: "tooling", activity: brief, tasks: [] }, "x", 0)).toBe("Briefing a subagent (Haiku)");
    const run = { ...base, tool: { id: "x", name: "Agent", detail: "", subagent_type: "Explore", model: "sonnet", effort: "medium" } };
    expect(activityVerb({ phase: "tooling", activity: run, tasks: [] }, "x", 0)).toBe("Running Explore subagent (Sonnet · medium)");
    const none = { ...base, tool: { id: "x", name: "Agent", detail: "", subagent_type: "Explore" } };
    expect(activityVerb({ phase: "tooling", activity: none, tasks: [] }, "x", 0)).toBe("Running Explore subagent");
    const live = rec({ id: "a", kind: "agent", subagent_type: "Plan", model: "opus" });
    expect(activityVerb({ phase: "thinking", activity: null, tasks: [live] }, "x", 0)).toBe("Running Plan subagent (Opus)");
  });
  test("an Agent call still streaming its brief says so, then names the type", () => {
    const base = { tools_open: 1, thinking_tokens: 0, hook: "", tasks: [], agent_rows: 0 };
    const briefing = { ...base, tool: { id: "x", name: "Agent", detail: "", streaming: true }, tool_input_bytes: 300 };
    expect(activityVerb({ phase: "tooling", activity: briefing, tasks: [] }, "x", 0)).toBe("Briefing a subagent");
    // Still streaming after the description arrived: the brief is not done.
    const named = { ...base, tool: { id: "x", name: "Agent", detail: "Fix it", streaming: true }, tool_input_bytes: 300 };
    expect(activityVerb({ phase: "tooling", activity: named, tasks: [] }, "x", 0)).toBe("Briefing a subagent");
    const typedStreaming = { ...base, tool: { id: "x", name: "Agent", detail: "Fix it", subagent_type: "Explore", streaming: true }, tool_input_bytes: 300 };
    expect(activityVerb({ phase: "tooling", activity: typedStreaming, tasks: [] }, "x", 0)).toBe("Briefing Explore subagent");
    const typed = { ...base, tool: { id: "x", name: "Agent", detail: "Fix it", subagent_type: "Explore", streaming: false }, tool_input_bytes: 300 };
    expect(activityVerb({ phase: "tooling", activity: typed, tasks: [] }, "x", 0)).toBe("Running Explore subagent");
    const done = { ...base, tool: { id: "x", name: "Agent", detail: "", streaming: false }, tool_input_bytes: 300 };
    expect(activityVerb({ phase: "tooling", activity: done, tasks: [] }, "x", 0)).toBe("Starting a subagent");
    const typedRegistry = rec({ id: "t", kind: "agent", subagent_type: "Explore" });
    expect(activityVerb({ phase: "tooling", activity: briefing, tasks: [typedRegistry, agent] }, "x", 0)).toBe(
      "Running 2 subagents: 1 Explore, 1 other",
    );
  });
  test("the quiet-wait line keeps its wording", () => {
    const activity = { tool: null, tools_open: 0, tool_input_bytes: 0, thinking_tokens: 0, hook: "", tasks: [{ id: "s", description: "" }], agent_rows: 0 };
    expect(activityVerb({ phase: "thinking", activity, tasks: [shell] }, "x", 60)).toBe("Waiting for a background command to finish");
  });
});

describe("review #5: the open bit is scoped to the run and conversation", () => {
  test("opened for one run, collapsed again for the next; reopening works there", () => {
    let api!: [boolean, () => void];
    function Probe({ scope }: { scope: string }) {
      api = useTasksOpen(scope);
      return null;
    }
    const base = { lastRunId: "r1", tasksRunId: "r1", sessionId: "s1", transcriptGen: 1 };
    const r = mount(<Probe scope={tasksPanelScope(base)} />);
    expect(api[0]).toBe(false);
    act(() => api[1]());
    expect(api[0]).toBe(true);
    // A later run in the same session.
    act(() => r.update(<Probe scope={tasksPanelScope({ ...base, lastRunId: "r2", tasksRunId: "r2" })} />));
    expect(api[0]).toBe(false);
    act(() => api[1]());
    expect(api[0]).toBe(true);
    // Another session opened, and a new chat (everything back to empty).
    act(() => r.update(<Probe scope={tasksPanelScope({ ...base, sessionId: "s2", transcriptGen: 2 })} />));
    expect(api[0]).toBe(false);
    act(() => api[1]());
    act(() => r.update(<Probe scope={tasksPanelScope({ lastRunId: null, tasksRunId: null, sessionId: null, transcriptGen: 0 })} />));
    expect(api[0]).toBe(false);
  });
});

describe("review #6: the model's own streaming outranks the task verb", () => {
  const agent = rec({ id: "a", kind: "agent" });
  test("composing and requesting win; the task verb replaces only the mood", () => {
    expect(activityVerb({ phase: "composing", activity: null, tasks: [agent] }, "Mulling", 0)).toBe("Claude is replying");
    expect(activityVerb({ phase: "requesting", activity: null, tasks: [agent] }, "Mulling", 0)).toBe("Sending to Claude");
    expect(activityVerb({ phase: "thinking", activity: null, tasks: [agent] }, "Mulling", 0)).toBe("Running a subagent");
  });
});

describe("review #1/#4: an unknown task in the panel", () => {
  test("labelled ended, no time, no Output button, no live class", () => {
    const r = mount(
      <TasksPanel
        tasks={[rec({ id: "u", kind: "shell", description: "srv", status: "unknown", ended_at: null })]}
        runId="r1"
        open
        onToggle={() => {}}
        now={() => 999_000}
      />,
    );
    // No end time = long ended: hidden until "Show 1 finished".
    expect(byClass(r.root, "chat-task").length).toBe(0);
    act(() => byClass(r.root, "chat-tasks-more")[0]!.props.onClick());
    const row = byClass(r.root, "chat-task")[0]!;
    expect(text(byClass(row, "chat-task-status")[0]!)).toBe("ended");
    expect(byClass(row, "chat-task-status")[0]!.props.className).toContain("is-unknown");
    expect(byClass(row, "chat-task-time").length).toBe(0);
    expect(byClass(row, "chat-task-out-btn").length).toBe(0);
    expect(row.props.className).toContain("is-ended");
  });
});
