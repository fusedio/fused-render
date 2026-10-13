import { describe, expect, test } from "bun:test";

import {
  currentWorktree,
  defaultWorktreeName,
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
