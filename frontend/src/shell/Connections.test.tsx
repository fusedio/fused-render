// Preferences → Connections (SPEC-connect-oauth.md §6, §11): the card per
// provider, each state's words and buttons, the Advanced kinds, and the
// login → consent tab → poll → connected round trip, against a stubbed
// `/api/connect` (a fake `globalThis.fetch`, the way the settings render
// tests stub their one endpoint).
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import Connections, {
  defaultAuth,
  FAST_POLL_MS,
  pollDelay,
  SLOW_POLL_MS,
  toolCount,
  type ConnectProvider,
  type ConnectStatus,
} from "@shell/Connections";

const notion: ConnectProvider = {
  key: "notion",
  name: "Notion",
  icon: "/api/connect/notion/icon",
  privacy: "Notion's MCP server sees what its tools touch.",
  auth: [{ kind: "mcp-oauth", available: true }],
  state: "connected",
  account: "Ada's workspace",
  tools: { remote: "https://mcp.notion.com/mcp", native: false, count: 23 }, // the list row's shape
  store: "file",
};
const github: ConnectProvider = {
  key: "github",
  name: "GitHub",
  icon: null,
  privacy: "GitHub sees the calls.",
  auth: [
    { kind: "mcp-oauth", available: true },
    { kind: "api-key", label: "Personal access token", available: true },
    { kind: "cli", available: false, reason: "gh is not installed" },
  ],
  state: "disconnected",
  tools: 0,
  store: "keychain",
};
const slack: ConnectProvider = {
  key: "slack",
  name: "Slack",
  icon: null,
  privacy: "Slack sees the calls.",
  auth: [
    {
      kind: "oauth-pkce",
      available: false,
      reason: "No Slack app is configured",
      byo: { manifest: "manifest.json", docs: "https://api.slack.com/apps?new_app=1" },
    },
  ],
  state: "disconnected",
  tools: 0,
  store: "keychain",
};

type Call = { method: string; url: string; body: unknown; headers: Record<string, string> };
let calls: Call[] = [];
let list: ConnectProvider[] = [];
let answers: Record<string, ConnectStatus> = {};
let opened: unknown[][] = [];
const realFetch = globalThis.fetch;
const w = window as unknown as { open?: unknown };
const realOpen = w.open;

function fakeFetch(url: string | URL, init?: RequestInit): Promise<Response> {
  const u = String(url);
  const method = init?.method ?? "GET";
  calls.push({
    method,
    url: u,
    body: init?.body ? JSON.parse(String(init.body)) : undefined,
    headers: (init?.headers ?? {}) as Record<string, string>,
  });
  const json = (d: unknown) => Promise.resolve(new Response(JSON.stringify(d), { status: 200 }));
  if (u === "/api/connect") return json(list);
  const key = `${method} ${u}`;
  if (key in answers) return json(answers[key]);
  return Promise.resolve(new Response(JSON.stringify({ error: "unexpected " + key }), { status: 404 }));
}

beforeEach(() => {
  calls = [];
  answers = {};
  opened = [];
  list = [notion, github, slack];
  globalThis.fetch = fakeFetch as typeof fetch;
  w.open = (...a: unknown[]) => {
    opened.push(a);
    return null;
  };
});
afterEach(() => {
  globalThis.fetch = realFetch;
  w.open = realOpen;
});

function textOf(n: ReactTestInstance | string): string {
  if (typeof n === "string") return n;
  return n.children.map(textOf).join("");
}
const card = (box: ReactTestRenderer, key: string) => box.root.find((n) => n.props["data-provider"] === key);
const buttons = (n: ReactTestInstance, label: string) =>
  n.findAll((x) => x.type === Button && textOf(x).trim() === label);
const click = async (b: ReactTestInstance) => act(async () => b.props.onClick());
const settle = () => act(async () => new Promise((r) => setTimeout(r, 0)));

async function mount() {
  let box!: ReactTestRenderer;
  await act(async () => {
    box = create(createElement(Connections));
  });
  await settle();
  return box;
}

describe("pure helpers", () => {
  test("polls fast only while a card is pending", () => {
    expect(pollDelay(["connected", "disconnected"])).toBe(SLOW_POLL_MS);
    expect(pollDelay(["connected", "pending"])).toBe(FAST_POLL_MS);
    expect(FAST_POLL_MS).toBe(1000);
    expect(SLOW_POLL_MS).toBe(30_000);
  });
  test("the default kind is the first available one", () => {
    expect(defaultAuth(github.auth)?.kind).toBe("mcp-oauth");
    expect(defaultAuth(slack.auth)).toBeNull();
  });
  test("only a numeric tools field is a count", () => {
    expect(toolCount({ state: "connected", tools: 4 })).toBe(4);
    expect(toolCount({ state: "connected", tools: { remote: "https://x", native: false, count: 7 } })).toBe(7);
    expect(toolCount({ state: "connected", tools: { remote: "https://x", native: false } })).toBeNull();
  });
});

