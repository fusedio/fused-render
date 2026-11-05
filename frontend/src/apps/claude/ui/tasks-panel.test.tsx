// SPEC T-5/T-6: the tasks panel, the agent chip's subagent block and the
// working line's task verbs. react-test-renderer, no DOM: buttons are driven
// through their `onClick` props.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
// The shim's `window` has no-op listeners and there is no `localStorage`: give
// the open-bit tests a real event bus and an in-memory store.
{
  const g = globalThis as unknown as Record<string, any>;
  const bus = new EventTarget();
  const w = g.window as Record<string, unknown>;
  w.addEventListener = bus.addEventListener.bind(bus);
  w.removeEventListener = bus.removeEventListener.bind(bus);
  w.dispatchEvent = bus.dispatchEvent.bind(bus);
  const store = new Map<string, string>();
  g.localStorage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  };
}
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
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

  test("open: only running rows, live first; no usage meta, no status pill, no Output button", () => {
    const r = mount(<TasksPanel tasks={TASKS} runId="r1" open onToggle={() => {}} now={() => 70_000} />);
    const rows = byClass(r.root, "chat-task");
    // The completed "npm test" is not listed.
    expect(rows.map((n) => text(byClass(n, "chat-task-desc")[0]!))).toEqual(["Explore the repo", "tail logs"]);
    for (const cls of ["chat-task-meta", "chat-task-summary", "chat-task-sub", "chat-task-status", "chat-task-out-btn"]) {
      expect(rows.map((n) => byClass(n, cls).length)).toEqual([0, 0]);
    }
    expect(byClass(r.root, "chat-tasks-more").length).toBe(0);
    expect(byClass(rows[0]!, "chat-task-desc")[0]!.props.title).toBe("Explore the repo — Explore");
    expect(text(byClass(rows[0]!, "chat-task-time")[0]!)).toBe("20s");
    expect(text(byClass(rows[1]!, "chat-task-time")[0]!)).toBe("10s");
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

  test("ended tasks are not rendered and there is no Show N finished", () => {
    const old = rec({ id: "o", kind: "shell", description: "old", status: "completed", ended_at: 1 });
    const bad = rec({ id: "b", kind: "shell", description: "bad", status: "failed", error: "boom", ended_at: 99 });
    const r = mount(<TasksPanel tasks={[old, bad]} runId="r1" open onToggle={() => {}} now={() => 100_000} />);
    expect(byClass(r.root, "chat-task").length).toBe(0);
    expect(byClass(r.root, "chat-tasks-more").length).toBe(0);
    expect(text(byClass(r.root, "chat-tasks-empty")[0]!)).toBe("Nothing running.");
    expect(byClass(r.root, "chat-task-error").length).toBe(0);
    const live = rec({ id: "l", kind: "shell", description: "live" });
    const r2 = mount(<TasksPanel tasks={[old, bad, live]} runId="r1" open onToggle={() => {}} now={() => 100_000} />);
    expect(byClass(r2.root, "chat-task-desc").map(text)).toEqual(["live"]);
    expect(byClass(r2.root, "chat-tasks-more").length).toBe(0);
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

  const stopBtn = (row: ReactTestInstance) => row.findAll((n) => n.props["aria-label"] === "Stop task" && typeof n.type === "string");
  const line = (row: ReactTestInstance) => byClass(row, "chat-task-line")[0]!;
  const ev = (key = "") => ({ key, stopPropagation() {}, preventDefault() {}, target: line, currentTarget: line });
  const press = (row: ReactTestInstance, key: string) => {
    const l = line(row);
    l.props.onKeyDown({ key, preventDefault() {}, target: l, currentTarget: l });
  };

  describe("stop", () => {
    test("Stop shows only for live rows with a runId", () => {
      const r = mount(<TasksPanel tasks={TASKS} runId="r1" open onToggle={() => {}} now={() => 70_000} />);
      expect(byClass(r.root, "chat-task").map((n) => stopBtn(n).length)).toEqual([1, 1]);
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
      act(() => btn().props.onClick(ev()));
      expect(asked).toEqual([["r1", "ag"]]);
      expect(btn().props.disabled).toBe(true);
      await act(async () => {
        resolve({ error: "No task found" });
      });
      expect(btn().props.disabled).toBe(false);
      expect(text(byClass(byClass(r.root, "chat-task")[0]!, "chat-task-error")[0]!)).toBe("No task found");
    });

    test("Stop does not also toggle the row or fetch output", async () => {
      const fetched: string[] = [];
      const stopped: string[] = [];
      const sh = rec({ id: "sh", kind: "shell", description: "srv" });
      let stopPropagated = 0;
      const r = mount(
        <TasksPanel
          tasks={[sh]}
          runId="r1"
          open
          onToggle={() => {}}
          fetchOutput={async (_run, id) => (fetched.push(id), { output: "x", status: "running", truncated: false })}
          stopTask={async (_run, id) => (stopped.push(id), {} as never)}
        />,
      );
      await act(async () => {
        stopBtn(r.root)[0]!.props.onClick({ stopPropagation: () => void stopPropagated++ });
      });
      expect(stopped).toEqual(["sh"]);
      expect(stopPropagated).toBe(1);
      expect(fetched).toEqual([]);
      expect(line(byClass(r.root, "chat-task")[0]!).props["aria-expanded"]).toBe(false);
    });

    test("stop state resets when the run changes", async () => {
      const r = mount(
        <TasksPanel tasks={TASKS} runId="r1" open onToggle={() => {}} stopTask={async () => ({ error: "nope" })} now={() => 70_000} />,
      );
      await act(async () => {
        stopBtn(r.root)[0]!.props.onClick(ev());
      });
      expect(byClass(r.root, "chat-task-error").length).toBe(1);
      act(() => r.update(<TasksPanel tasks={TASKS} runId="r2" open onToggle={() => {}} now={() => 70_000} />));
      expect(byClass(r.root, "chat-task-error").length).toBe(0);
    });

    test("a paused row is marked is-paused, still listed, and has no pill", () => {
      const paused = rec({ id: "p", kind: "agent", description: "held", status: "paused" });
      const r = mount(<TasksPanel tasks={[paused]} runId="r1" open onToggle={() => {}} now={() => 100_000} />);
      expect(byClass(r.root, "chat-task")[0]!.props.className).toContain("is-paused");
      expect(byClass(r.root, "chat-task-status").length).toBe(0);
    });
  });

  test("no run to ask: a shell row is not a toggle, and has no Output button", () => {
    const r = mount(<TasksPanel tasks={TASKS} runId={null} open onToggle={() => {}} />);
    expect(byClass(r.root, "chat-task-out-btn").length).toBe(0);
    const rows = byClass(r.root, "chat-task");
    // The agent row still toggles its update; the monitor (needs a run) does not.
    expect(line(rows[0]!).props.role).toBe("button");
    expect(line(rows[1]!).props.role).toBeUndefined();
  });

  test("open with no tasks says so", () => {
    const r = mount(<TasksPanel tasks={[]} runId="r1" open onToggle={() => {}} />);
    expect(text(byClass(r.root, "chat-tasks-empty")[0]!)).toBe("Nothing running.");
  });

  describe("the row toggles its output or update", () => {
    const SHELL = rec({ id: "sh", kind: "shell", description: "dev server", started_at: 60 });
    const seam = () => {
      const asked: Array<[string, string]> = [];
      let resolve!: (v: TaskOutputResponse | ErrorOnly) => void;
      const fetchOutput = (run: string, id: string) => {
        asked.push([run, id]);
        return new Promise<TaskOutputResponse | ErrorOnly>((res) => (resolve = res));
      };
      return { asked, fetchOutput, resolve: (v: TaskOutputResponse | ErrorOnly) => resolve(v) };
    };

    test("clicking a shell row asks task_output once and shows it in a CollapsiblePre; a second click hides it", async () => {
      const sm = seam();
      const r = mount(<TasksPanel tasks={[SHELL]} runId="r1" open onToggle={() => {}} fetchOutput={sm.fetchOutput} now={() => 70_000} />);
      const row = () => byClass(r.root, "chat-task")[0]!;
      expect(line(row()).props.role).toBe("button");
      expect(line(row()).props.tabIndex).toBe(0);
      expect(line(row()).props["aria-expanded"]).toBe(false);
      act(() => line(row()).props.onClick());
      expect(sm.asked).toEqual([["r1", "sh"]]);
      expect(text(byClass(row(), "chat-task-note")[0]!)).toBe("Loading…");
      // A second click while loading does not ask again.
      act(() => line(row()).props.onClick());
      expect(sm.asked.length).toBe(1);
      await act(async () => {
        sm.resolve({ output: "ok\nall green", status: "running", truncated: true });
      });
      const pre = r.root.findAll((n) => n.type === "pre");
      expect(pre.length).toBe(1);
      expect(text(pre[0]!)).toBe("ok\nall green");
      expect(text(byClass(r.root, "chat-task-note")[0]!)).toBe("Showing the end of the output.");
      expect(line(row()).props["aria-expanded"]).toBe(true);
      act(() => line(row()).props.onClick());
      expect(r.root.findAll((n) => n.type === "pre").length).toBe(0);
      expect(line(row()).props["aria-expanded"]).toBe(false);
      expect(sm.asked.length).toBe(1);
    });

    test("Enter and Space on the row do the same; other keys and bubbled keys do nothing", async () => {
      const sm = seam();
      const r = mount(<TasksPanel tasks={[SHELL]} runId="r1" open onToggle={() => {}} fetchOutput={sm.fetchOutput} now={() => 70_000} />);
      const row = () => byClass(r.root, "chat-task")[0]!;
      act(() => press(row(), "a"));
      expect(sm.asked.length).toBe(0);
      // A key pressed on a child (not the line itself) is ignored.
      act(() => line(row()).props.onKeyDown({ key: "Enter", preventDefault() {}, target: {}, currentTarget: line(row()) }));
      expect(sm.asked.length).toBe(0);
      act(() => press(row(), "Enter"));
      expect(sm.asked).toEqual([["r1", "sh"]]);
      await act(async () => {
        sm.resolve({ output: "hi", status: "running", truncated: false });
      });
      expect(line(row()).props["aria-expanded"]).toBe(true);
      act(() => press(row(), " "));
      expect(line(row()).props["aria-expanded"]).toBe(false);
    });

    test("an empty output says No output yet.", async () => {
      const r = mount(
        <TasksPanel tasks={[SHELL]} runId="r1" open onToggle={() => {}} fetchOutput={async () => ({ output: "", status: "running", truncated: false })} now={() => 70_000} />,
      );
      await act(async () => {
        line(byClass(r.root, "chat-task")[0]!).props.onClick();
      });
      expect(text(byClass(r.root, "chat-task-note")[0]!)).toBe("No output yet.");
    });

    describe("a shown output is polled while the task is live", () => {
      const w = globalThis.window as unknown as Record<string, any>;
      let ticks: Array<() => void> = [];
      let realSet: any, realClear: any;
      beforeEach(() => {
        realSet = w.setInterval;
        realClear = w.clearInterval;
        ticks = [];
        w.setInterval = (fn: () => void) => {
          ticks.push(fn);
          return ticks.length;
        };
        w.clearInterval = (id: number) => {
          ticks[id - 1] = () => {};
        };
      });
      afterEach(() => {
        w.setInterval = realSet;
        w.clearInterval = realClear;
      });
      const tick = async () => {
        await act(async () => {
          ticks.forEach((f) => f());
        });
      };
      const answers = (...a: Array<TaskOutputResponse | ErrorOnly | "reject">) => {
        let n = 0;
        const fetchOutput = async () => {
          const v = a[Math.min(n++, a.length - 1)]!;
          if (v === "reject") throw new Error("boom");
          return v;
        };
        return { fetchOutput, count: () => n };
      };
      const open1 = async (r: ReactTestRenderer) =>
        act(async () => {
          line(byClass(r.root, "chat-task")[0]!).props.onClick();
        });
      const ok = (output: string, truncated = false): TaskOutputResponse => ({ output, status: "running", truncated });

      test("an empty first read is replaced by the next poll", async () => {
        const a = answers(ok(""), ok("hello"));
        const r = mount(<TasksPanel tasks={[SHELL]} runId="r1" open onToggle={() => {}} fetchOutput={a.fetchOutput} now={() => 70_000} />);
        await open1(r);
        expect(text(byClass(r.root, "chat-task-note")[0]!)).toBe("No output yet.");
        await tick();
        expect(a.count()).toBe(2);
        expect(r.root.findAll((n) => n.type === "pre").map(text)).toEqual(["hello"]);
      });

      test("a failing poll keeps the last good text", async () => {
        const a = answers(ok("kept"), { error: "gone" }, "reject");
        const r = mount(<TasksPanel tasks={[SHELL]} runId="r1" open onToggle={() => {}} fetchOutput={a.fetchOutput} now={() => 70_000} />);
        await open1(r);
        await tick();
        await tick();
        expect(a.count()).toBe(3);
        expect(r.root.findAll((n) => n.type === "pre").map(text)).toEqual(["kept"]);
        expect(byClass(r.root, "chat-task-error").length).toBe(0);
      });

      test("a paused (not live) row is not polled and an empty one says No output.", async () => {
        const a = answers(ok(""));
        const paused = rec({ ...SHELL, status: "paused" });
        const r = mount(<TasksPanel tasks={[paused]} runId="r1" open onToggle={() => {}} fetchOutput={a.fetchOutput} now={() => 70_000} />);
        await open1(r);
        await tick();
        expect(a.count()).toBe(1);
        expect(text(byClass(r.root, "chat-task-note")[0]!)).toBe("No output.");
      });

      test("going from live to not live reads once more, then stops", async () => {
        const a = answers(ok("a"), ok("a\nfinal"));
        const mk = (status: string) => (
          <TasksPanel tasks={[rec({ ...SHELL, status: status as TaskRecord["status"] })]} runId="r1" open onToggle={() => {}} fetchOutput={a.fetchOutput} now={() => 70_000} />
        );
        const r = mount(mk("running"));
        await open1(r);
        await act(async () => r.update(mk("paused")));
        expect(a.count()).toBe(2);
        expect(r.root.findAll((n) => n.type === "pre").map(text)).toEqual(["a\nfinal"]);
        await tick();
        expect(a.count()).toBe(2);
      });

      test("an older in-flight poll answering after the final read does not overwrite it", async () => {
        const pending: Array<(v: TaskOutputResponse) => void> = [];
        let n = 0;
        const fetchOutput = (): Promise<TaskOutputResponse> => {
          if (n++ === 0) return Promise.resolve(ok("first"));
          return new Promise((res) => pending.push(res));
        };
        const mk = (status: string) => (
          <TasksPanel tasks={[rec({ ...SHELL, status: status as TaskRecord["status"] })]} runId="r1" open onToggle={() => {}} fetchOutput={fetchOutput} now={() => 70_000} />
        );
        const r = mount(mk("running"));
        await open1(r);
        await tick(); // poll #2, stays pending
        await act(async () => r.update(mk("paused"))); // forced final read #3
        expect(pending.length).toBe(2);
        await act(async () => pending[1]!(ok("final")));
        await act(async () => pending[0]!(ok("old")));
        expect(r.root.findAll((x) => x.type === "pre").map(text)).toEqual(["final"]);
      });

      test("collapsing stops the polling", async () => {
        const a = answers(ok("x"));
        const r = mount(<TasksPanel tasks={[SHELL]} runId="r1" open onToggle={() => {}} fetchOutput={a.fetchOutput} now={() => 70_000} />);
        await open1(r);
        act(() => line(byClass(r.root, "chat-task")[0]!).props.onClick());
        await tick();
        expect(a.count()).toBe(1);
      });
    });

    test("an error answer is shown under the row", async () => {
      const r = mount(
        <TasksPanel tasks={TASKS} runId="r1" open onToggle={() => {}} fetchOutput={async () => ({ error: "no live session" })} now={() => 70_000} />,
      );
      await act(async () => {
        line(byClass(r.root, "chat-task")[1]!).props.onClick();
      });
      expect(text(byClass(byClass(r.root, "chat-task")[1]!, "chat-task-error")[0]!)).toBe("no live session");
    });

    test("clicking an agent row shows its latest update (or No update yet.) and never fetches output", () => {
      const fetched: string[] = [];
      const fetchOutput = async (_run: string, id: string) => (fetched.push(id), { output: "", status: "running", truncated: false });
      const withSum = rec({ id: "a1", kind: "agent", description: "Explorer", summary: "Found 3 call sites" });
      const noSum = rec({ id: "a2", kind: "agent", description: "Fresh" });
      const r = mount(<TasksPanel tasks={[withSum, noSum]} runId="r1" open onToggle={() => {}} fetchOutput={fetchOutput} />);
      const row = (i: number) => byClass(r.root, "chat-task")[i]!;
      expect(byClass(row(0), "chat-task-note").length).toBe(0);
      act(() => line(row(0)).props.onClick());
      expect(text(byClass(row(0), "chat-task-note")[0]!)).toBe("Found 3 call sites");
      expect(line(row(0)).props["aria-expanded"]).toBe(true);
      expect(byClass(row(1), "chat-task-note").length).toBe(0);
      act(() => press(row(1), "Enter"));
      expect(text(byClass(row(1), "chat-task-note")[0]!)).toBe("No update yet.");
      act(() => line(row(0)).props.onClick());
      expect(byClass(row(0), "chat-task-note").length).toBe(0);
      expect(fetched).toEqual([]);
    });

    test("with onPeekAgent an agent row opens the peek, no inline note and no aria-expanded; without it the toggle stands", () => {
      const peeked: string[] = [];
      const a = rec({ id: "a1", kind: "agent", description: "Explorer", summary: "S", tool_use_id: "tu1" });
      const shell = rec({ id: "sh", kind: "shell", description: "tail" });
      const r = mount(<TasksPanel tasks={[a, shell]} runId={null} open onToggle={() => {}} onPeekAgent={(t) => void peeked.push(t.id)} />);
      const row = byClass(r.root, "chat-task")[0]!;
      expect(line(row).props["aria-expanded"]).toBeUndefined();
      act(() => line(row).props.onClick());
      act(() => press(row, "Enter"));
      expect(peeked).toEqual(["a1", "a1"]);
      expect(byClass(row, "chat-task-note").length).toBe(0);
      // An agent with no tool_use_id has nothing to peek at: the old toggle.
      const bare = rec({ id: "a2", kind: "agent", description: "Bare", summary: "B" });
      const r2 = mount(<TasksPanel tasks={[bare]} runId={null} open onToggle={() => {}} onPeekAgent={() => void peeked.push("x")} />);
      act(() => line(byClass(r2.root, "chat-task")[0]!).props.onClick());
      expect(text(byClass(r2.root, "chat-task-note")[0]!)).toBe("B");
      expect(peeked.length).toBe(2);
      // No prop: today's toggle.
      const r3 = mount(<TasksPanel tasks={[a]} runId={null} open onToggle={() => {}} />);
      act(() => line(byClass(r3.root, "chat-task")[0]!).props.onClick());
      expect(text(byClass(r3.root, "chat-task-note")[0]!)).toBe("S");
    });

    test("an agent row still toggles its update with no runId", () => {
      const a = rec({ id: "a1", kind: "agent", description: "Explorer", summary: "done soon" });
      const r = mount(<TasksPanel tasks={[a]} runId={null} open onToggle={() => {}} />);
      act(() => line(byClass(r.root, "chat-task")[0]!).props.onClick());
      expect(text(byClass(r.root, "chat-task-note")[0]!)).toBe("done soon");
    });

    test("open rows reset when the session changes", () => {
      const a = rec({ id: "a1", kind: "agent", description: "Explorer", summary: "x" });
      const el = (sid: string) => <TasksPanel tasks={[a]} runId={null} sessionId={sid} open onToggle={() => {}} />;
      const r = mount(el("s1"));
      act(() => line(byClass(r.root, "chat-task")[0]!).props.onClick());
      expect(byClass(r.root, "chat-task-note").length).toBe(1);
      act(() => r.update(el("s2")));
      expect(byClass(r.root, "chat-task-note").length).toBe(0);
    });
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

describe("the open bit is a remembered global preference", () => {
  const base = { lastRunId: "r1", tasksRunId: "r1", sessionId: "s1", transcriptGen: 1 };
  const next = { ...base, lastRunId: "r2", tasksRunId: "r2" };
  let api!: [boolean, () => void];
  function Probe({ scope, has }: { scope: string; has: boolean }) {
    api = useTasksOpen(scope, has);
    return null;
  }
  beforeEach(() => localStorage.clear());

  test("default is collapsed", () => {
    mount(<Probe scope={tasksPanelScope(base)} has={true} />);
    expect(api[0]).toBe(false);
  });
  test("open survives a new run and a new session while tasks exist", () => {
    const r = mount(<Probe scope={tasksPanelScope(base)} has={true} />);
    act(() => api[1]());
    expect(api[0]).toBe(true);
    act(() => r.update(<Probe scope={tasksPanelScope(next)} has={true} />));
    expect(api[0]).toBe(true);
    act(() => r.update(<Probe scope={tasksPanelScope({ ...base, sessionId: "s2", transcriptGen: 2 })} has={true} />));
    expect(api[0]).toBe(true);
  });
  test("remembered open + empty new scope is closed; toggling there opens it", () => {
    const r = mount(<Probe scope={tasksPanelScope(base)} has={true} />);
    act(() => api[1]());
    act(() => r.update(<Probe scope={tasksPanelScope(next)} has={false} />));
    expect(api[0]).toBe(false);
    act(() => api[1]());
    expect(api[0]).toBe(true);
  });
  test("closing persists into a new scope with tasks", () => {
    const r = mount(<Probe scope={tasksPanelScope(base)} has={true} />);
    act(() => api[1]());
    act(() => api[1]());
    expect(api[0]).toBe(false);
    act(() => r.update(<Probe scope={tasksPanelScope(next)} has={true} />));
    expect(api[0]).toBe(false);
  });
  test("toggle writes localStorage and a fresh mount reads it", () => {
    const r = mount(<Probe scope={tasksPanelScope(base)} has={true} />);
    act(() => api[1]());
    expect(localStorage.getItem("fused-render.claude.tasks-open")).toBe("1");
    act(() => r.unmount());
    mount(<Probe scope={tasksPanelScope(base)} has={true} />);
    expect(api[0]).toBe(true);
  });
  test("two mounted hooks stay in sync", () => {
    let a!: [boolean, () => void];
    let b!: [boolean, () => void];
    function A() {
      a = useTasksOpen("x", true);
      return null;
    }
    function B() {
      b = useTasksOpen("y", true);
      return null;
    }
    mount(<><A /><B /></>);
    act(() => a[1]());
    expect(b[0]).toBe(true);
    act(() => b[1]());
    expect(a[0]).toBe(false);
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
  test("an unknown (ended, outcome unreported) task is not active, so it is not listed", () => {
    const r = mount(
      <TasksPanel
        tasks={[rec({ id: "u", kind: "shell", description: "srv", status: "unknown", ended_at: null })]}
        runId="r1"
        open
        onToggle={() => {}}
        now={() => 999_000}
      />,
    );
    expect(byClass(r.root, "chat-task").length).toBe(0);
    expect(byClass(r.root, "chat-tasks-more").length).toBe(0);
    expect(text(byClass(r.root, "chat-tasks-empty")[0]!)).toBe("Nothing running.");
  });
});
