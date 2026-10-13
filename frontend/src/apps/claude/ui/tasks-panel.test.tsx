// SPEC T-5/T-6: the tasks panel, the agent chip's subagent block and the
// working line's task verbs. react-test-renderer, no DOM: buttons are driven
// through their `onClick` props.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

const { TasksPanel } = await import("./TasksPanel");
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
  rec({ id: "done", kind: "shell", description: "npm test", status: "completed", started_at: 10, ended_at: 40 }),
  rec({ id: "ag", kind: "agent", description: "Explore the repo", subagent_type: "Explore", started_at: 50, usage: { total_tokens: 4200, tool_uses: 7, duration_ms: 0 } }),
  rec({ id: "mon", kind: "monitor", description: "tail logs", started_at: 60, events: 3, summary: "line 3" }),
];

describe("TasksPanel", () => {
  test("nothing at all while closed with no tasks", () => {
    const r = mount(<TasksPanel tasks={[]} runId="r1" open={false} onToggle={() => {}} />);
    expect(r.toJSON()).toBeNull();
  });

  test("collapsed: one line with the summary, no rows", () => {
    let toggles = 0;
    const r = mount(<TasksPanel tasks={TASKS} runId="r1" open={false} onToggle={() => toggles++} />);
    const root = r.root;
    expect(byClass(root, "chat-tasks").length).toBe(1);
    expect(text(byClass(root, "chat-tasks-summary")[0]!)).toBe("2 running (1 agent, 1 monitor) · 1 done");
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
    expect(text(byClass(rows[0]!, "chat-task-meta")[0]!)).toBe("4.2k tokens · 7 tool uses");
    expect(text(byClass(rows[1]!, "chat-task-meta")[0]!)).toBe("3 events");
    expect(text(byClass(rows[1]!, "chat-task-summary")[0]!)).toBe("line 3");
    expect(text(byClass(rows[0]!, "chat-task-time")[0]!)).toBe("20s");
    expect(text(byClass(rows[2]!, "chat-task-time")[0]!)).toBe("30s");
    expect(text(byClass(rows[2]!, "chat-task-status")[0]!)).toBe("done");
    expect(rows.map((n) => byClass(n, "chat-task-out-btn").length)).toEqual([0, 1, 1]);
  });

  test("no run to ask → no Output buttons", () => {
    const r = mount(<TasksPanel tasks={TASKS} runId={null} open onToggle={() => {}} />);
    expect(byClass(r.root, "chat-task-out-btn").length).toBe(0);
  });

  test("open with no tasks says so", () => {
    const r = mount(<TasksPanel tasks={[]} runId="r1" open onToggle={() => {}} />);
    expect(text(byClass(r.root, "chat-tasks-empty")[0]!)).toBe("No background tasks in this run.");
  });

  test("Output asks task_output and shows it in a CollapsiblePre; a second press hides it", async () => {
    const asked: Array<[string, string]> = [];
    let resolve!: (v: TaskOutputResponse | ErrorOnly) => void;
    const fetchOutput = (run: string, id: string) => {
      asked.push([run, id]);
      return new Promise<TaskOutputResponse | ErrorOnly>((res) => (resolve = res));
    };
    const r = mount(<TasksPanel tasks={TASKS} runId="r1" open onToggle={() => {}} fetchOutput={fetchOutput} />);
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
    expect(activityVerb({ phase: "tooling", activity, tasks: [] }, "x", 0)).toBe("Running a helper agent");
  });
  test("the quiet-wait line keeps its wording", () => {
    const activity = { tool: null, tools_open: 0, tool_input_bytes: 0, thinking_tokens: 0, hook: "", tasks: [{ id: "s", description: "" }], agent_rows: 0 };
    expect(activityVerb({ phase: "thinking", activity, tasks: [shell] }, "x", 60)).toBe("Waiting for a background command to finish");
  });
});
