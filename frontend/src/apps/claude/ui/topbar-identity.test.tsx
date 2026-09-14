// THE CHAT'S HEADER IS THE TASK PANEL'S (Akshil, 2026-09-14).
//
// In a session with a row behind it, the top line is the task side peek's own
// identity block — status ring · TASK-nnn · title · project — and not the ✻
// Claude wordmark and the file path it used to print. The two surfaces draw the
// SAME component (`shell/TaskPeekWho.tsx`), which is the whole point: a reader
// moving between the peek and the chat must not have to pair up two headers for
// one conversation.
//
// The fallback is the other half of the claim, and it is a real state: a chat
// seconds old has a session id before `/api/tasks` has a row for it, so the old
// line stands until one lands rather than the header flashing in late.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { Task } from "@platform/lib/api";

// DYNAMIC, after the shim above has run: the header reaches
// `@platform/lib/router` through the shell row's own link builder, and that
// module reads `location` at import time (testDomShim's own note). A static
// import is hoisted above the shim call.
const { Topbar } = await import("./Topbar");

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

const task = (over: Partial<Task> = {}): Task =>
  ({
    key: "sess-1",
    task_id: "TASK-042",
    title: "Rename the pane noun",
    project: "/repo/app",
    target: "/repo/app/x.py",
    session_id: "sess-1",
    status: "in_progress",
    ...over,
  }) as Task;

function render(props: Parameters<typeof Topbar>[0]) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Topbar, props));
  });
  mounted.push(r);
  const has = (cls: string) =>
    r.root.findAll(
      (n) => typeof n.type === "string" && String(n.props.className ?? "").split(" ").includes(cls),
      { deep: true },
    );
  return { r, has, text: () => JSON.stringify(r.toJSON()) };
}

test("a session with a task row wears the peek's identity block", () => {
  const v = render({ sessionId: "sess-1", subtitle: "x.py", task: task(), running: false });
  expect(v.has("task-side-peek-who").length).toBe(1);
  expect(v.has("task-side-peek-id").length).toBe(1);
  expect(v.has("task-side-peek-title").length).toBe(1);
  expect(v.has("task-side-peek-project").length).toBe(1);
  expect(v.text()).toContain("TASK-042");
  expect(v.text()).toContain("Rename the pane noun");
  // The project is the folder's NAME, not its path — the chip's tooltip carries
  // the whole of it.
  expect(v.text()).toContain("app");
  // …and the tool's own marks are gone: the wordmark, the "Claude" label and
  // the file path are facts about the tool and the file, printed twice over
  // elsewhere on the page.
  expect(v.has("c-tb-title").length).toBe(0);
  expect(v.has("c-tb-file").length).toBe(0);
  expect(v.has("c-session").length).toBe(0);
});

test("the running word stays beside the ring", () => {
  // The ring is the LISTING's answer and arrives on a long-poll; this is the
  // page's own turn clock, and at both ends of a turn it is the faster of the
  // two.
  const v = render({ sessionId: "sess-1", task: task(), running: true });
  expect(v.has("c-tb-run").length).toBe(1);
  const off = render({ sessionId: "sess-1", task: task(), running: false });
  expect(off.has("c-tb-run").length).toBe(0);
});

test("no row yet — the line the chat has always printed", () => {
  const v = render({
    sessionId: "sess-1",
    subtitle: "x.py",
    taskId: "TASK-042",
    task: null,
    running: false,
  });
  expect(v.has("task-side-peek-who").length).toBe(0);
  expect(v.has("c-tb-title").length).toBe(1);
  expect(v.has("c-session").length).toBe(1);
  expect(v.text()).toContain("TASK-042");
});
