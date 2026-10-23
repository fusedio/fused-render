// The Workspace view as rendered: row order, the marker, close with Undo.
// (The keyboard map is workspace-lib's pure `workspaceKeyAction`; the DOM shim
// has no event dispatch, so the key handler is not driven from here.)
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, mock, test } from "bun:test";
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
// The DOM shim has no storage. Most tests want "Safe to archive" open, so a
// stub says so; the collapsed-default test removes it.
const store: Record<string, string> = {};
const stubStorage = (): void => {
  (globalThis as { localStorage?: unknown }).localStorage = {
    getItem: (k: string) => store[k] ?? null,
    setItem: (k: string, v: string) => {
      store[k] = v;
    },
    removeItem: (k: string) => {
      delete store[k];
    },
  };
};
beforeEach(() => {
  for (const k of Object.keys(store)) delete store[k];
  store["fused.tasks.workspace.collapsed"] = JSON.stringify({ clear: false });
  stubStorage();
});
afterEach(() => {
  delete (globalThis as { localStorage?: unknown }).localStorage;
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
  expect(r.root.findByProps({ className: "ws-badge ws-clear" }).children.join("")).toBe("safe to archive");
});

test("the card drops reasons its chips or bold title already say, and the status word for Done", async () => {
  serve();
  const r = await render([
    task({ key: "a", status: "done", unread: 3, background_jobs: { count: 1, run_id: "r", jobs: [{ id: "j", title: "dev" }] } }),
  ]);
  // Held (unread + bg), so no "clear" tag, and nothing left to print as text.
  expect(r.root.findAllByProps({ className: "ws-badge ws-clear" })).toHaveLength(0);
  expect(r.root.findAllByProps({ className: "ws-badge ws-marker" })).toHaveLength(0);
  expect(r.root.findAllByProps({ className: "ws-badge ws-status" })).toHaveLength(0);
  expect(r.root.findByProps({ className: "ws-badge ws-bg" }).findByProps({ className: "ws-bg-title" }).children.join("")).toBe("dev");
});

const text = (n: { children: unknown[] }): string =>
  n.children.map((c) => (typeof c === "string" ? c : text(c as { children: unknown[] }))).join("");

test("background jobs show their titles, three at most, then +N bg", async () => {
  serve();
  const jobs = ["a", "b", "c", "d", "e"].map((t) => ({ id: t, title: t }));
  const r = await render([task({ key: "a", background_jobs: { count: 5, run_id: "r", jobs } })]);
  const chips = r.root.findAllByProps({ className: "ws-badge ws-bg" }).map(text);
  expect(chips).toEqual(["joba", "jobb", "jobc", "+2 bg"]);
});

test("PR chips cap at 8 and the dot sits inside the .task-pr-num pill", async () => {
  serve();
  const prs = Array.from({ length: 10 }, (_, i) => ({ repo: "o/r", number: i + 1 }));
  const r = await render([task({ key: "a", prs } as Partial<Task>)]);
  const pills = r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("task-pr-num"));
  expect(pills).toHaveLength(8);
  for (const pill of pills) expect(pill.findAllByProps({ className: "task-pr-dot" }).length).toBe(1);
  expect(text(r.root.findByProps({ className: "ws-pr-more" }))).toBe("+2");
});

test("the message preview shows unless it repeats the title", async () => {
  serve();
  const r = await render([
    task({ key: "a", title: "Fix it", last_message: { role: "assistant", text: "Done, merged.", at: 1 } }),
    task({ key: "b", started: 0, title: "Same", last_message: { role: "user", text: "Same", at: 1 } }),
  ]);
  const card = (k: string) => r.root.findByProps({ "data-key": k });
  expect(card("a").findAllByProps({ className: "ws-preview" })).toHaveLength(1);
  expect(card("b").findAllByProps({ className: "ws-preview" })).toHaveLength(0);
  expect(card("b").findAllByProps({ className: "ws-card-ctx" })).toHaveLength(0);
});

test("a running card shows its status word and not the running reason", async () => {
  serve();
  const r = await render([task({ key: "a", status: "in_progress" })]);
  expect(r.root.findByProps({ className: "ws-badge ws-status" }).children.join("")).toBe("Running");
  expect(r.root.findAllByProps({ className: "ws-badge ws-marker" })).toHaveLength(0);
});

