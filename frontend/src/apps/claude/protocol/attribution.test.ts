// THE CHAT'S TRANSPORT: the three routes it posts to, the call log's headers
// on every one of them (SPEC CL-5, `fused_render/calls.py:75-86`), and how an
// answer maps onto a result or a thrown error.
//
// The headers are observability only, which is precisely why they need a
// test: nothing else would ever notice them break. The names are a CONTRACT
// with `calls.py`, which reads them lower-cased, and both path values are
// percent-encoded because `_header_path` decodes them on the way in.
import { afterEach, beforeEach, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const { runHeaders } = await import("@platform/lib/api");
const { AgentError, CLAUDE_PAGE_ID, runAgent, runAppEntry, runArtifacts, fetchTerminalCommand } =
  await import("./agent");

interface Sent {
  url: string;
  headers: Record<string, string>;
  body: unknown;
}
let sent: Sent[] = [];
let reply: { status: number; body: unknown } = { status: 200, body: {} };
const realFetch = globalThis.fetch;

beforeEach(() => {
  sent = [];
  reply = { status: 200, body: {} };
  (globalThis as { fetch: unknown }).fetch = async (
    input: unknown,
    init?: { headers?: Record<string, string>; body?: string },
  ): Promise<Response> => {
    sent.push({
      url: String(typeof input === "string" ? input : (input as { url: string }).url),
      headers: { ...(init?.headers ?? {}) },
      body: init?.body ? JSON.parse(init.body) : undefined,
    });
    const { status, body } = reply;
    return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
  };
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

// ---- the builder ----------------------------------------------------------

test("runHeaders spells the three exactly as calls.py reads them", () => {
  expect(runHeaders({ page: "/w/p/.claude/template.html", target: "/w/p", callId: "c1" })).toEqual({
    "X-Fused-Page": encodeURIComponent("/w/p/.claude/template.html"),
    "X-Fused-Target": encodeURIComponent("/w/p"),
    "X-Fused-Call": "c1",
  });
});

test("the PATH headers are percent-encoded (`_header_path`'s contract)", () => {
  const h = runHeaders({ page: "/w/a b/t.html", target: "/w/a b/x.md" });
  expect(h["X-Fused-Page"]).toBe("%2Fw%2Fa%20b%2Ft.html");
  expect(h["X-Fused-Target"]).toBe("%2Fw%2Fa%20b%2Fx.md");
});

test("no page, no headers at all — `X-Fused-Page` is what makes it an app call", () => {
  // `calls.py:76` — "X-Fused-Page is what makes a request an 'app call' at all".
  expect(runHeaders(undefined)).toEqual({});
  expect(runHeaders({ page: "" })).toEqual({});
});

test("the optional three are omitted rather than sent empty", () => {
  expect(runHeaders({ page: "/w/t.html" })).toEqual({
    "X-Fused-Page": encodeURIComponent("/w/t.html"),
  });
  expect(runHeaders({ page: "/w/t.html", target: null })).toEqual({
    "X-Fused-Page": encodeURIComponent("/w/t.html"),
  });
});

// ---- what an actual agent call sends ----------------------------------------

test("a poll posts {action, ...fields} and carries the page, the target and a call id", async () => {
  await runAgent("poll", { run_id: "r1" } as never, { target: "/w/p" });
  expect(sent).toHaveLength(1);
  expect(sent[0]!.url).toBe("/api/claude/agent");
  expect(sent[0]!.body).toEqual({ action: "poll", run_id: "r1" });
  const h = sent[0]!.headers;
  // ONE constant page for every chat call — `fused_render.claude_agent.CLAUDE_PAGE_ID`.
  expect(CLAUDE_PAGE_ID).toBe("fused-render://claude");
  expect(h["X-Fused-Page"]).toBe(encodeURIComponent(CLAUDE_PAGE_ID));
  expect(h["X-Fused-Target"]).toBe(encodeURIComponent("/w/p"));
  expect(h["X-Fused-Call"]).toBeTruthy();
  expect(h["X-Fused-Supersedes"]).toBeUndefined();
  // And the two fixed headers are untouched by the additions.
  expect(h["X-Fused"]).toBe("1");
  expect(h["Content-Type"]).toBe("application/json");
});

test("every call gets its OWN id", async () => {
  await runAgent("poll", {} as never);
  await runAgent("poll", {} as never);
  expect(sent[0]!.headers["X-Fused-Call"]).not.toBe(sent[1]!.headers["X-Fused-Call"]);
});

test("no target given: the page still attributes the call", async () => {
  await runAgent("snapshots", {} as never);
  expect(sent[0]!.headers["X-Fused-Page"]).toBeTruthy();
  expect(sent[0]!.headers["X-Fused-Target"]).toBeUndefined();
});

test("the app entry and the artifacts reader have routes of their own", async () => {
  reply = { status: 200, body: { entry: "/w/p/index.html" } };
  expect(await runAppEntry("/w/p")).toEqual({ entry: "/w/p/index.html" });
  reply = { status: 200, body: { artifacts: [] } };
  expect(await runArtifacts({ action: "list", file: "/w/p" })).toEqual({ artifacts: [] });
  expect(sent.map((s) => s.url)).toEqual(["/api/claude/app-entry", "/api/claude/artifacts"]);
  expect(sent[0]!.body).toEqual({ dir: "/w/p" });
  expect(sent[1]!.body).toEqual({ action: "list", file: "/w/p" });
  for (const s of sent) expect(s.headers["X-Fused-Page"]).toBe(encodeURIComponent(CLAUDE_PAGE_ID));
});

// ---- what an answer maps to -------------------------------------------------

test("a 200 IS the result — a handler's own {error} comes back, not thrown", async () => {
  reply = { status: 200, body: { error: "folder is busy" } };
  expect(await runAgent("start", {} as never)).toEqual({ error: "folder is busy" } as never);
});

test("a 500's structured error becomes an AgentError with its fields", async () => {
  reply = {
    status: 500,
    body: { error: { type: "KeyError", message: "'run_id'", traceback: "Traceback…" } },
  };
  const err = await runAgent("poll", {} as never).catch((e: unknown) => e);
  expect(err).toBeInstanceOf(AgentError);
  expect((err as InstanceType<typeof AgentError>).type).toBe("KeyError");
  expect((err as Error).message).toBe("'run_id'");
  expect((err as InstanceType<typeof AgentError>).traceback).toBe("Traceback…");
});

test("a 504 is an AgentError of type Timeout", async () => {
  reply = { status: 504, body: { error: { type: "Timeout", message: "poll exceeded 20 s" } } };
  const err = await runAgent("poll", {} as never).catch((e: unknown) => e);
  expect(err).toBeInstanceOf(AgentError);
  expect((err as InstanceType<typeof AgentError>).type).toBe("Timeout");
  expect((err as Error).message).toBe("poll exceeded 20 s");
});

test("a string error (400 bad action, 403) stays the platform's HttpError", async () => {
  reply = { status: 400, body: { error: "unknown action: nope" } };
  const err = await runAgent("poll", {} as never).catch((e: unknown) => e);
  expect(err).not.toBeInstanceOf(AgentError);
  expect((err as Error).message).toBe("unknown action: nope");
  expect((err as { status?: number }).status).toBe(400);
});

test("fetchTerminalCommand returns the command, and throws a handler's error", async () => {
  reply = { status: 200, body: { command: "cd '/w/p' && claude --resume s1", cwd: "/w/p" } };
  expect(await fetchTerminalCommand("/w/p", "s1")).toBe("cd '/w/p' && claude --resume s1");
  expect(sent[0]!.body).toEqual({ action: "terminal_command", file: "/w/p", session_id: "s1" });
  reply = { status: 200, body: { error: "no claude on PATH" } };
  await expect(fetchTerminalCommand("/w/p", "s1")).rejects.toThrow("no claude on PATH");
});