describe("the cards", () => {
  test("one card per provider, each in its state's words", async () => {
    const box = await mount();
    const n = card(box, "notion");
    const t = textOf(n);
    expect(t).toContain("Connected as Ada's workspace");
    expect(t).toContain("23 tools");
    expect(t).toContain("Notion's MCP server sees what its tools touch.");
    expect(t).toContain("Stored in a private file in your Fused folder (no keychain on this OS)");
    expect(buttons(n, "Disconnect")).toHaveLength(1);
    expect(n.findAll((x) => x.type === "img")[0].props.src).toBe("/api/connect/notion/icon");

    const g = card(box, "github");
    expect(textOf(g)).toContain("Not connected");
    expect(textOf(g)).not.toContain("no keychain");
    expect(buttons(g, "Connect")).toHaveLength(1); // the default; Advanced's cli is unavailable
    // Advanced lists the other kinds: the key field and the unavailable CLI's reason.
    expect(textOf(g)).toContain("Advanced");
    expect(textOf(g)).toContain("gh is not installed");
    expect(g.findAll((x) => x.type === Input && x.props.type === "password")).toHaveLength(1);

    // No available kind: the reason, muted, and no Connect on the card itself —
    // only Advanced's BYO form, with its manifest link.
    const s = card(box, "slack");
    expect(textOf(s)).toContain("No Slack app is configured");
    expect(s.findAll((x) => x.props.className === "settings-row-control")).toHaveLength(0);
    expect(buttons(s, "Connect")).toHaveLength(1); // the BYO form's, under Advanced
  });

  test("a connected card's Disconnect posts logout", async () => {
    answers["POST /api/connect/notion/logout"] = { state: "disconnected", tools: 0 };
    const box = await mount();
    await click(buttons(card(box, "notion"), "Disconnect")[0]);
    const post = calls.find((c) => c.url === "/api/connect/notion/logout")!;
    expect(post.method).toBe("POST");
    expect(post.headers["X-Fused"]).toBe("1");
    expect(textOf(card(box, "notion"))).toContain("Not connected");
  });

  test("reconnect and error states offer the way back", async () => {
    list = [
      { ...notion, state: "reconnect" },
      { ...github, state: "error", error: "The vendor said no." },
    ];
    const box = await mount();
    expect(textOf(card(box, "notion"))).toContain("Needs reconnecting");
    expect(buttons(card(box, "notion"), "Reconnect")).toHaveLength(1);
    expect(textOf(card(box, "github"))).toContain("The vendor said no.");
    expect(buttons(card(box, "github"), "Try again")).toHaveLength(1);
  });

  test("BYO posts the client id with its kind", async () => {
    answers["POST /api/connect/slack/login"] = { state: "pending", url: "https://slack.example/oauth?x=1", tools: 0 };
    const box = await mount();
    const s = card(box, "slack");
    expect(s.find((x) => x.type === "a" && textOf(x) === "Create from manifest").props.href).toBe(
      "https://api.slack.com/apps?new_app=1",
    );
    const field = s.find((x) => x.type === Input && x.props["aria-label"] === "Client ID");
    await act(async () => field.props.onChange({ target: { value: " 123.456 " } }));
    const form = card(box, "slack").findAll((x) => x.type === "form")[0];
    await act(async () => form.props.onSubmit({ preventDefault() {} }));
    await settle();
    expect(calls.find((c) => c.url === "/api/connect/slack/login")!.body).toEqual({
      kind: "oauth-pkce",
      client_id: "123.456",
    });
  });

  test("a pasted key posts kind api-key with its value", async () => {
    answers["POST /api/connect/github/login"] = { state: "connected", account: "octocat", tools: 5 };
    const box = await mount();
    const field = card(box, "github").find((x) => x.type === Input && x.props.type === "password");
    await act(async () => field.props.onChange({ target: { value: "ghp_secret" } }));
    const form = card(box, "github").find((x) => x.type === "form" && x.findAll((i) => i.type === Input && i.props.type === "password").length === 1);
    await act(async () => form.props.onSubmit({ preventDefault() {} }));
    await settle();
    expect(calls.find((c) => c.url === "/api/connect/github/login")!.body).toEqual({ kind: "api-key", value: "ghp_secret" });
    expect(textOf(card(box, "github"))).toContain("Connected as octocat");
    expect(opened).toHaveLength(0);
  });
});

