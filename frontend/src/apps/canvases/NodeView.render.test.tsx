// The node view's start flow (SPEC §53.3 NV-5, NV-6), driven: the server's
// answers come from a `globalThis.fetch` stub (not a module mock — see
// FilesHome.render.test.tsx for why), and react-test-renderer renders the
// real component with real effects. No DOM: the workbench frame is an element
// in the tree, so its `src` can be read but nothing loads in it — the relay
// itself is embed-delta-relay.test.ts's.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import NodeView from "./NodeView";
import { emptySyncState } from "./sync-state";

type Reply = { status?: number; body: unknown } | (() => Promise<{ status?: number; body: unknown }>);

const realFetch = globalThis.fetch;
const realWebSocket = globalThis.WebSocket;
// The events socket never opens here: the feed falls back to its poll.
const sockets: string[] = [];
class DeadSocket {
  constructor(url: string) {
    sockets.push(url);
  }
  onopen: (() => void) | null = null;
  onmessage: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  close() {}
}
const SYNC = { ...emptySyncState("demo"), disk_rev: 1, watcher_epoch: "e1", watching: true };
let routes: Record<string, Reply> = {};
let calls: string[] = [];
// Every request in full: the query string and the JSON body.
let requests: Array<{ method: string; url: URL; body: Record<string, unknown> | null }> = [];

function route(method: string, path: string, reply: Reply) {
  routes[`${method} ${path}`] = reply;
}

beforeEach(() => {
  // Another suite in this process may have taken `window` away.
  installDomShim();
  (globalThis as { WebSocket: unknown }).WebSocket = DeadSocket;
  routes = {};
  calls = [];
  requests = [];
  sockets.length = 0;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://127.0.0.1:1777");
    const method = (init?.method ?? "GET").toUpperCase();
    const key = `${method} ${url.pathname}`;
    calls.push(key);
    requests.push({ method, url, body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
    const hit = routes[key];
    if (!hit) return new Response(JSON.stringify({ error: `no route ${key}` }), { status: 404 });
    const r = typeof hit === "function" ? await hit() : hit;
    return new Response(JSON.stringify(r.body), { status: r.status ?? 200 });
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  globalThis.WebSocket = realWebSocket;
});

const NODE = {
  enabled: true,
  env: "prod",
  name: "demo",
  node: "udf_1",
  kind: "udf",
  file: "udf_1.py",
  title: "Load data",
  live: true,
  reference: false,
  signed_in: true,
  handle: "ada",
};

const STATUS = {
  cli_found: true,
  logged_in: true,
  auth_env: "prod",
  creds_stamp: 1,
  login_in_flight: false,
  workbench_env: "prod",
  locked: false,
  envs: {
    prod: { base_url: "https://www.fused.io", canvases_dir: "/x/prod/user", cloned_count: 1, available: true },
    unstable: { base_url: "https://unstable.fused.io", canvases_dir: "/x/unstable/user", cloned_count: 0, available: true },
    custom: { base_url: "https://c.example", canvases_dir: "/x/custom/user", cloned_count: 0, available: true },
  },
  settings_path: "/x/settings.toml",
};

async function settle(n = 8) {
  for (let i = 0; i < n; i += 1) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

function text(r: ReactTestRenderer): string {
  const out: string[] = [];
  const walk = (n: unknown) => {
    if (typeof n === "string") out.push(n);
    else if (Array.isArray(n)) n.forEach(walk);
    else if (n && typeof n === "object" && "children" in n) walk((n as { children: unknown }).children);
  };
  walk(r.toJSON());
  return out.join("");
}

async function mount(): Promise<ReactTestRenderer> {
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(NodeView, { path: "/Users/me/Fused/canvas/prod/user/demo/udf_1.py", placement: "content" }));
  });
  await settle();
  return r;
}

