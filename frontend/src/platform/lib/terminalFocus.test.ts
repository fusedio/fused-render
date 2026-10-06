// terminalFocus: the focus report and the metadata-only hint. fetch is stubbed
// per test; nothing is kept at module scope but the saved original.
import { afterEach, beforeEach, expect, test } from "bun:test";

import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

const {
  reportFocusedTerminal,
  terminalHint,
  terminalHintObject,
  askClaudeTerminalPrompt,
  askClaudeSelectionPrompt,
  fixTerminalFailurePrompt,
  parseTerminalAsk,
  resetTerminalFocusForTests,
} = await import("@platform/lib/terminalFocus");

const realFetch = globalThis.fetch;
let calls: { url: string; method: string; body?: string }[] = [];
let listing: unknown = { sessions: [] };

beforeEach(() => {
  calls = [];
  listing = { sessions: [] };
  resetTerminalFocusForTests();
  globalThis.fetch = (async (url: string, init?: { method?: string; body?: string }) => {
    calls.push({ url: String(url), method: init?.method ?? "GET", body: init?.body });
    const data = init?.method === "PUT" ? { ok: true } : listing;
    return { ok: true, status: 200, json: async () => data } as Response;
  }) as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

const sess = (over: Record<string, unknown> = {}) => ({
  id: "t1",
  alive: true,
  shell: "zsh",
  cwd: "/p",
  lastCommand: "ls",
  lastExit: 0,
  lastActivity: 990,
  ...over,
});

test("reporting a tab PUTs its id once and not again for a repeat", async () => {
  reportFocusedTerminal("t2", "zsh");
  reportFocusedTerminal("t2", "zsh");
  reportFocusedTerminal(null);
  await Promise.resolve();
  const puts = calls.filter((c) => c.method === "PUT");
  expect(puts.map((c) => c.url)).toEqual(["/api/terminal/focus", "/api/terminal/focus"]);
  expect(puts.map((c) => JSON.parse(c.body!))).toEqual([{ id: "t2" }, { id: null }]);
});

test("no live terminal means no hint", async () => {
  expect(await terminalHint()).toBe("");
  listing = { sessions: [sess({ alive: false })] };
  expect(await terminalHint()).toBe("");
});

test("the hint is the focused tab's metadata, never contents", async () => {
  listing = { sessions: [sess(), sess({ id: "t2", cwd: "/q", lastCommand: "make" })] };
  reportFocusedTerminal("t2", "build");
  const hint = await terminalHintObject(1000);
  expect(hint).toEqual({ id: "t2", title: "build", cwd: "/q", lastCommand: "make", lastExit: 0, ageSec: 10 });
  expect(Object.keys(hint!)).not.toContain("text");
});

test("falls back to the server's focused flag, then to a lone terminal", async () => {
  listing = { sessions: [sess(), sess({ id: "t2", focused: true })] };
  expect((await terminalHintObject(1000))?.id).toBe("t2");
  listing = { sessions: [sess()] };
  expect((await terminalHintObject(1000))?.id).toBe("t1");
});

test("several terminals and no known focus gives no hint", async () => {
  listing = { sessions: [sess(), sess({ id: "t2" })] };
  expect(await terminalHint()).toBe("");
});

test("an unreachable list gives no hint instead of throwing", async () => {
  globalThis.fetch = (async () => {
    throw new Error("down");
  }) as unknown as typeof fetch;
  expect(await terminalHint()).toBe("");
});

test("the Ask Claude prompt names the terminal for terminal_read", () => {
  const p = askClaudeTerminalPrompt({ id: "t3", label: "zsh", cwd: "/x" });
  expect(p).toContain("[terminal t3: zsh — /x]");
  expect(p).toContain('terminal_read with id "t3"');
});

const tabA = { id: "t3", label: "zsh", cwd: "/w" };
test("selection prompt: header with and without cwd", () => {
  expect(
    askClaudeSelectionPrompt(tabA, "boom").startsWith("[terminal t3: zsh — /w]\nSelected from this terminal:\n```text\nboom\n```\n"),
  ).toBe(true);
  expect(askClaudeSelectionPrompt({ id: "t3", label: "zsh" }, "x").startsWith("[terminal t3: zsh]\n")).toBe(true);
  expect(askClaudeSelectionPrompt(tabA, "x")).toContain('terminal_read with id "t3"');
});
test("selection prompt: fence escalates past backtick runs", () => {
  expect(askClaudeSelectionPrompt(tabA, "a ```` b")).toContain("`````text\na ```` b\n`````\n");
});
test("selection prompt: trims trailing whitespace and blank lines", () => {
  expect(askClaudeSelectionPrompt(tabA, "a  \nb\t\n\n  \n")).toContain("```text\na\nb\n```");
});
test("selection prompt: truncation keeps the tail", () => {
  const p = askClaudeSelectionPrompt(tabA, "HEAD" + "x".repeat(7000) + "TAIL");
  expect(p).toContain("```text\n…(truncated)\n");
  expect(p).toContain("xTAIL\n```");
  expect(p).not.toContain("HEAD");
});
test("fix prompt: command, empty command, backticks", () => {
  expect(fixTerminalFailurePrompt(tabA, { command: "ls /nope", exitCode: 2 })).toBe(
    '[terminal t3: zsh — /w]\n`ls /nope` failed with exit code 2. Read its output with terminal_read (id "t3"), explain why it failed, and propose a fix. Do not type into the terminal or run anything yet.',
  );
  expect(fixTerminalFailurePrompt({ id: "t3", label: "zsh" }, { command: "", exitCode: 1 })).toContain(
    "[terminal t3: zsh]\nThe last command failed with exit code 1. Read",
  );
  expect(fixTerminalFailurePrompt(tabA, { command: "echo `x`", exitCode: 1 })).toContain("`` echo `x` `` failed");
});

// parseTerminalAsk: the chat draws a terminal ask as a chip + selection block.
test("parseTerminalAsk round-trips the tab prompt, with and without cwd", () => {
  const a = parseTerminalAsk(askClaudeTerminalPrompt({ id: "t1", label: "zsh", cwd: "/a/b" }))!;
  expect(a.id).toBe("t1");
  expect(a.label).toBe("zsh");
  expect(a.cwd).toBe("/a/b");
  expect(a.selection).toBeUndefined();
  expect(a.body).toContain("terminal_read");
  const b = parseTerminalAsk(askClaudeTerminalPrompt({ id: "t1", label: "zsh" }))!;
  expect(b.cwd).toBeUndefined();
  expect(b.label).toBe("zsh");
});

test("parseTerminalAsk round-trips selections, including a 4-backtick fence and truncation", () => {
  const tab = { id: "cc0c", label: "zsh", cwd: "/x/y" };
  const plain = parseTerminalAsk(askClaudeSelectionPrompt(tab, "line1\nline2"))!;
  expect(plain.selection).toBe("line1\nline2");
  expect(plain.body.startsWith("Explain this.")).toBe(true);
  const fenced = parseTerminalAsk(askClaudeSelectionPrompt(tab, "a\n```\nb"))!;
  expect(fenced.selection).toBe("a\n```\nb");
  expect(fenced.body.startsWith("Explain this.")).toBe(true);
  const long = parseTerminalAsk(askClaudeSelectionPrompt({ id: "t", label: "zsh" }, "x".repeat(9000)))!;
  expect(long.selection!.startsWith("…(truncated)\n")).toBe(true);
  expect(long.cwd).toBeUndefined();
});

test("parseTerminalAsk round-trips the fix prompt", () => {
  const f = parseTerminalAsk(fixTerminalFailurePrompt({ id: "t2", label: "bash", cwd: "/p" }, { command: "ls /nope", exitCode: 2 }))!;
  expect(f.id).toBe("t2");
  expect(f.selection).toBeUndefined();
  expect(f.body).toContain("failed with exit code 2");
});

test("parseTerminalAsk is null for anything else", () => {
  expect(parseTerminalAsk("hello there")).toBeNull();
  expect(parseTerminalAsk("[terminal t1: zsh\nbody")).toBeNull();
  expect(parseTerminalAsk("intro\n[terminal t1: zsh]\nbody")).toBeNull();
  expect(parseTerminalAsk("[terminal ]\nbody")).toBeNull();
});
