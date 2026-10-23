// The Workspace view as rendered: row order, the marker, close with Undo.
// (The keyboard map is workspace-lib's pure `workspaceKeyAction`; the DOM shim
// has no event dispatch, so the key handler is not driven from here.)
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, mock, test } from "bun:test";
import { createElement } from "react";
import { act, create } from "react-test-renderer";
import type { Task } from "@platform/lib/api";

const { TaskWorkspace } = await import("./TaskWorkspace");

const realFetch = globalThis.fetch;
// Every tree rendered here is unmounted after its test: TaskWorkspace polls
// ports and holds on an interval, and a tree left mounted keeps that timer alive
// for the rest of the process, firing reads into whichever file's fetch stub is
// installed (useTaskId.test.tsx counted them: 10 reads instead of 2).
const mounted: ReturnType<typeof create>[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  globalThis.fetch = realFetch;
});

const task = (over: Partial<Task>): Task =>
  ({
    key: "k", task_id: "T1", title: "t", project: "/p", target: "/p", session_id: "s",
    status: "done", unread: 0, started: 1, ...over,
  }) as Task;

function serve() {
  const posts: { url: string; body: unknown }[] = [];
  globalThis.fetch = mock(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    posts.push({ url, body });
    const reply: Record<string, unknown> = {
      "/api/tasks/ports": { ports: {} },
      "/api/tasks/holds": { holds: {} },
      "/api/tasks/archive": { ok: true, key: body?.key, cancelled: 0, filed: true },
      "/api/tasks/unarchive": { ok: true, key: body?.key, unfiled: true, status: "done" },
    };
    return new Response(JSON.stringify(reply[url] ?? {}), { status: 200 });
  }) as unknown as typeof fetch;
  return posts;
}

async function render(tasks: Task[], onReload = () => {}) {
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(
      createElement(TaskWorkspace, {
        tasks, allTasks: tasks, emptyLabel: "Nothing.", peekOn: false, onReload, onNewTask: () => {},
      }),
    );
  });
  mounted.push(r);
  return r;
}

test("rows run newest first and archived tasks are left out", async () => {
  serve();
  const r = await render([
    task({ key: "a", task_id: "T1", started: 1 }),
    task({ key: "b", task_id: "T2", started: 2 }),
    task({ key: "c", task_id: "T3", started: 3, status: "archived" }),
  ]);
  const rows = r.root.findAll((n) => typeof n.props.className === "string" && /^ws-card( |$)/.test(n.props.className));
  expect(rows.map((n) => n.props["data-key"])).toEqual(["b", "a"]);
});

test("an unread row is marked for bold; a read one is not", async () => {
  serve();
  const r = await render([task({ key: "a", unread: 2 }), task({ key: "b", started: 0 })]);
  const byKey = (k: string) => r.root.findByProps({ "data-key": k });
  expect(byKey("a").props["data-unread"]).toBe("1");
  expect(byKey("b").props["data-unread"]).toBeUndefined();
});

test("a clear task says clear once its holds have loaded", async () => {
  serve();
  const r = await render([task({ key: "a" })]);
  expect(r.root.findByProps({ className: "ws-clear" }).children.join("")).toBe("clear");
});

test("the card drops reasons its chips or bold title already say, and the status word for Done", async () => {
  serve();
  const r = await render([
    task({ key: "a", status: "done", unread: 3, background_jobs: { count: 1, run_id: "r", jobs: [{ id: "j", title: "dev" }] } }),
  ]);
  // Held (unread + bg), so no "clear" tag, and nothing left to print as text.
  expect(r.root.findAllByProps({ className: "ws-clear" })).toHaveLength(0);
  expect(r.root.findAllByProps({ className: "ws-marker" })).toHaveLength(0);
  expect(r.root.findAllByProps({ className: "ws-status" })).toHaveLength(0);
  expect(r.root.findByProps({ className: "ws-bg" }).props.title).toBe("dev");
});

test("a running card shows its status word and not the running reason", async () => {
  serve();
  const r = await render([task({ key: "a", status: "in_progress" })]);
  expect(r.root.findByProps({ className: "ws-status" }).children.join("")).toBe("Running");
  expect(r.root.findAllByProps({ className: "ws-marker" })).toHaveLength(0);
});

test("an empty set says why", async () => {
  serve();
  const r = await render([]);
  expect(r.root.findByProps({ className: "schedule-tv-empty" }).children.join("")).toBe("Nothing.");
});

test("closing archives the task, hides the row and offers Undo, which un-archives", async () => {
  const posts = serve();
  let reloads = 0;
  const r = await render([task({ key: "a", title: "Fix it" })], () => {
    reloads++;
  });
  await act(async () => {
    r.root.findByProps({ className: "task-card-door ws-close" }).props.onClick({ stopPropagation() {} });
  });
  expect(posts.some((p) => p.url === "/api/tasks/archive" && (p.body as { key: string }).key === "a")).toBe(true);
  expect(r.root.findAllByProps({ "data-key": "a" })).toHaveLength(0);
  expect(reloads).toBe(1);
  await act(async () => {
    r.root.findByProps({ className: "ws-undo-btn" }).props.onClick();
  });
  expect(posts.some((p) => p.url === "/api/tasks/unarchive")).toBe(true);
  expect(r.root.findAllByProps({ className: "ws-undo" })).toHaveLength(0);
  expect(r.root.findAllByProps({ "data-key": "a" })).toHaveLength(1);
});