test("an empty set says why", async () => {
  serve();
  const r = await render([]);
  expect(r.root.findByProps({ className: "schedule-tv-empty" }).children.join("")).toBe("Nothing.");
});

test("pressing x archives the task, hides the row and offers Undo, which un-archives", async () => {
  const posts = serve();
  let reloads = 0;
  const handlers: ((e: unknown) => void)[] = [];
  const realAdd = window.addEventListener;
  window.addEventListener = ((type: string, fn: (e: unknown) => void) => {
    if (type === "keydown") handlers.push(fn);
  }) as typeof window.addEventListener;
  const r = await render([task({ key: "a", title: "Fix it" })], () => {
    reloads++;
  });
  window.addEventListener = realAdd;
  const press = (key: string) =>
    handlers.forEach((h) =>
      h({ key, defaultPrevented: false, target: null, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, preventDefault() {} }),
    );
  await act(async () => {
    press("j");
  });
  await act(async () => {
    press("x"); // the card's hover x is gone; the keyboard still archives
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

test("the branch chip shows the worktree branch, with +N for more, and is omitted when unknown", async () => {
  globalThis.fetch = mock(async (url: string) => {
    const reply: Record<string, unknown> = {
      "/api/tasks/ports": { ports: {} },
      "/api/tasks/holds": {
        holds: {
          a: [
            { path: "/w1", dirty_files: 0, unpushed: 0, branch: "feat/one", reasons: [] },
            { path: "/w2", dirty_files: 0, unpushed: 0, branch: "feat/two", reasons: [] },
          ],
        },
      },
    };
    return new Response(JSON.stringify(reply[url] ?? {}), { status: 200 });
  }) as unknown as typeof fetch;
  const r = await render([task({ key: "a" }), task({ key: "b", started: 0 })]);
  const chip = r.root.findByProps({ "data-key": "a" }).findByProps({ className: "ws-badge ws-branch" });
  expect(chip.props.title).toBe("feat/one (+1 more)");
  expect(r.root.findByProps({ "data-key": "b" }).findAllByProps({ className: "ws-badge ws-branch" })).toHaveLength(0);
});

// -- Revision 4: two lines per card ---------------------------------------------
const within = (n: { findAllByProps: (p: object) => unknown[] }, cls: string) => n.findAllByProps({ className: cls });
const head = (card: ReturnType<ReturnType<typeof create>["root"]["findByProps"]>) => card.findByProps({ className: "ws-card-head" });

test("a card with only 'clear' is one line: clear rides line 1's right, after the time", async () => {
  serve();
  const r = await render([task({ key: "a" })]);
  const card = r.root.findByProps({ "data-key": "a" });
  expect(within(card, "ws-card-line2")).toHaveLength(0);
  expect(within(head(card), "ws-badge ws-clear")).toHaveLength(1);
});

test("a preview plus clear: clear sits at line 2's right end, not on its own line", async () => {
  serve();
  const r = await render([task({ key: "a", title: "Fix", last_message: { role: "assistant", text: "Merged.", at: 1 } })]);
  const card = r.root.findByProps({ "data-key": "a" });
  const line2 = card.findByProps({ className: "ws-card-line2" });
  expect(within(line2, "ws-preview")).toHaveLength(1);
  expect(within(line2.findByProps({ className: "ws-card-facts" }), "ws-badge ws-clear")).toHaveLength(1);
  expect(within(head(card), "ws-badge ws-clear")).toHaveLength(0);
  expect(within(card, "ws-card-facts")).toHaveLength(1);
});

test("facts sit in line 2's right group, clear last; a fact-only card has no left group", async () => {
  serve();
  const r = await render([task({ key: "a", prs: [{ repo: "o/r", number: 1 }] } as Partial<Task>)]);
  const card = r.root.findByProps({ "data-key": "a" });
  const facts = card.findByProps({ className: "ws-card-facts" });
  expect(within(facts, "ws-prs")).toHaveLength(1);
  expect(within(card, "ws-card-ctx")).toHaveLength(0);
  expect(within(head(card), "ws-badge ws-clear")).toHaveLength(0);
});

test("no hover x is rendered on a card", async () => {
  const r = await render([task({ key: "a", title: "Fix it" })]);
  expect(r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("ws-close"))).toHaveLength(0);
  const css = require("node:fs").readFileSync(require("node:path").join(import.meta.dir, "../styles/task-workspace.css"), "utf8") as string;
  expect(css).not.toMatch(/ws-close|ws-doors/);
  expect(css).toMatch(/\.ws-card-facts \{[^}]*max-width:\s*65%/);
  expect(css).not.toMatch(/data-view="workspace"/);
});

test("the list is the scroller: root fills the pane, list scrolls inside it", () => {
  const css = require("node:fs").readFileSync(require("node:path").join(import.meta.dir, "../styles/task-workspace.css"), "utf8") as string;
  expect(/\.schedule-page \.schedule-main > \.ws-root \{([^}]*)\}/.exec(css)![1]).toMatch(/min-height:\s*0/);
  const list = /\.ws-root > \.ws-list \{([^}]*)\}/.exec(css)![1];
  expect(list).toMatch(/overflow-y:\s*auto/);
  expect(list).toMatch(/min-height:\s*0/);
});

test("headers show label and count in order, and keys walk the grouped order", async () => {
  serve();
  const r = await render([
    task({ key: "a", started: 4 }),
    task({ key: "b", started: 3, status: "in_progress" }),
    task({ key: "c", started: 2, status: "needs_attention" }),
    task({ key: "d", started: 1, status: "blocked", blocked_reason: "failed" }),
  ]);
  const heads = r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("ws-group-head"));
  expect(heads.map((h) => text(h))).toEqual(["Needs you2", "Running1", "Safe to archive1"]);
  expect(heads.every((h) => h.props.role === "presentation")).toBe(true);
  expect(heads.every((h) => h.findAllByType("button").length === 1)).toBe(true);
  const cards = r.root.findAll((n) => typeof n.props.className === "string" && /^ws-card( |$)/.test(n.props.className));
  expect(cards.map((n) => n.props["data-key"])).toEqual(["c", "d", "b", "a"]);
});

