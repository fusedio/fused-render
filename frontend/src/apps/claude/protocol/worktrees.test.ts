import { describe, expect, test } from "bun:test";

import {
  createAndTarget,
  currentWorktree,
  defaultWorktreeName,
  switchTarget,
  dirtyState,
  isDirtyRefusal,
  removeLabel,
  parseWorktreeCommand,
  resolveWorktree,
  triggerLabel,
  validWorktreeName,
  worktreeRows,
  listWorktrees,
  type WorktreeList,
} from "./worktrees";

const LIST: WorktreeList = {
  root: "/r",
  current: "/r",
  branch_current: "main",
  worktrees: [
    { path: "/r/.claude/worktrees/zed", branch: "zed", head: "b", name: "zed", managed: true, is_main: false, dirty: true },
    { path: "/r", branch: "main", head: "a", name: "r", managed: false, is_main: true, dirty: false },
    { path: "/elsewhere/alpha", branch: "alpha", head: "c", name: "alpha", managed: false, is_main: false, dirty: null },
  ],
};

describe("worktree row model", () => {
  test("main first, then by name; current marked; remove only on managed", () => {
    const rows = worktreeRows(LIST);
    expect(rows.map((r) => r.name)).toEqual(["r", "alpha", "zed"]);
    expect(rows.map((r) => r.current)).toEqual([true, false, false]);
    expect(rows.map((r) => r.removable)).toEqual([false, false, true]);
    expect(rows.map((r) => r.dirty)).toEqual([false, false, true]);
  });

  test("trigger: branch on the main checkout, name in a worktree", () => {
    expect(triggerLabel(LIST)).toBe("main");
    const inZed = { ...LIST, current: "/r/.claude/worktrees/zed/" };
    expect(currentWorktree(inZed)?.name).toBe("zed");
    expect(triggerLabel(inZed)).toBe("zed");
    expect(worktreeRows(inZed).find((r) => r.current)?.name).toBe("zed");
  });

  test("default name is claude-<yymmdd>-<hhmm>", () => {
    expect(defaultWorktreeName(new Date(2026, 9, 3, 7, 5))).toBe("claude-261003-0705");
    expect(validWorktreeName(defaultWorktreeName())).toBe(true);
  });
});

describe("/worktree parse", () => {
  test("no arg opens, a good name is taken, a bad one is refused", () => {
    expect(parseWorktreeCommand("")).toEqual({ open: true });
    expect(parseWorktreeCommand("  ")).toEqual({ open: true });
    expect(parseWorktreeCommand(" feat-1.x_y ")).toEqual({ name: "feat-1.x_y" });
    for (const bad of ["..", ".", "a b", "a/b", "x".repeat(65)]) {
      expect("error" in parseWorktreeCommand(bad)).toBe(true);
    }
  });
});

describe("resolveWorktree", () => {
  test("switches to an existing one without creating", async () => {
    let made = 0;
    const out = await resolveWorktree("/r", "zed", {
      list: async () => LIST,
      create: async () => {
        made += 1;
        return { path: "x", branch: "x" };
      },
    });
    expect(out).toEqual({ path: "/r/.claude/worktrees/zed" });
    expect(made).toBe(0);
  });

  test("creates a missing one; errors pass through", async () => {
    const out = await resolveWorktree("/r", "new", {
      list: async () => LIST,
      create: async (_f, name) => ({ path: "/r/.claude/worktrees/" + name, branch: name }),
    });
    expect(out).toEqual({ path: "/r/.claude/worktrees/new" });
    expect(await resolveWorktree("/x", "a", { list: async () => ({ error: "not a git repository" }) })).toEqual({
      error: "not a git repository",
    });
  });
});

describe("transport", () => {
  test("list posts {action, file} to the worktrees route", async () => {
    const real = globalThis.fetch;
    let seen: { url: string; body: unknown; fused: string | null } = { url: "", body: null, fused: null };
    (globalThis as { fetch: unknown }).fetch = (url: string, init: RequestInit) => {
      seen = { url, body: JSON.parse(String(init.body)), fused: new Headers(init.headers).get("X-Fused-Page") };
      return Promise.resolve(new Response(JSON.stringify(LIST), { status: 200, headers: { "content-type": "application/json" } }));
    };
    try {
      expect(await listWorktrees("/r/app")).toEqual(LIST);
      expect(seen.url).toBe("/api/claude/worktrees");
      expect(seen.body).toEqual({ action: "list", file: "/r/app" });
      expect(decodeURIComponent(seen.fused ?? "")).toBe("fused-render://claude");
    } finally {
      (globalThis as { fetch: unknown }).fetch = real;
    }
  });
});