describe("NodeView start (NV-5)", () => {
  test("not signed in: the sign-in action, and nothing cloned or started", async () => {
    route("GET", "/api/canvases/node", { body: { ...NODE, signed_in: false, live: false } });
    const r = await mount();
    expect(text(r)).toContain("Sign in to Fused");
    expect(calls).toEqual(["GET /api/canvases/node"]);
    r.unmount();
  });

  test("not a node: says so, and calls nothing else", async () => {
    route("GET", "/api/canvases/node", { body: { enabled: true, node: null } });
    const r = await mount();
    expect(text(r)).toContain("not a node of a canvas");
    expect(calls).toEqual(["GET /api/canvases/node"]);
    r.unmount();
  });

  test("a reference copy is cloned first ('Cloning <canvas>…'), then the sync starts", async () => {
    let finishClone!: () => void;
    const cloned = new Promise<void>((res) => (finishClone = res));
    route("GET", "/api/canvases/node", { body: { ...NODE, live: false, reference: true } });
    route("POST", "/api/canvases/clone", async () => {
      await cloned;
      return { body: { ok: true, dir: "/x" } };
    });
    route("POST", "/api/canvases/sync/start", { body: { name: "demo", dir: "/x", error: null } });
    route("GET", "/api/canvases/status", { body: STATUS });
    let fsChanged = 0;
    (window as { _fusedFsChanged?: () => void })._fusedFsChanged = () => (fsChanged += 1);
    const r = await mount();
    expect(text(r)).toContain("Cloning demo…");
    expect(calls).not.toContain("POST /api/canvases/sync/start");
    await act(async () => finishClone());
    await settle();
    const clone = calls.indexOf("POST /api/canvases/clone");
    const start = calls.indexOf("POST /api/canvases/sync/start");
    expect(clone).toBeGreaterThanOrEqual(0);
    expect(start).toBeGreaterThan(clone);
    // The explorer is told the file may have changed under its editor.
    expect(fsChanged).toBe(1);
    delete (window as { _fusedFsChanged?: () => void })._fusedFsChanged;
    r.unmount();
  });

  test("a live clone: no clone, the sync starts, and the frame is the node-mode URL (NV-6)", async () => {
    route("GET", "/api/canvases/node", { body: NODE });
    route("POST", "/api/canvases/sync/start", { body: { name: "demo", dir: "/x", error: null } });
    route("GET", "/api/canvases/status", { body: STATUS });
    route("GET", "/api/canvases/sync/state", { body: SYNC });
    const r = await mount();
    expect(calls).not.toContain("POST /api/canvases/clone");
    expect(calls).toContain("POST /api/canvases/sync/start");
    // The handle the server already had: no whoami run.
    expect(calls).not.toContain("GET /api/canvases/whoami");
    const frame = r.root.findByType("iframe");
    expect(frame.props.src).toBe(
      "https://www.fused.io/workbench/ada/demo?fused_embed_auth=1&fused_embed_v=3&fused_embed_node=udf_1",
    );
    expect(frame.props.referrerPolicy).toBe("origin");
    // NM-1: a per-frame window name, so the workbench's stored node flag
    // is this frame's alone.
    expect(frame.props.name).toMatch(/^fused-node-[A-Za-z0-9_-]{1,64}$/);
    expect(text(r)).toContain("Load data");
    expect(text(r)).toContain("Open in canvas");
    r.unmount();
  });

  test("the custom env frames through its embed proxy, and asks whoami when no handle is cached", async () => {
    route("GET", "/api/canvases/node", { body: { ...NODE, env: "custom", handle: undefined } });
    route("POST", "/api/canvases/sync/start", { body: { name: "demo", dir: "/x", error: null } });
    route("GET", "/api/canvases/status", {
      body: {
        ...STATUS,
        envs: { ...STATUS.envs, custom: { ...STATUS.envs.custom, embed_url: "http://127.0.0.1:4100" } },
      },
    });
    route("GET", "/api/canvases/whoami", { body: { handle: "bo" } });
    route("GET", "/api/canvases/sync/state", { body: SYNC });
    const r = await mount();
    expect(r.root.findByType("iframe").props.src).toBe(
      "http://127.0.0.1:4100/workbench/bo/demo?fused_embed_auth=1&fused_embed_v=3&fused_embed_node=udf_1",
    );
    r.unmount();
  });

  test("owned by another process: the take-over notice, and no frame", async () => {
    route("GET", "/api/canvases/node", { body: NODE });
    route("POST", "/api/canvases/sync/start", {
      status: 409,
      body: { error: "owned", code: "owned", owner: { pid: 42, port: 1778 } },
    });
    route("GET", "/api/canvases/status", { body: STATUS });
    const r = await mount();
    expect(text(r)).toContain("Take over here");
    expect(text(r)).toContain("pid 42");
    expect(r.root.findAllByType("iframe")).toEqual([]);
    r.unmount();
  });
});