const cardsOf = (r: ReturnType<typeof create>) =>
  r.root.findAll((n) => typeof n.props.className === "string" && /^ws-card( |$)/.test(n.props.className));
const moreBtn = (r: ReturnType<typeof create>) =>
  r.root.findAll((n) => n.type === "button" && typeof n.props.className === "string" && n.props.className.includes("ws-more"));

test("a held card with unread shows an unread badge", async () => {
  serve();
  const r = await render([task({ key: "a", unread: 2 })]);
  const badges = r.root.findAll((n) => n.props.className === "ws-badge ws-unread");
  expect(badges).toHaveLength(1);
  expect(text(badges[0])).toBe("unread");
});

test("a group over 8 cards shows 8 and a Show N more that reveals the rest", async () => {
  serve();
  const many = Array.from({ length: 11 }, (_, i) => task({ key: `k${i}`, task_id: `T${i}`, started: 100 - i, unread: 1 }));
  const r = await render(many);
  expect(cardsOf(r)).toHaveLength(8);
  const heads = r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("ws-group-head"));
  expect(text(heads[0])).toBe("Open11");
  const btn = moreBtn(r);
  expect(btn).toHaveLength(1);
  expect(text(btn[0])).toBe("Show 3 more");
  await act(async () => {
    btn[0].props.onClick();
  });
  expect(cardsOf(r)).toHaveLength(11);
  expect(moreBtn(r)).toHaveLength(0);
});

test("Safe to archive starts collapsed: header and count, no cards", async () => {
  serve();
  for (const k of Object.keys(store)) delete store[k];
  const r = await render([task({ key: "a" }), task({ key: "b", started: 0 })]);
  const heads = r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("ws-group-head"));
  expect(heads.map((h) => text(h))).toEqual(["Safe to archive2"]);
  expect(cardsOf(r)).toHaveLength(0);
  await act(async () => {
    heads[0].findByType("button").props.onClick();
  });
  expect(cardsOf(r)).toHaveLength(2);
  expect(JSON.parse(store["fused.tasks.workspace.collapsed"])).toEqual({ clear: false });
});

test("a header click collapses an open group and remembers it", async () => {
  serve();
  const r = await render([task({ key: "a", unread: 1 })]);
  expect(cardsOf(r)).toHaveLength(1);
  const head = r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("ws-group-head"))[0];
  await act(async () => {
    head.findByType("button").props.onClick();
  });
  expect(cardsOf(r)).toHaveLength(0);
  expect(JSON.parse(store["fused.tasks.workspace.collapsed"]).idle).toBe(true);
});
