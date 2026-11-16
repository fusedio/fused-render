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
class DeadSocket {
  onopen: (() => void) | null = null;
  onmessage: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  close() {}
}
const SYNC = { ...emptySyncState("demo"), disk_rev: 1, watcher_epoch: "e1", watching: true };
let routes: Record<string, Reply> = {};
let calls: string[] = [];

function route(method: string, path: string, reply: Reply) {
  routes[`${method} ${path}`] = reply;
}

beforeEach(() => {
  // Another suite in this process may have taken `window` away.
  installDomShim();
  (globalThis as { WebSocket: unknown }).WebSocket = DeadSocket;
  routes = {};
  calls = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://127.0.0.1:1777");
    const method = (init?.method ?? "GET").toUpperCase();
    const key = `${method} ${url.pathname}`;
    calls.push(key);
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
    r = create(createElement(NodeView, { path: "/Users/me/Fused/canvas/prod/user/demo/udf_1.py", placement: "side" }));
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