describe("signing in", () => {
  test("Connect opens the consent page once, shows the wait, and a 1 s poll lands it", async () => {
    const url = "https://vendor.example/authorize?state=abc";
    answers["POST /api/connect/github/login"] = { state: "pending", kind: "mcp-oauth", url, tools: 0 };
    answers["GET /api/connect/github"] = { state: "pending", kind: "mcp-oauth", url, tools: 0 };
    const box = await mount();
    await click(buttons(card(box, "github"), "Connect")[0]);
    await settle();

    const login = calls.find((c) => c.url === "/api/connect/github/login")!;
    expect(login.body).toEqual({ kind: "mcp-oauth" });
    expect(login.headers["X-Fused"]).toBe("1");
    expect(login.headers["Content-Type"]).toBe("application/json");
    expect(opened).toEqual([[url, "_blank"]]);

    const g = card(box, "github");
    expect(textOf(g)).toContain("Waiting for you in the browser…");
    expect(g.find((x) => x.type === "a" && textOf(x) === "Open again").props.href).toBe(url);
    expect(buttons(g, "Cancel")).toHaveLength(1);
    expect(textOf(g)).toContain("Browser on another machine? Paste the address it landed on");

    // First fast poll: still pending, the same URL — not opened again.
    await act(async () => new Promise((r) => setTimeout(r, FAST_POLL_MS + 150)));
    expect(calls.filter((c) => c.url === "/api/connect/github" && c.method === "GET").length).toBeGreaterThanOrEqual(1);
    expect(opened).toHaveLength(1);

    answers["GET /api/connect/github"] = { state: "connected", kind: "mcp-oauth", account: "octocat", tools: 1 };
    await act(async () => new Promise((r) => setTimeout(r, FAST_POLL_MS + 150)));
    expect(textOf(card(box, "github"))).toContain("Connected as octocat");
    expect(textOf(card(box, "github"))).toContain("1 tool");
    // Only the pending card was re-read on the fast cadence.
    expect(calls.some((c) => c.url === "/api/connect/notion" && c.method === "GET")).toBe(false);
    await act(async () => box.unmount());
  });

  test("a URL that arrives on a later poll still opens, once", async () => {
    const url = "https://vendor.example/authorize?state=late";
    answers["POST /api/connect/github/login"] = { state: "pending", kind: "mcp-oauth", tools: 0 };
    answers["GET /api/connect/github"] = { state: "pending", kind: "mcp-oauth", url, tools: 0 };
    const box = await mount();
    await click(buttons(card(box, "github"), "Connect")[0]);
    expect(opened).toHaveLength(0);
    await act(async () => new Promise((r) => setTimeout(r, FAST_POLL_MS + 150)));
    expect(opened).toEqual([[url, "_blank"]]);
    await act(async () => new Promise((r) => setTimeout(r, FAST_POLL_MS + 150)));
    expect(opened).toHaveLength(1);
    await act(async () => box.unmount());
  });

  test("device flow: the code big with Copy, the verification link, Cancel posts cancel", async () => {
    list = [
      {
        ...github,
        state: "pending",
        kind: "oauth-device",
        user_code: "WDJB-MJHT",
        verification_uri: "https://github.com/login/device",
      },
    ];
    answers["POST /api/connect/github/cancel"] = { state: "disconnected", tools: 0 };
    const box = await mount();
    const g = card(box, "github");
    expect(g.find((x) => x.props["aria-label"] === "Your code" && x.type === "span").children).toEqual(["WDJB-MJHT"]);
    expect(buttons(g, "Copy")).toHaveLength(1);
    expect(g.find((x) => x.type === "a" && textOf(x) === "https://github.com/login/device").props.target).toBe("_blank");
    await click(buttons(g, "Cancel")[0]);
    expect(calls.find((c) => c.url === "/api/connect/github/cancel")!.method).toBe("POST");
    expect(textOf(card(box, "github"))).toContain("Not connected");
    await act(async () => box.unmount());
  });

  test("the paste fallback posts the landed address to /callback", async () => {
    list = [{ ...github, state: "pending", kind: "mcp-oauth", url: "https://vendor.example/a" }];
    answers["POST /api/connect/github/callback"] = { state: "connected", account: "octocat", tools: 2 };
    const box = await mount();
    const isPaste = (x: ReactTestInstance) => x.type === Input && /landed on/.test(String(x.props["aria-label"]));
    const field = card(box, "github").find(isPaste);
    const landed = "http://localhost:47823/callback?code=c&state=s";
    await act(async () => field.props.onChange({ target: { value: landed } }));
    const form = card(box, "github").find((x) => x.type === "form" && x.findAll(isPaste).length === 1);
    await act(async () => form.props.onSubmit({ preventDefault() {} }));
    await settle();
    expect(calls.find((c) => c.url === "/api/connect/github/callback")!.body).toEqual({ url: landed });
    expect(textOf(card(box, "github"))).toContain("Connected as octocat");
    await act(async () => box.unmount());
  });
});