describe("review fixes", () => {
  test("/worktree <current> is reported as current, not a switch", async () => {
    expect(await resolveWorktree("/r", "r", { list: async () => LIST })).toEqual({ path: "/r", current: true });
    const inZed = { ...LIST, current: "/r/.claude/worktrees/zed" };
    expect(await resolveWorktree("/r", "zed", { list: async () => inZed })).toEqual({
      path: "/r/.claude/worktrees/zed",
      current: true,
    });
  });

  test("dirty: null is possibly dirty, and the confirm says so", () => {
    const rows = worktreeRows(LIST);
    const alpha = rows.find((r) => r.name === "alpha")!;
    expect(alpha.dirty).toBe(false);
    expect(alpha.maybeDirty).toBe(true);
    expect(dirtyState(alpha)).toBe("unknown");
    expect(removeLabel(true, "unknown")).toBe("May have uncommitted changes. Remove anyway");
    expect(removeLabel(true, "dirty")).toBe("Has uncommitted changes. Remove anyway");
    expect(removeLabel(true, "clean")).toBe("Confirm remove");
    expect(removeLabel(false, "dirty")).toBe("Remove this worktree");
  });

  test("the server's dirty refusals are recognised", () => {
    expect(isDirtyRefusal("this worktree has uncommitted changes")).toBe(true);
    expect(isDirtyRefusal("could not tell whether this worktree has changes")).toBe(true);
    expect(isDirtyRefusal("only worktrees under .claude/worktrees/ can be removed")).toBe(false);
  });
});

describe("resolveWorktree prefers a managed worktree by name", () => {
  const SHADOWED: WorktreeList = {
    root: "/r",
    current: "/r",
    branch_current: "main",
    worktrees: [
      { path: "/r", branch: "main", head: "a", name: "r", managed: false, is_main: true, dirty: false },
      { path: "/hand/feat", branch: "feat", head: "b", name: "feat", managed: false, is_main: false, dirty: false },
      { path: "/r/.claude/worktrees/feat", branch: "feat-2", head: "c", name: "feat", managed: true, is_main: false, dirty: false },
      { path: "/r/.claude/worktrees/r", branch: "r", head: "d", name: "r", managed: true, is_main: false, dirty: false },
    ],
  };
  test("over a hand-made worktree of the same name", async () => {
    expect(await resolveWorktree("/r", "feat", { list: async () => SHADOWED })).toEqual({
      path: "/r/.claude/worktrees/feat",
    });
  });
  test("over the main checkout named like the repo folder", async () => {
    expect(await resolveWorktree("/r", "r", { list: async () => SHADOWED })).toEqual({
      path: "/r/.claude/worktrees/r",
    });
  });
});

describe("a chat on a FILE switches to the same file in the other checkout", () => {
  const FILE_LIST: WorktreeList = {
    root: "/r",
    current: "/r",
    branch_current: "main",
    relative: "src/app.py",
    worktrees: [
      { path: "/r", branch: "main", head: "a", name: "r", managed: false, is_main: true, dirty: false, mapped: { path: "/r/src/app.py", exists: true } },
      { path: "/r/.claude/worktrees/has", branch: "has", head: "b", name: "has", managed: true, is_main: false, dirty: false, mapped: { path: "/r/.claude/worktrees/has/src/app.py", exists: true } },
      { path: "/r/.claude/worktrees/lacks", branch: "lacks", head: "c", name: "lacks", managed: true, is_main: false, dirty: false, mapped: { path: "/r/.claude/worktrees/lacks/src/app.py", exists: false } },
    ],
  };

  test("switchTarget: the mapped path when it exists, else the worktree root", () => {
    expect(switchTarget(FILE_LIST.worktrees[1]!)).toBe("/r/.claude/worktrees/has/src/app.py");
    expect(switchTarget(FILE_LIST.worktrees[2]!)).toBe("/r/.claude/worktrees/lacks");
    // An older server sends no `mapped`: the root, as before.
    expect(switchTarget({ path: "/r/x" })).toBe("/r/x");
  });

  test("rows carry the target, and flag a file that is not on the branch", () => {
    const rows = worktreeRows(FILE_LIST);
    expect(rows.map((r) => [r.name, r.target, r.missing])).toEqual([
      ["r", "/r/src/app.py", false],
      ["has", "/r/.claude/worktrees/has/src/app.py", false],
      ["lacks", "/r/.claude/worktrees/lacks", true],
    ]);
    // A folder-root target never says "missing".
    expect(worktreeRows({ ...FILE_LIST, relative: "" }).some((r) => r.missing)).toBe(false);
  });

  test("/worktree switches to the mapped file; a create lands on the new row's file", async () => {
    expect(await resolveWorktree("/r/src/app.py", "has", { list: async () => FILE_LIST })).toEqual({
      path: "/r/.claude/worktrees/has/src/app.py",
    });
    const after: WorktreeList = {
      ...FILE_LIST,
      worktrees: [
        ...FILE_LIST.worktrees,
        { path: "/r/.claude/worktrees/new", branch: "new", head: "d", name: "new", managed: true, is_main: false, dirty: false, mapped: { path: "/r/.claude/worktrees/new/src/app.py", exists: true } },
      ],
    };
    let lists = 0;
    const out = await resolveWorktree("/r/src/app.py", "new", {
      list: async () => (lists++ === 0 ? FILE_LIST : after),
      create: async () => ({ path: "/r/.claude/worktrees/new", branch: "new" }),
    });
    expect(out).toEqual({ path: "/r/.claude/worktrees/new/src/app.py" });
    // The re-read failing falls back to the new worktree's root.
    expect(
      await createAndTarget("/r/src/app.py", "new", {
        list: async () => ({ error: "boom" }),
        create: async () => ({ path: "/r/.claude/worktrees/new", branch: "new" }),
      }),
    ).toEqual({ path: "/r/.claude/worktrees/new" });
  });
});
