// A SERVER WITHOUT BOTS (Windows; `/api/config` bots_available false): the Bots page still opens for the specialists,
// which are task sessions, not bots (docs/specialists.md). Its poll lists them alone and never asks `/api/bots` (a 503
// there, whose failure would take the specialists' rows down with it), and a bare /bots lands on one of them.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
// The poll's page half writes the tab title (lib/notify updateTitle); the shim has none.
(document as { title: string }).title = "Bots";
import { afterAll, beforeEach, describe, expect, test } from "bun:test";

const { pollOnce, getState, setState } = await import("./store");
const { setBotsRoot } = await import("../lib/root");
const { publishSpecialistsEnabled, resetSpecialistsFlagForTests } = await import("@platform/lib/specialists-flag");
const { setBotsAvailableForTests } = await import("@platform/lib/bots-available");

const realFetch = globalThis.fetch;
const calls: string[] = [];
const spec = { id: "canvas", name: "Canvas", about: "Canvases", folder: "", face: null, home: "/w/specialists/canvas", working: false, last: { text: "Noted.", at: 50 }, new: false };
(globalThis as { fetch: unknown }).fetch = (url: string | URL, init?: RequestInit) => {
  const u = String(url);
  calls.push(`${init?.method || "GET"} ${u}`);
  if (u.startsWith("/api/bots")) return Promise.resolve(new Response('{"detail":"bots are not supported on Windows"}', { status: 503 }));
  const body = u.startsWith("/api/specialists") && !init?.method ? [spec] : { ok: true };
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
};
beforeEach(() => { calls.length = 0; });
// The store is one module for every suite in the run: leave it as found.
afterAll(() => {
  setBotsRoot(null);
  setBotsAvailableForTests(null);
  resetSpecialistsFlagForTests();
  setState({ bots: [], sel: null, events: {}, cursors: {}, seen: {}, banner: { show: false, text: "" } });
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

describe("the specialists-only Bots page (no bots on this server)", () => {
  test("the page lists the specialists alone, never asks /api/bots, and shows no error", async () => {
    setBotsAvailableForTests(false);
    setBotsRoot({ isConnected: true } as HTMLElement);
    publishSpecialistsEnabled(true);
    setState({ bots: [], sel: null });
    await pollOnce();
    expect(getState().bots.map((b) => b.id)).toEqual(["canvas"]);
    expect(getState().bots[0]?.kind).toBe("specialist");
    expect(calls.some((c) => c.includes("/api/bots"))).toBe(false);
    expect(getState().banner.show).toBe(false);
  });

  test("with nothing selected, the poll lands on a specialist", async () => {
    setState({ sel: null });
    await pollOnce();
    expect(getState().sel).toBe("canvas");
  });

  test("a server with bots is unchanged: /api/bots is asked", async () => {
    setBotsAvailableForTests(true);
    await pollOnce().catch(() => {});
    expect(calls.some((c) => c.startsWith("GET /api/bots?"))).toBe(true);
  });
});
