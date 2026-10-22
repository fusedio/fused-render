import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { agentSocketCall, resetAgentSocketForTests } from "./agentSocket";
import { isNetworkFailure } from "./trouble";

// location / WebSocket are process-wide globals: save and restore them.
const realLocation = (globalThis as { location?: unknown }).location;
const realWebSocket = (globalThis as { WebSocket?: unknown }).WebSocket;

class FakeWS {
  static OPEN = 1;
  static instances: FakeWS[] = [];
  static autoOpen = true;
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeWS.instances.push(this);
    queueMicrotask(() => {
      if (FakeWS.autoOpen) this.open();
      else this.drop();
    });
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  send(t: string) {
    this.sent.push(JSON.parse(t));
  }
  reply(m: unknown) {
    this.onmessage?.({ data: JSON.stringify(m) });
  }
  drop() {
    this.readyState = 3;
    this.onclose?.();
  }
  close() {
    this.drop();
  }
}

function setEnv(host: string) {
  (globalThis as { location?: unknown }).location = { host, protocol: "http:" };
  (globalThis as { WebSocket?: unknown }).WebSocket = FakeWS;
}

const tick = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  FakeWS.instances = [];
  FakeWS.autoOpen = true;
  resetAgentSocketForTests();
  setEnv("127.0.0.1:1777");
});
afterEach(() => {
  resetAgentSocketForTests();
  (globalThis as { location?: unknown }).location = realLocation;
  (globalThis as { WebSocket?: unknown }).WebSocket = realWebSocket;
});

describe("agentSocketCall", () => {
  test("null when there is no location.host (bun tests, non-browser)", () => {
    setEnv("");
    expect(agentSocketCall("agent", {}, { action: "poll" })).toBeNull();
    expect(FakeWS.instances.length).toBe(0);
  });

  test("a reply resolves with status and nested body; headers and body ride the frame", async () => {
    const p = agentSocketCall("agent", { "X-Fused-Page": "p" }, { action: "poll" })!;
    await tick();
    const ws = FakeWS.instances[0]!;
    expect(ws.url).toBe("ws://127.0.0.1:1777/api/claude/agent/ws");
    expect(ws.sent[0]).toMatchObject({ route: "agent", headers: { "X-Fused-Page": "p" }, body: { action: "poll" } });
    ws.reply({ id: ws.sent[0]!.id, status: 200, body: { id: "x", status: "y" } });
    expect(await p).toEqual({ status: 200, body: { id: "x", status: "y" } });
  });

  test("status >= 400 comes back as status and body for the caller's mapper", async () => {
    const p = agentSocketCall("artifacts", {}, { action: "list" })!;
    await tick();
    const ws = FakeWS.instances[0]!;
    ws.reply({ id: ws.sent[0]!.id, status: 504, body: { error: { type: "Timeout", message: "m" } } });
    expect(await p).toEqual({ status: 504, body: { error: { type: "Timeout", message: "m" } } });
  });

  test("a drop mid-flight rejects with a message the network-failure matcher accepts", async () => {
    const p = agentSocketCall("agent", {}, { action: "send" })!;
    await tick();
    FakeWS.instances[0]!.reply({ id: -1, status: 200, body: null }); // proves the socket
    FakeWS.instances[0]!.drop();
    let err: unknown;
    try {
      await p;
    } catch (e) {
      err = e;
    }
    expect(String((err as Error).message)).toMatch(/failed to fetch/i);
    expect(isNetworkFailure(err)).toBe(true);
  });

  test("a socket that never opens latches the fallback: resolves null, later calls return null", async () => {
    FakeWS.autoOpen = false;
    const p = agentSocketCall("agent", {}, { action: "poll" })!;
    expect(await p).toBeNull();
    expect(agentSocketCall("agent", {}, { action: "poll" })).toBeNull();
  });

  test("a socket that closes before any reply latches the fallback", async () => {
    const p = agentSocketCall("agent", {}, { action: "send" })!;
    await tick();
    FakeWS.instances[0]!.drop();
    await p.catch(() => {});
    expect(agentSocketCall("agent", {}, { action: "poll" })).toBeNull();
  });

  test("a proven socket that drops backs off: calls during the gap go to fetch", async () => {
    const p = agentSocketCall("agent", {}, { action: "poll" })!;
    await tick();
    const ws = FakeWS.instances[0]!;
    ws.reply({ id: ws.sent[0]!.id, status: 200, body: {} });
    await p;
    ws.drop();
    expect(agentSocketCall("agent", {}, { action: "poll" })).toBeNull();
  });

  test("abort removes the pending request and rejects with AbortError", async () => {
    const ac = new AbortController();
    const p = agentSocketCall("agent", {}, { action: "poll" }, ac.signal)!;
    await tick();
    ac.abort();
    let err: unknown;
    try {
      await p;
    } catch (e) {
      err = e;
    }
    expect((err as Error).name).toBe("AbortError");
    // a late reply for the aborted id is ignored
    const ws = FakeWS.instances[0]!;
    ws.reply({ id: ws.sent[0]!.id, status: 200, body: {} });
  });
});
