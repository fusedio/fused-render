// The working line's "background" button and its Ctrl+B shortcut. Props only
// (no mock.module); react-test-renderer has no DOM, so the document keydown
// listener is driven through a real event bus swapped onto `document`.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { TaskRecord } from "../protocol/types";
import type { Working } from "../protocol/controller-api";

const { WorkingLine, backgroundable, __resetBackgroundKeyState } = await import("./WorkingLine");
const { AgentProvider } = await import("./agent-context");

const doc = globalThis.document as unknown as Record<string, unknown>;
const saved = { add: doc.addEventListener, remove: doc.removeEventListener };
const bus = new EventTarget();
beforeAll(() => {
  doc.addEventListener = bus.addEventListener.bind(bus);
  doc.removeEventListener = bus.removeEventListener.bind(bus);
});
afterAll(() => {
  doc.addEventListener = saved.add;
  doc.removeEventListener = saved.remove;
});

function working(over: Partial<Working> = {}, toolOver: Record<string, unknown> = {}): Working {
  return {
    startedAt: Date.now(),
    tokens: 0,
    phase: "tooling",
    retry: null,
    external: false,
    activity: { tool: { id: "toolu_1", name: "Bash", detail: "sleep 60", ...toolOver } },
    ...over,
  } as unknown as Working;
}

function task(over: Record<string, unknown> = {}) {
  return {
    id: "b1",
    description: "",
    kind: "shell",
    type: "local_bash",
    subagent_type: "",
    isolation: "",
    tool_use_id: "toolu_1",
    status: "running",
    started_at: 0,
    ended_at: null,
    backgrounded: false,
    usage: null,
    error: "",
    summary: "",
    events: 0,
    ...over,
  } as unknown as TaskRecord;
}
const T = [task()];

const mounted: ReactTestRenderer[] = [];
// The renderer has no DOM, so the line's ref is a stub whose `closest` names
// the chat-root it sits in (a fresh one per mount unless given).
function mount(el: React.ReactElement, chatRoot: object = { contains: () => true, isConnected: true }): ReactTestRenderer {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(el, { createNodeMock: () => ({ closest: () => chatRoot }) });
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  __resetBackgroundKeyState();
  while (mounted.length) {
    const r = mounted.pop()!;
    act(() => r.unmount());
  }
});

function press(type: string, target: unknown) {
  const ev = new Event(type);
  Object.defineProperty(ev, "target", { value: { closest: () => target } });
  act(() => void bus.dispatchEvent(ev));
}

function key(init: Record<string, unknown>) {
  let prevented = false;
  const props = {
    key: "b",
    ctrlKey: false,
    metaKey: false,
    altKey: false,
    shiftKey: false,
    target: null,
    ...init,
  };
  const ev = new Event("keydown");
  for (const [k, v] of Object.entries(props)) Object.defineProperty(ev, k, { value: v });
  Object.defineProperty(ev, "defaultPrevented", { get: () => prevented });
  Object.defineProperty(ev, "preventDefault", { value: () => (prevented = true) });
  act(() => void bus.dispatchEvent(ev));
  return () => prevented;
}

const buttons = (r: ReactTestRenderer) => r.root.findAllByType("button");

describe("backgroundable", () => {
  test("truth table", () => {
    expect(backgroundable(working(), "running", T)).toBe(true);
    expect(backgroundable(working({}, { name: "Agent" }), "running", T)).toBe(false);
    expect(backgroundable(working({ phase: "composing" }), "running", T)).toBe(false);
    expect(backgroundable(working({ external: true }), "running", T)).toBe(false);
    expect(backgroundable(working(), "stopping", T)).toBe(false);
    expect(backgroundable(working({}, { id: "" }), "running", T)).toBe(false);
  });

  test("needs a matching running, not-yet-backgrounded task", () => {
    expect(backgroundable(working(), "running")).toBe(false);
    expect(backgroundable(working(), "running", [])).toBe(false);
    expect(backgroundable(working(), "running", [task({ tool_use_id: "toolu_other" })])).toBe(false);
    expect(backgroundable(working(), "running", [task({ backgrounded: true })])).toBe(false);
    expect(backgroundable(working(), "running", [task({ status: "completed" })])).toBe(false);
  });
});

