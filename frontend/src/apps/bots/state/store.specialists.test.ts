// SPECIALIST ROWS IN THE BOTS STORE (docs/specialists-v2.md): the page's poll appends the specialists
// to the bots; off the page it never asks for them. Opening a specialist marks nothing seen: not the bots backend,
// which does not know it, and not its learning dot, which its Learning tab clears. Its menu has only what its routes do.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, beforeEach, describe, expect, test } from "bun:test";

const { pollOnce, getState, openBot, setState } = await import("./store");
const { setBotsRoot } = await import("../lib/root");
const { menuItems } = await import("../components/menuIcons");
const { publishSpecialistsEnabled, resetSpecialistsFlagForTests } = await import("@platform/lib/specialists-flag");

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
afterAll(() => { setBotsRoot(null); resetSpecialistsFlagForTests(); setState({ bots: [], sel: null, events: {}, cursors: {}, seen: {} }); (globalThis as { fetch: unknown }).fetch = realFetch; });

describe("specialist rows", () => {
  test("off the page: the bots only, and no specialists request", async () => {
    await pollOnce();
    expect(getState().bots.map((b) => b.id)).toEqual(["b1"]);
    expect(calls.some((c) => c.includes("/api/specialists"))).toBe(false);
  });

  test("on the page with the switch off: no specialists request", async () => {
    setBotsRoot({ isConnected: true } as HTMLElement);
    publishSpecialistsEnabled(false);
    await pollOnce();
    expect(getState().bots.map((b) => b.id)).toEqual(["b1"]);
    expect(calls.some((c) => c.includes("/api/specialists"))).toBe(false);
  });

  test("on the page: appended after the bots as their own kind; opening one marks nothing seen", async () => {
    publishSpecialistsEnabled(true);
    setState({ bots: [] });
    await pollOnce();
    const row = getState().bots.find((b) => b.id === "canvas");
    expect(getState().bots.map((b) => b.id)).toEqual(["b1", "canvas"]);
    expect(row?.kind).toBe("specialist");
    expect(row?.specialist?.home).toBe("/w/specialists/canvas");
    calls.length = 0;
    openBot("canvas");
    await new Promise((r) => setTimeout(r, 0));
    // Neither the bots backend's open nor the specialist's seen: its dot is cleared by its Learning tab.
    expect(calls.some((c) => c.startsWith("POST /api/bots/canvas") || c.endsWith("/seen"))).toBe(false);
  });

  test("the menu: Settings…, Export transcript, Delete…", () => {
    const row = getState().bots.find((b) => b.id === "canvas")!;
    expect(menuItems(row, { live: true }).map((i) => (i === "hr" ? "hr" : i.a))).toEqual(["settings", "export", "hr", "delete"]);
  });
});
