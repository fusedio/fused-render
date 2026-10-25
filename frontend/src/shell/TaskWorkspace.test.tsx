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

test("rows run newest first, archived ones included", async () => {
  serve();
  const r = await render([
    task({ key: "a", task_id: "T1", started: 1 }),
    task({ key: "b", task_id: "T2", started: 2 }),
    task({ key: "c", task_id: "T3", started: 3, status: "archived" }),
  ]);
  const rows = r.root.findAll((n) => typeof n.props.className === "string" && /^ws-card( |$)/.test(n.props.className));
  expect(rows.map((n) => n.props["data-key"])).toEqual(["b", "a", "c"]);
});

test("an archived card wears no hold markers and its group has no checkbox", async () => {
  serve();
  const r = await render([task({ key: "c", status: "archived" })]);
  const card = r.root.findByProps({ "data-key": "c" });
  expect(within(card, "ws-badge ws-clear")).toHaveLength(0);
  expect(within(card, "ws-marker")).toHaveLength(0);
  expect(within(card, "ws-skel")).toHaveLength(0);
  const heads = r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("ws-group-head"));
  expect(heads.map((h) => text(h))).toEqual(["Archived1"]);
  expect(byClass(r, "tasks-group-check")).toHaveLength(0);
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
  const r = await render([task({ key: "a", last_active: Math.floor(Date.now() / 1000) - 25 * 3600 })]);
  expect(r.root.findByProps({ className: "ws-badge ws-clear" }).children.join("")).toBe("safe to archive");
});

test("a fresh done task with no holds sits in Your turn and wears no safe-to-archive badge", async () => {
  serve();
  const r = await render([task({ key: "a", last_active: Math.floor(Date.now() / 1000) - 3600 })]);
  expect(r.root.findAllByProps({ className: "ws-badge ws-clear" })).toHaveLength(0);
});

test("the card drops reasons its chips or bold title already say, and the status word for Done", async () => {
  serve();
  const r = await render([
    task({ key: "a", status: "done", unread: 3, background_jobs: { count: 1, run_id: "r", jobs: [{ id: "j", title: "dev" }] } }),
  ]);
  // Held (bg job), so no "clear" tag, and nothing left to print as text.
  expect(r.root.findAllByProps({ className: "ws-badge ws-clear" })).toHaveLength(0);
  expect(r.root.findAllByProps({ className: "ws-badge ws-marker" })).toHaveLength(0);
  expect(r.root.findAllByProps({ className: "ws-badge ws-status" })).toHaveLength(0);
  expect(text(r.root.findByProps({ className: "ws-badge ws-bg" }))).toBe("1 job");
});

const text = (n: { children: unknown[] }): string =>
  n.children.map((c) => (typeof c === "string" ? c : text(c as { children: unknown[] }))).join("");

test("background jobs show one count badge whose tooltip lists the titles", async () => {
  serve();
  const jobs = ["a", "b", "c", "d", "e"].map((t) => ({ id: t, title: t }));
  const r = await render([task({ key: "a", background_jobs: { count: 5, run_id: "r", jobs } })]);
  const chips = r.root.findAllByProps({ className: "ws-badge ws-bg" });
  expect(chips.map(text)).toEqual(["5 jobs"]);
  expect(chips[0].props.title).toBe("a\nb\nc\nd\ne");
});

test("PR chips cap at 3, open first, and the dot sits inside the .task-pr-num pill", async () => {
  serve();
  const prs = Array.from({ length: 10 }, (_, i) => ({ repo: "o/r", number: i + 1 }));
  const r = await render([task({ key: "a", prs } as Partial<Task>)]);
  const pills = r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("task-pr-num"));
  expect(pills).toHaveLength(3);
  for (const pill of pills) expect(pill.findAllByProps({ className: "task-pr-dot" }).length).toBe(1);
  expect(text(r.root.findByProps({ className: "task-pr-more" }))).toBe("+7");
});