describe("NodeView's frame and relay (NV-6, NV-7)", () => {
  const ORIGIN = "https://www.fused.io";

  test("frame id and scope on every embed call, origin + source checked, nothing written", async () => {
    route("GET", "/api/canvases/node", { body: NODE });
    route("POST", "/api/canvases/sync/start", { body: { name: "demo", dir: "/x", error: null } });
    route("GET", "/api/canvases/status", { body: STATUS });
    route("GET", "/api/canvases/sync/state", { body: SYNC });
    route("GET", "/api/canvases/token", { body: { access_token: "tok" } });
    route("GET", "/api/canvases/delta", { body: { rev: 1, base_rev: null, full: true, ops: [{ op: "upsert", name: "udf_1" }], gen: null, seq: null } });
    route("POST", "/api/canvases/delta/ack", { body: { ok: true, rev: 1 } });

    // The page's message listener, caught from the shim's window.
    const listeners: Array<(e: unknown) => void> = [];
    const win = window as unknown as { addEventListener: unknown; removeEventListener: unknown };
    const realAdd = win.addEventListener;
    const realRemove = win.removeEventListener;
    win.addEventListener = (type: string, fn: (e: unknown) => void) => {
      if (type === "message") listeners.push(fn);
    };
    win.removeEventListener = (type: string, fn: (e: unknown) => void) => {
      const i = listeners.indexOf(fn);
      if (type === "message" && i >= 0) listeners.splice(i, 1);
    };
    // The frame's window, as the iframe ref sees it.
    const posted: Array<{ msg: Record<string, unknown>; origin: string }> = [];
    const frameWin = { postMessage: (msg: Record<string, unknown>, origin: string) => posted.push({ msg, origin }) };
    const other = { postMessage() {} };
    const send = async (data: unknown, source: unknown, origin = ORIGIN) => {
      await act(async () => {
        for (const fn of [...listeners]) fn({ data, source, origin });
      });
      await settle(4);
    };

    try {
      let r!: ReactTestRenderer;
      await act(async () => {
        r = create(
          createElement(NodeView, { path: "/Users/me/Fused/canvas/prod/user/demo/udf_1.py", placement: "content" }),
          { createNodeMock: (el) => (el.type === "iframe" ? { contentWindow: frameWin } : null) },
        );
      });
      await settle();
      const frame = r.root.findByType("iframe");
      const name = String(frame.props.name);
      const frameId = name.replace(/^fused-node-/, "");
      expect(frameId).toMatch(/^[A-Za-z0-9_-]{1,64}$/);
      expect(frame.props.src).toBe(`${ORIGIN}/workbench/ada/demo?fused_embed_auth=1&fused_embed_v=3&fused_embed_node=udf_1`);

      // The events socket and the sync/state poll name this frame.
      expect(new URL(sockets[0]).searchParams.get("frame")).toBe(frameId);
      const state = requests.find((q) => q.url.pathname === "/api/canvases/sync/state")!;
      expect(state.url.searchParams.get("frame")).toBe(frameId);
      expect(state.url.searchParams.get("env")).toBe("prod");

      // A hello from another window, or from the frame on another origin: ignored.
      await send({ type: "fused-embed-hello" }, other);
      await send({ type: "fused-embed-hello" }, frameWin, "https://evil.example");
      await send({ type: "fused-embed-auth-ready" }, other);
      expect(calls).not.toContain("GET /api/canvases/delta");
      expect(calls).not.toContain("GET /api/canvases/token");

      // From the frame: the token (no caps, exact origin), then a full cut.
      await send({ type: "fused-embed-auth-ready" }, frameWin);
      const token = posted.find((p) => p.msg.type === "fused-embed-auth-token")!;
      expect(token.origin).toBe(ORIGIN);
      expect(token.msg.caps).toEqual([]);
      expect(token.msg.accessToken).toBe("tok");
      await send({ type: "fused-embed-hello" }, frameWin);
      const cut = requests.find((q) => q.url.pathname === "/api/canvases/delta")!;
      expect(cut.url.searchParams.get("frame")).toBe(frameId);
      expect(cut.url.searchParams.get("frame_scope")).toBe("udf_1");
      expect(cut.url.searchParams.get("full")).toBe("1");
      expect(posted.some((p) => p.msg.type === "fused-embed-delta" && p.msg.rev === 1 && p.origin === ORIGIN)).toBe(true);

      // Its ack: frame and scope in the body.
      await send({ type: "fused-embed-delta-applied", rev: 1 }, frameWin);
      const ack = requests.find((q) => q.url.pathname === "/api/canvases/delta/ack")!;
      expect(ack.body).toMatchObject({ name: "demo", env: "prod", frame: frameId, frame_scope: "udf_1", rev: 1, ok: true });

      // A reading frame's writes are never relayed.
      await send({ type: "fused-embed-snapshot", gen: 1, seq: 1, body: {} }, frameWin);
      await send({ type: "fused-embed-body", seq: 1, name: "udf_1", file: "udf_1.py", base_hash: "x", text: "y" }, frameWin);
      expect(calls.filter((c) => /snapshot|\/body$|rename/.test(c))).toEqual([]);
      r.unmount();
    } finally {
      win.addEventListener = realAdd;
      win.removeEventListener = realRemove;
    }
  });

  test("a failed token request keeps the frame mounted; Retry recovers it", async () => {
    route("GET", "/api/canvases/node", { body: NODE });
    route("POST", "/api/canvases/sync/start", { body: { name: "demo", dir: "/x", error: null } });
    route("GET", "/api/canvases/status", { body: STATUS });
    route("GET", "/api/canvases/sync/state", { body: SYNC });
    route("GET", "/api/canvases/token", { status: 503, body: { error: "token service down" } });

    const listeners: Array<(e: unknown) => void> = [];
    const win = window as unknown as { addEventListener: unknown; removeEventListener: unknown };
    const realAdd = win.addEventListener;
    const realRemove = win.removeEventListener;
    win.addEventListener = (type: string, fn: (e: unknown) => void) => {
      if (type === "message") listeners.push(fn);
    };
    win.removeEventListener = (type: string, fn: (e: unknown) => void) => {
      const i = listeners.indexOf(fn);
      if (type === "message" && i >= 0) listeners.splice(i, 1);
    };
    const posted: Array<Record<string, unknown>> = [];
    const frameWin = { postMessage: (msg: Record<string, unknown>) => posted.push(msg) };
    try {
      let r!: ReactTestRenderer;
      await act(async () => {
        r = create(
          createElement(NodeView, { path: "/Users/me/Fused/canvas/prod/user/demo/udf_1.py", placement: "content" }),
          { createNodeMock: (el) => (el.type === "iframe" ? { contentWindow: frameWin } : null) },
        );
      });
      await settle();
      await act(async () => {
        for (const fn of [...listeners]) fn({ data: { type: "fused-embed-auth-ready" }, source: frameWin, origin: ORIGIN });
      });
      await settle(4);
      expect(text(r)).toContain("token service down");
      // The frame is still there to take a token.
      expect(r.root.findAllByType("iframe").length).toBe(1);
      expect(posted.some((m) => m.type === "fused-embed-auth-token")).toBe(false);

      route("GET", "/api/canvases/token", { body: { access_token: "tok" } });
      const retry = r.root.find((n) => n.type === "button" && n.props.children === "Retry");
      await act(async () => {
        retry.props.onClick();
      });
      await settle(4);
      expect(posted.some((m) => m.type === "fused-embed-auth-token" && m.accessToken === "tok")).toBe(true);
      expect(text(r)).not.toContain("token service down");
      expect(r.root.findAllByType("iframe").length).toBe(1);
      r.unmount();
    } finally {
      win.addEventListener = realAdd;
      win.removeEventListener = realRemove;
    }
  });

  test("a failed pull keeps the Pull button, as SyncStrip does", async () => {
    const failed = {
      ...SYNC,
      legs: {
        ...SYNC.legs,
        server_to_disk: {
          state: "error",
          since_ms: 0,
          changed: [{ slug: "udf_1", kind: "changed" }],
          reason: "pull failed",
        },
      },
    };
    route("GET", "/api/canvases/node", { body: NODE });
    route("POST", "/api/canvases/sync/start", { body: { name: "demo", dir: "/x", error: null } });
    route("GET", "/api/canvases/status", { body: STATUS });
    route("GET", "/api/canvases/sync/state", { body: failed });
    const r = await mount();
    expect(r.root.findAll((n) => n.type === "button" && n.props.children === "Pull").length).toBe(1);
    r.unmount();
  });

  test("signed out: no sync chip", async () => {
    route("GET", "/api/canvases/node", { body: { ...NODE, signed_in: false, live: false } });
    const r = await mount();
    expect(text(r)).not.toContain("Connecting");
    expect(r.root.findAll((n) => n.props?.className === "node-view-sync")).toEqual([]);
    r.unmount();
  });
});
