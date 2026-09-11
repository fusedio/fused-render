// THE CALL LOG'S FOUR HEADERS (SPEC CL-5, `fused_render/calls.py:75-86`).
//
// Flag-on, a chat's calls were anonymous: `runtime.js` builds these off the
// EMBEDDED PAGE's own URL (R:1434-1448 — `ownQuery("path")`,
// `ownQuery("_file")`), and a native chat has no such URL, so nothing set them.
// `fused-render calls`, `--page <chat template>` and the `.calls.jsonl` viewer
// all showed an empty history for a conversation, and the chat's failed-call
// digests went with it.
//
// Observability only, which is precisely why it needed a test: nothing else
// would ever notice it break. The names are a CONTRACT with `calls.py`, which
// reads them lower-cased, and both path values are percent-encoded because
// `_header_path` decodes them on the way in.
import { afterEach, beforeEach, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const { runHeaders } = await import("@platform/lib/api");
const { runAgent, resetSupersedesForTests } = await import("./agent");

interface Sent {
  url: string;
  headers: Record<string, string>;
}
let sent: Sent[] = [];
const realFetch = globalThis.fetch;

beforeEach(() => {
  sent = [];
  resetSupersedesForTests();
  (globalThis as { fetch: unknown }).fetch = async (
    input: unknown,
    init?: { headers?: Record<string, string> },
  ): Promise<Response> => {
    sent.push({
      url: String(typeof input === "string" ? input : (input as { url: string }).url),
      headers: { ...(init?.headers ?? {}) },
    });
    return { ok: true, status: 200, json: async () => ({ ok: true, result: {} }) } as unknown as Response;
  };
});
afterEach(() => {
  globalThis.fetch = realFetch;
  resetSupersedesForTests();
});

// ---- the builder ----------------------------------------------------------

test("runHeaders spells the four exactly as calls.py reads them", () => {
  expect(
    runHeaders({ page: "/w/p/.claude/template.html", target: "/w/p", callId: "c1", supersedes: "c0" }),
  ).toEqual({
    "X-Fused-Page": encodeURIComponent("/w/p/.claude/template.html"),
    "X-Fused-Target": encodeURIComponent("/w/p"),
    "X-Fused-Call": "c1",
    "X-Fused-Supersedes": "c0",
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

// ---- what an actual agent.py call sends -----------------------------------

test("a poll carries the page, the target and a call id", async () => {
  await runAgent("/w/p/.claude", "poll", { run_id: "r1" } as never, {
    key: null,
    target: "/w/p",
  });
  expect(sent).toHaveLength(1);
  const h = sent[0]!.headers;
  expect(sent[0]!.url).toBe("/api/run");
  // The PAGE is the template's own html, derived from the script's dir — what
  // `--page` names, and what a reader looking for "the chat's calls" types.
  expect(h["X-Fused-Page"]).toBe(encodeURIComponent("/w/p/.claude/template.html"));
  expect(h["X-Fused-Target"]).toBe(encodeURIComponent("/w/p"));
  expect(h["X-Fused-Call"]).toBeTruthy();
  expect(h["X-Fused-Supersedes"]).toBeUndefined();
  // And the two fixed headers are untouched by the additions.
  expect(h["X-Fused"]).toBe("1");
  expect(h["Content-Type"]).toBe("application/json");
});

test("every call gets its OWN id", async () => {
  await runAgent("/w/p/.claude", "poll", {} as never, { key: null });
  await runAgent("/w/p/.claude", "poll", {} as never, { key: null });
  expect(sent[0]!.headers["X-Fused-Call"]).not.toBe(sent[1]!.headers["X-Fused-Call"]);
});

test("a SUPERSEDED call is named on the request that superseded it", async () => {
  // `calls.py:80-85` — the mark rides the superseding request because that
  // request "leaves in the same task as the abort, so the mark lands before the
  // abandoned call's record is written".
  //
  // Both on one key, so the second aborts the first. The first's promise never
  // settles by design (the supersede rule hangs it), so it is not awaited.
  void runAgent("/w/p/.claude", "poll", {} as never, { key: "poll" });
  const firstId = sent[0]!.headers["X-Fused-Call"];
  expect(firstId).toBeTruthy();

  await runAgent("/w/p/.claude", "poll", {} as never, { key: "poll" });
  expect(sent).toHaveLength(2);
  expect(sent[1]!.headers["X-Fused-Supersedes"]).toBe(firstId);
});

test("the supersede mark is spent once, not carried onto later calls", async () => {
  void runAgent("/w/p/.claude", "poll", {} as never, { key: "poll" });
  await runAgent("/w/p/.claude", "poll", {} as never, { key: "poll" });
  expect(sent[1]!.headers["X-Fused-Supersedes"]).toBeTruthy();
  await runAgent("/w/p/.claude", "poll", {} as never, { key: null });
  expect(sent[2]!.headers["X-Fused-Supersedes"]).toBeUndefined();
});

test("no target given: the page still attributes the call", async () => {
  await runAgent("/w/p/.claude", "snapshots", {} as never, { key: null });
  expect(sent[0]!.headers["X-Fused-Page"]).toBeTruthy();
  expect(sent[0]!.headers["X-Fused-Target"]).toBeUndefined();
});