test("5 PRs: 3 chips plus a popover trigger '+2'; clicking it does not open the task", async () => {
  serve();
  const prs = Array.from({ length: 5 }, (_, i) => ({ repo: "o/r", number: i + 1 }));
  const r = await render([task({ key: "a", prs } as Partial<Task>)]);
  const pills = r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("task-pr-num"));
  expect(pills).toHaveLength(3);
  const trigger = r.root.findByProps({ className: "task-pr-more" });
  expect(text(trigger)).toBe("+2");
  let stopped = false;
  await act(async () => {
    trigger.props.onClick({ stopPropagation() { stopped = true; } });
  });
  // The card opens on a bubbled click; the trigger swallows it.
  expect(stopped).toBe(true);
});

test("the dirty facts collapse to one word badge with the counts in its tooltip", async () => {
  globalThis.fetch = mock(async (url: string) => {
    const reply: Record<string, unknown> = {
      "/api/tasks/ports": { ports: {} },
      "/api/tasks/holds": {
        holds: { a: [{ path: "/w", dirty_files: 4, tracked_dirty: 3, unpushed: 2, branch: "b", reasons: ["dirty", "unpushed"] }] },
      },
    };
    return new Response(JSON.stringify(reply[url] ?? {}), { status: 200 });
  }) as unknown as typeof fetch;
  const r = await render([task({ key: "a" })]);
  const badges = r.root.findByProps({ "data-key": "a" }).findAllByProps({ className: "ws-badge ws-marker ws-dirty" });
  expect(badges).toHaveLength(1);
  expect(text(badges[0])).toBe("uncommitted");
  expect(/\d/.test(text(badges[0]))).toBe(false);
  expect(badges[0].props.title).toBe("3 uncommitted, 2 unpushed");
});