describe("background button", () => {
  test("renders only with onBackground and a backgroundable state; click calls it", () => {
    let n = 0;
    const r = mount(<WorkingLine working={working()} status="running" tasks={T} onStop={() => {}} onBackground={() => n++} />);
    const bg = buttons(r).filter((b) => b.props.className === "stop background");
    expect(bg).toHaveLength(1);
    act(() => bg[0].props.onClick());
    expect(n).toBe(1);
    expect(buttons(r)[0]).toBe(bg[0]); // before the stop button

    const noProp = mount(<WorkingLine working={working()} status="running" tasks={T} onStop={() => {}} />);
    expect(buttons(noProp)).toHaveLength(1);
    const notBash = mount(
      <WorkingLine working={working({}, { name: "Read" })} status="running" tasks={T} onStop={() => {}} onBackground={() => {}} />,
    );
    expect(buttons(notBash)).toHaveLength(1);
  });

  test("tool open but no task yet: no button", () => {
    const r = mount(<WorkingLine working={working()} status="running" onStop={() => {}} onBackground={() => {}} />);
    expect(buttons(r)).toHaveLength(1);
  });
});

describe("Ctrl+B", () => {
  test("calls onBackground on document.body", () => {
    let n = 0;
    mount(<WorkingLine working={working()} status="running" tasks={T} onStop={() => {}} onBackground={() => n++} />);
    const prevented = key({ ctrlKey: true });
    expect(n).toBe(1);
    expect(prevented()).toBe(true);
  });

  test("Cmd+B and Ctrl+Shift+B do nothing", () => {
    let n = 0;
    mount(<WorkingLine working={working()} status="running" tasks={T} onStop={() => {}} onBackground={() => n++} />);
    key({ metaKey: true });
    key({ ctrlKey: true, shiftKey: true });
    expect(n).toBe(0);
  });

  test("tool open but no task yet: Ctrl+B not claimed", () => {
    let n = 0;
    mount(<WorkingLine working={working()} status="running" onStop={() => {}} onBackground={() => n++} />);
    const prevented = key({ ctrlKey: true });
    expect(n).toBe(0);
    expect(prevented()).toBe(false);
  });

  test("not backgroundable: ignored and not prevented", () => {
    let n = 0;
    mount(
      <WorkingLine working={working({ phase: "composing" })} status="running" tasks={T} onStop={() => {}} onBackground={() => n++} />,
    );
    const prevented = key({ ctrlKey: true });
    expect(n).toBe(0);
    expect(prevented()).toBe(false);
  });
});

describe("backend and multi-chat", () => {
  test("a non-Claude backend gets no button and Ctrl+B is left alone", () => {
    let n = 0;
    const r = mount(
      <AgentProvider backend="codex">
        <WorkingLine working={working()} status="running" tasks={T} onStop={() => {}} onBackground={() => n++} />
      </AgentProvider>,
    );
    expect(buttons(r)).toHaveLength(1);
    const prevented = key({ ctrlKey: true });
    expect(n).toBe(0);
    expect(prevented()).toBe(false);
  });

  const two = () => {
    const calls: string[] = [];
    const rootA = { contains: () => true, isConnected: true };
    const rootB = { contains: () => true, isConnected: true };
    mount(<WorkingLine working={working()} status="running" tasks={T} onStop={() => {}} onBackground={() => calls.push("a")} />, rootA);
    mount(<WorkingLine working={working()} status="running" tasks={T} onStop={() => {}} onBackground={() => calls.push("b")} />, rootB);
    return { calls, rootA, rootB };
  };

  test("a loose Ctrl+B goes to the chat last clicked", () => {
    const { calls, rootB } = two();
    press("pointerdown", rootB);
    const prevented = key({ ctrlKey: true });
    expect(calls).toEqual(["b"]);
    expect(prevented()).toBe(true);
  });

  test("two claimants and no recorded interaction: neither fires", () => {
    const { calls } = two();
    const prevented = key({ ctrlKey: true });
    expect(calls).toEqual([]);
    expect(prevented()).toBe(false);
  });
});
