import { describe, expect, test } from "bun:test";

import {
  LOCAL_COMMANDS,
  classifyCommand,
  fetchCommands,
  filterCommands,
  menuCommands,
} from "./commands";

describe("classifyCommand", () => {
  const known = ["compact", "review", "plugin:deploy"];

  test("not a command: prose, an empty box, a path", () => {
    expect(classifyCommand("hello", known)).toBeNull();
    expect(classifyCommand("", known)).toBeNull();
    expect(classifyCommand("/", known)).toBeNull();
    expect(classifyCommand("/Users/me/file.py is broken", known)).toBeNull();
    expect(classifyCommand("please /compact", known)).toBeNull();
  });

  test("local names are local whatever the list says", () => {
    expect(classifyCommand("/clear", known)).toEqual({ kind: "local", name: "clear", arg: "" });
    expect(classifyCommand("  /model opus ", known)).toEqual({ kind: "local", name: "model", arg: "opus" });
    expect(classifyCommand("/worktree feature-x", ["worktree"])).toEqual({
      kind: "local",
      name: "worktree",
      arg: "feature-x",
    });
    for (const c of LOCAL_COMMANDS) expect(classifyCommand("/" + c.name)?.kind).toBe("local");
  });

  test("a known name is cli, with its argument (newlines kept)", () => {
    expect(classifyCommand("/compact", known)).toEqual({ kind: "cli", name: "compact", arg: "" });
    expect(classifyCommand("/review the\nwhole diff", new Set(known))).toEqual({
      kind: "cli",
      name: "review",
      arg: "the\nwhole diff",
    });
    expect(classifyCommand("/plugin:deploy", known)?.kind).toBe("cli");
  });

  test("an unknown name is unknown (still sent verbatim by the caller)", () => {
    expect(classifyCommand("/nope", known)).toEqual({ kind: "unknown", name: "nope", arg: "" });
    expect(classifyCommand("/nope")?.kind).toBe("unknown");
  });
});

describe("filterCommands", () => {
  const list = [{ name: "review" }, { name: "compact" }, { name: "context" }, { name: "cost" }, { name: "pr-comments" }];

  test("empty query is the whole list, in order", () => {
    expect(filterCommands(list, "").map((r) => r.name)).toEqual(list.map((r) => r.name));
    expect(filterCommands(list, "/").map((r) => r.name)).toEqual(list.map((r) => r.name));
  });

  test("prefix matches first, then substring, each stable", () => {
    expect(filterCommands(list, "co").map((r) => r.name)).toEqual(["compact", "context", "cost", "pr-comments"]);
    expect(filterCommands(list, "e").map((r) => r.name)).toEqual(["review", "context", "pr-comments"]);
    expect(filterCommands(list, "/REV").map((r) => r.name)).toEqual(["review"]);
    expect(filterCommands(list, "zzz")).toEqual([]);
  });
});

describe("menuCommands", () => {
  test("local first, cli after, no name twice", () => {
    const rows = menuCommands([
      { name: "compact", description: "Compact", source: "builtin" },
      { name: "clear", description: "dup", source: "builtin" },
      { name: "/review", description: "", source: "builtin" },
      { name: "compact", description: "again", source: "x" },
    ]);
    expect(rows.slice(0, LOCAL_COMMANDS.length).every((r) => r.local)).toBe(true);
    expect(rows.slice(LOCAL_COMMANDS.length).map((r) => [r.name, r.local])).toEqual([
      ["compact", false],
      ["review", false],
    ]);
    expect(rows.filter((r) => r.name === "clear")).toHaveLength(1);
    expect(menuCommands(null)).toHaveLength(LOCAL_COMMANDS.length);
  });
});

describe("fetchCommands", () => {
  test("posts the commands action and normalises the answer", async () => {
    const real = globalThis.fetch;
    let body: Record<string, unknown> = {};
    let url = "";
    (globalThis as { fetch: unknown }).fetch = (u: string, init: RequestInit) => {
      url = u;
      body = JSON.parse(String(init.body));
      return Promise.resolve(
        new Response(JSON.stringify({ commands: [{ name: "a", description: "", source: "x" }], from: "cli" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    };
    try {
      const out = await fetchCommands("/repo");
      expect(url).toBe("/api/claude/agent");
      expect(body).toEqual({ action: "commands", file: "/repo" });
      expect(out).toEqual({ commands: [{ name: "a", description: "", source: "x" }], agents: [], stale: false, from: "cli" });
    } finally {
      (globalThis as { fetch: unknown }).fetch = real;
    }
  });

  test("a handler error throws", async () => {
    const real = globalThis.fetch;
    (globalThis as { fetch: unknown }).fetch = () =>
      Promise.resolve(new Response(JSON.stringify({ error: "nope" }), { status: 200, headers: { "content-type": "application/json" } }));
    try {
      await expect(fetchCommands("/repo")).rejects.toThrow("nope");
    } finally {
      (globalThis as { fetch: unknown }).fetch = real;
    }
  });
});