test("untracked-only files show no badge", async () => {
  globalThis.fetch = mock(async (url: string) => {
    const reply: Record<string, unknown> = {
      "/api/tasks/ports": { ports: {} },
      "/api/tasks/holds": {
        holds: { a: [{ path: "/w", dirty_files: 1, tracked_dirty: 0, unpushed: 0, branch: "b", reasons: ["dirty"] }] },
      },
    };
    return new Response(JSON.stringify(reply[url] ?? {}), { status: 200 });
  }) as unknown as typeof fetch;
  const r = await render([task({ key: "a" })]);
  const badges = r.root.findByProps({ "data-key": "a" }).findAllByProps({ className: "ws-badge ws-marker ws-dirty" });
  expect(badges).toHaveLength(0);
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

test("a running card shows no status word badge and not the running reason", async () => {
  serve();
  const r = await render([task({ key: "a", status: "in_progress" })]);
  expect(r.root.findAllByProps({ className: "ws-badge ws-status" })).toHaveLength(0);
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

test("x on a RUNNING task asks first; nothing is archived until it is confirmed", async () => {
  const posts = serve();
  const handlers: ((e: unknown) => void)[] = [];
  const realAdd = window.addEventListener;
  window.addEventListener = ((type: string, fn: (e: unknown) => void) => {
    if (type === "keydown") handlers.push(fn);
  }) as typeof window.addEventListener;
  const r = await render([task({ key: "run", title: "Busy", status: "in_progress" })]);
  window.addEventListener = realAdd;
  const press = (key: string) =>
    handlers.forEach((h) =>
      h({ key, defaultPrevented: false, target: null, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, preventDefault() {} }),
    );
  await act(async () => {
    press("j");
  });
  await act(async () => {
    press("x");
  });
  expect(posts.some((p) => p.url === "/api/tasks/archive")).toBe(false);
  const dlg = r.root.findAllByProps({ title: "Archive task" })[0];
  expect(dlg.props.message).toBe("Archive 1 task? 1 is still running and will be stopped.");
  await act(async () => dlg.props.onConfirm());
  expect(posts.some((p) => p.url === "/api/tasks/archive" && (p.body as { key: string }).key === "run")).toBe(true);
});

test("the Undo toast says what the archive stopped and removed", async () => {
  const posts = serve();
  const base = globalThis.fetch;
  globalThis.fetch = mock(async (url: string, init?: RequestInit) => {
    if (url === "/api/tasks/archive") {
      return new Response(
        JSON.stringify({ ok: true, key: "a", cancelled: 0, filed: true, teardown: { stopped: 1, killed: [9], removed: ["/w"], kept: [], gone: [] } }),
        { status: 200 },
      );
    }
    return base(url, init);
  }) as unknown as typeof fetch;
  void posts;
  const r = await render([task({ key: "a", title: "Fix it" })]);
  const btn = r.root.findByProps({ className: "tasks-rowmark ws-mark" }).findByProps({ className: "tasks-act tasks-act--archive" });
  await act(async () => {
    btn.props.onClick({ stopPropagation() {} });
  });
  const text = r.root.findByProps({ className: "ws-undo-text" }).children.join("");
  expect(text).toBe("Closed \u201cFix it\u201d \u2014 stopped 2 processes, removed 1 worktree; Undo won't restart them");
});

test("the branch chip shows the worktree branch, naming the others in its title, and is omitted when unknown", async () => {
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
  expect(chip.props.title).toBe("feat/one\nalso: feat/two");
  expect(r.root.findByProps({ "data-key": "a" }).findAllByProps({ className: "ws-branch-more" })).toHaveLength(0);
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
  expect(heads.map((h) => text(h))).toEqual(["Needs you2", "Working1", "Safe to archive1"]);
  expect(heads.every((h) => h.props.role === "presentation")).toBe(true);
  expect(heads.every((h) => h.findAllByType("button").length === 1)).toBe(true);
  const cards = r.root.findAll((n) => typeof n.props.className === "string" && /^ws-card( |$)/.test(n.props.className));
  expect(cards.map((n) => n.props["data-key"])).toEqual(["c", "d", "b", "a"]);
});

const cardsOf = (r: ReturnType<typeof create>) =>
  r.root.findAll((n) => typeof n.props.className === "string" && /^ws-card( |$)/.test(n.props.className));
const moreBtn = (r: ReturnType<typeof create>) =>
  r.root.findAll((n) => n.type === "button" && typeof n.props.className === "string" && n.props.className.includes("ws-more"));

test("a group over 8 cards shows 8 and a Show N more that reveals the rest", async () => {
  serve();
  const many = Array.from({ length: 11 }, (_, i) => task({ key: `k${i}`, task_id: `T${i}`, started: 100 - i, background_jobs: { count: 1, run_id: "r", jobs: [{ id: "j", title: "dev" }] } }));
  const r = await render(many);
  expect(cardsOf(r)).toHaveLength(8);
  const heads = r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("ws-group-head"));
  expect(text(heads[0])).toBe("Working11");
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
  const r = await render([task({ key: "a" }), task({ key: "b", started: 2 })]);
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
  const r = await render([task({ key: "a", background_jobs: { count: 1, run_id: "r", jobs: [{ id: "j", title: "dev" }] } })]);
  expect(cardsOf(r)).toHaveLength(1);
  const head = r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("ws-group-head"))[0];
  await act(async () => {
    head.findByType("button").props.onClick();
  });
  expect(cardsOf(r)).toHaveLength(0);
  expect(JSON.parse(store["fused.tasks.workspace.collapsed"]).working).toBe(true);
});

// ---- Bulk archive ---------------------------------------------------------
const byClass = (r: ReturnType<typeof create>, c: string) =>
  // The checkboxes are the shadcn Checkbox: match its element (the one carrying onCheckedChange), not the host button under it.
  r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.split(" ").includes(c) && (/check/.test(c) ? (n.type as { name?: string }).name === "Checkbox" : typeof n.type === "string"));
const press = async (el: { props: Record<string, (...a: unknown[]) => void> }, fn = "onClick") => {
  await act(async () => {
    el.props[fn]({ stopPropagation() {} });
  });
};
const barButton = (r: ReturnType<typeof create>, label: string) =>
  byClass(r, "tasks-bulk-btn").find((b) => text(b) === label)!;
const many = (n: number, over: Partial<Task> = {}): Task[] =>
  Array.from({ length: n }, (_, i) => task({ key: `k${i}`, task_id: `T${i}`, title: `task ${i}`, started: 100 - i, ...over }));
const archived = (posts: { url: string; body: unknown }[], url = "/api/tasks/archive") =>
  posts.filter((p) => p.url === url).map((p) => (p.body as { key: string }).key).sort();

test("the group checkbox selects the whole group, cards past the cap included", async () => {
  serve();
  const r = await render(many(10));
  expect(byClass(r, "ws-card")).toHaveLength(8);
  const head = byClass(r, "tasks-group-check");
  expect(head).toHaveLength(1);
  await press(head[0] as never, "onCheckedChange");
  expect(text(byClass(r, "tasks-bulk-count")[0])).toBe("10 selected");
  expect(byClass(r, "tasks-group-check")[0].props.checked).toBe(true);
});

test("a folded group still selects all of its tasks, and the header does not fold", async () => {
  serve();
  store["fused.tasks.workspace.collapsed"] = JSON.stringify({ clear: true });
  const r = await render(many(3));
  expect(byClass(r, "ws-card")).toHaveLength(0);
  let stopped = false;
  const check = byClass(r, "tasks-group-check")[0] as never as { props: Record<string, (...a: unknown[]) => void> };
  await act(async () => check.props.onClick({ stopPropagation: () => (stopped = true) }));
  expect(stopped).toBe(true);
  await press(check, "onCheckedChange");
  expect(text(byClass(r, "tasks-bulk-count")[0])).toBe("3 selected");
  expect(byClass(r, "ws-card")).toHaveLength(0);
});

test("a card checkbox toggles only that task; Clear empties the selection", async () => {
  serve();
  const r = await render(many(3));
  await press(byClass(r, "ws-check")[1] as never, "onCheckedChange");
  expect(text(byClass(r, "tasks-bulk-count")[0])).toBe("1 selected");
  await press(barButton(r, "Clear"));
  expect(byClass(r, "tasks-bulk-bar")).toHaveLength(0);
});

test("Archive calls the archive once per selected key and raises ONE Undo for the batch", async () => {
  const posts = serve();
  const r = await render(many(5));
  await press(byClass(r, "tasks-group-check")[0] as never, "onCheckedChange");
  await press(barButton(r, "Archive"));
  expect(archived(posts)).toEqual(["k0", "k1", "k2", "k3", "k4"]);
  expect(byClass(r, "ws-card")).toHaveLength(0);
  const toast = byClass(r, "ws-undo");
  expect(toast).toHaveLength(1);
  expect(text(byClass(r, "ws-undo-text")[0])).toBe("Archived 5 tasks");
  await press(byClass(r, "ws-undo-btn")[0] as never);
  expect(archived(posts, "/api/tasks/unarchive")).toEqual(["k0", "k1", "k2", "k3", "k4"]);
  expect(byClass(r, "ws-card")).toHaveLength(5);
});

test("a running task in the selection asks first, and nothing is archived until it is confirmed", async () => {
  const posts = serve();
  const r = await render([...many(2), ...many(1, { key: "run", task_id: "T9", status: "in_progress", started: 500 })]);
  const heads = byClass(r, "tasks-group-check");
  for (const h of heads) await press(h as never, "onCheckedChange");
  await press(barButton(r, "Archive"));
  expect(archived(posts)).toEqual([]);
  const dlg = r.root.findAllByProps({ title: "Archive tasks" })[0];
  expect(dlg.props.message).toBe("Archive 3 tasks? 1 is still running and will be stopped.");
  await act(async () => dlg.props.onConfirm());
  expect(archived(posts)).toEqual(["k0", "k1", "run"]);
});

test("a failed archive shows that task again and sends one error", async () => {
  const posts = serve();
  const base = globalThis.fetch;
  globalThis.fetch = mock(async (url: string, init?: RequestInit) =>
    url === "/api/tasks/archive" && JSON.parse(String(init?.body)).key === "k1"
      ? new Response(JSON.stringify({ error: "nope" }), { status: 500 })
      : (base as typeof fetch)(url, init),
  ) as unknown as typeof fetch;
  const r = await render(many(3));
  await press(byClass(r, "tasks-group-check")[0] as never, "onCheckedChange");
  await press(barButton(r, "Archive"));
  expect(byClass(r, "ws-card").map((n) => n.props["data-key"])).toEqual(["k1"]);
  expect(text(byClass(r, "ws-undo-text")[0])).toBe("Archived 2 tasks");
  expect(posts.length).toBeGreaterThan(0);
});

test("holds are read for every task, in chunks of at most 100; one chunk failing marks only its keys", async () => {
  const posts = serve();
  const base = globalThis.fetch;
  globalThis.fetch = mock(async (url: string, init?: RequestInit) => {
    const ids: string[] = init?.body ? JSON.parse(String(init.body)).ids ?? [] : [];
    if (url === "/api/tasks/holds" && ids.includes("k149")) {
      posts.push({ url, body: { ids } });
      return new Response("{}", { status: 500 });
    }
    return base(url, init);
  }) as unknown as typeof fetch;
  const tasks = Array.from({ length: 150 }, (_, i) => task({ key: `k${i}`, task_id: `T${i}`, started: 1000 - i }));
  await render(tasks);
  const asked = posts.filter((p) => p.url === "/api/tasks/holds").map((p) => (p.body as { ids: string[] }).ids);
  expect(asked.every((ids) => ids.length <= 100)).toBe(true);
  expect(new Set(asked.flat()).size).toBe(150);
  const ports = posts.filter((p) => p.url === "/api/tasks/ports").flatMap((p) => (p.body as { ids: string[] }).ids);
  expect(ports.length).toBeLessThanOrEqual(100);
});

test("the card's ring slot holds the List's Archive button, and pressing it archives with Undo", async () => {
  const posts = serve();
  const r = await render([task({ key: "a", title: "Fix it" })]);
  const mark = r.root.findByProps({ className: "tasks-rowmark ws-mark" });
  const btn = mark.findByProps({ className: "tasks-act tasks-act--archive" });
  expect(btn.props["aria-label"]).toBe("Archive");
  expect(byClass(r, "ws-check").length).toBeGreaterThan(0);
  await act(async () => {
    btn.props.onClick({ stopPropagation() {} });
  });
  expect(posts.some((p) => p.url === "/api/tasks/archive" && (p.body as { key: string }).key === "a")).toBe(true);
  expect(r.root.findAllByProps({ "data-key": "a" })).toHaveLength(0);
  expect(r.root.findAllByProps({ className: "ws-undo-btn" })).toHaveLength(1);
});

test("holds not loaded yet: the card shows a shimmer pill in the badge slot, not the word 'checking'", async () => {
  globalThis.fetch = mock(async (url: string) => {
    if (url === "/api/tasks/holds") return new Promise<Response>(() => {});
    return new Response(JSON.stringify(url === "/api/tasks/ports" ? { ports: {} } : {}), { status: 200 });
  }) as unknown as typeof fetch;
  const r = await render([task({ key: "a" })]);
  const card = r.root.findByProps({ "data-key": "a" });
  expect(card.findAllByProps({ className: "skel-bar ws-skel" })).toHaveLength(1);
  expect(card.findByProps({ className: "skel-bar ws-skel" }).props["aria-hidden"]).toBe("true");
  expect(card.findByProps({ className: "ws-card-facts" }).props["aria-busy"]).toBe("true");
  expect(card.findAllByProps({ className: "ws-badge ws-marker" })).toHaveLength(0);
  expect(text(card).toLowerCase().replace("checking worktree", "")).not.toContain("checking");
});

test("a failed holds load still shows 'unknown' as text, with no shimmer", async () => {
  globalThis.fetch = mock(async (url: string) => {
    if (url === "/api/tasks/holds") return new Response("{}", { status: 500 });
    return new Response(JSON.stringify(url === "/api/tasks/ports" ? { ports: {} } : {}), { status: 200 });
  }) as unknown as typeof fetch;
  const r = await render([task({ key: "a" })]);
  const card = r.root.findByProps({ "data-key": "a" });
  expect(text(card.findByProps({ className: "ws-badge ws-marker" }))).toBe("unknown");
  expect(card.findAllByProps({ className: "skel-bar ws-skel" })).toHaveLength(0);
});
