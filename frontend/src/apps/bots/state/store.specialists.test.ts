// SPECIALIST ROWS IN THE BOTS STORE (docs/specialists-v2.md, plan risk 3): the page's poll appends the specialists
// to the bots; off the page it never asks for them. Opening a specialist marks it seen through its own route, never
// the bots backend's `open` or `seen`, which do not know it. Its menu has only what its routes do.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, beforeEach, describe, expect, test } from "bun:test";

const { pollOnce, getState, openBot, setState } = await import("./store");
const { setBotsRoot } = await import("../lib/root");
const { menuItems } = await import("../components/menuIcons");

const realFetch = globalThis.fetch;
const calls: string[] = [];
const spec = { id: "canvas", name: "Canvas", about: "Canvases", folder: "", face: null, home: "/w/specialists/canvas", working: false, last: { text: "Noted.", at: 50 }, new: true };
const bots = { ts: 1, usage: null, imessage: null, bots: [{ id: "b1", name: "Helper", status: "idle", seq: 1, news_seq: 1, seen_seq: 1, events: [] }] };
(globalThis as { fetch: unknown }).fetch = (url: string | URL, init?: RequestInit) => {
  const u = String(url);
  calls.push(`${init?.method || "GET"} ${u}`);
  const body = u.startsWith("/api/specialists") && !init?.method ? [spec] : u.startsWith("/api/bots?") ? bots : { ok: true };
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
};
beforeEach(() => { calls.length = 0; });
// The store is one module for every suite in the run: leave it as found.
afterAll(() => { setBotsRoot(null); setState({ bots: [], sel: null, events: {}, cursors: {}, seen: {} }); (globalThis as { fetch: unknown }).fetch = realFetch; });

describe("specialist rows", () => {
  test("off the page: the bots only, and no specialists request", async () => {
    await pollOnce();
    expect(getState().bots.map((b) => b.id)).toEqual(["b1"]);
    expect(calls.some((c) => c.includes("/api/specialists"))).toBe(false);
  });

  test("on the page: appended after the bots as their own kind; opening one marks it seen on its own route only", async () => {
    setBotsRoot({ isConnected: true } as HTMLElement);
    await pollOnce();
    const row = getState().bots.find((b) => b.id === "canvas");
    expect(getState().bots.map((b) => b.id)).toEqual(["b1", "canvas"]);
    expect(row?.kind).toBe("specialist");
    expect(row?.specialist?.home).toBe("/w/specialists/canvas");
    calls.length = 0;
    openBot("canvas");
    await new Promise((r) => setTimeout(r, 0));
    expect(calls).toContain("POST /api/specialists/canvas/seen");
    expect(calls.some((c) => c.startsWith("POST /api/bots/canvas"))).toBe(false);
  });

  test("the menu: Settings…, Update now, Delete…", () => {
    const row = getState().bots.find((b) => b.id === "canvas")!;
    expect(menuItems(row, { live: true }).map((i) => (i === "hr" ? "hr" : i.a))).toEqual(["settings", "update", "hr", "delete"]);
  });
});
