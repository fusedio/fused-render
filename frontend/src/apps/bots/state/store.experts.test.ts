// EXPERT ROWS IN THE BOTS STORE (docs/experts.md): the page's poll appends the experts
// to the bots; off the page it never asks for them. Opening an expert marks nothing seen: not the bots backend,
// which does not know it, and not its learning dot, which its Learning tab clears. Its menu has only what its routes do.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, beforeEach, describe, expect, test } from "bun:test";

const { pollOnce, getState, openBot, setState, resetPollForTests } = await import("./store");
const { setBotsRoot } = await import("../lib/root");
const { menuItems } = await import("../components/menuIcons");
const { publishExpertsEnabled, resetExpertsFlagForTests } = await import("@platform/lib/experts-flag");

const realFetch = globalThis.fetch;
const calls: string[] = [];
let failList = false;
const spec = { id: "canvas", name: "Canvas", about: "Canvases", folder: "", face: null, home: "/w/experts/canvas", working: false, last: { text: "Noted.", at: 50 }, new: true };
const bots = { ts: 1, usage: null, imessage: null, bots: [{ id: "b1", name: "Helper", status: "idle", seq: 1, news_seq: 1, seen_seq: 1, events: [] }] };
(globalThis as { fetch: unknown }).fetch = (url: string | URL, init?: RequestInit) => {
  const u = String(url);
  calls.push(`${init?.method || "GET"} ${u}`);
  if (failList && u.startsWith("/api/experts") && !init?.method) return Promise.resolve(new Response("{}", { status: 500 }));
  const body = u.startsWith("/api/experts") && !init?.method ? [spec] : u.startsWith("/api/bots?") ? bots : { ok: true };
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
};
// The store is shared with every file in the run: a poll another file left in flight against its own stub (the rail's
// bot card feeds this store) would be what actExpert's poll waits on, until the 5 s test timeout.
resetPollForTests();
beforeEach(() => { calls.length = 0; });
// The store is one module for every suite in the run: leave it as found.
afterAll(() => { setBotsRoot(null); resetExpertsFlagForTests(); setState({ bots: [], sel: null, events: {}, cursors: {}, seen: {} }); (globalThis as { fetch: unknown }).fetch = realFetch; });

describe("expert rows", () => {
  test("off the page: the bots only, and no experts request", async () => {
    await pollOnce();
    expect(getState().bots.map((b) => b.id)).toEqual(["b1"]);
    expect(calls.some((c) => c.includes("/api/experts"))).toBe(false);
  });

  test("on the page with the switch off: no experts request", async () => {
    setBotsRoot({ isConnected: true } as HTMLElement);
    publishExpertsEnabled(false);
    await pollOnce();
    expect(getState().bots.map((b) => b.id)).toEqual(["b1"]);
    expect(calls.some((c) => c.includes("/api/experts"))).toBe(false);
  });

  test("on the page: appended after the bots as their own kind; opening one marks nothing seen", async () => {
    publishExpertsEnabled(true);
    setState({ bots: [] });
    await pollOnce();
    const row = getState().bots.find((b) => b.id === "canvas");
    expect(getState().bots.map((b) => b.id)).toEqual(["b1", "canvas"]);
    expect(row?.kind).toBe("expert");
    expect(row?.expert?.home).toBe("/w/experts/canvas");
    calls.length = 0;
    openBot("canvas");
    await new Promise((r) => setTimeout(r, 0));
    // Neither the bots backend's open nor the expert's seen: its dot is cleared by its Learning tab.
    expect(calls.some((c) => c.startsWith("POST /api/bots/canvas") || c.endsWith("/seen"))).toBe(false);
  });

  test("a failed refresh keeps the rows, and the open expert stays selected", async () => {
    const { actExpert } = await import("./store");
    failList = true;
    await actExpert(() => Promise.resolve());  // drops the row cache, then polls
    failList = false;
    expect(getState().bots.map((b) => b.id)).toEqual(["b1", "canvas"]);
    expect(getState().sel).toBe("canvas");
  });

  test("the menu: Settings…, Export transcript, Delete…", () => {
    const row = getState().bots.find((b) => b.id === "canvas")!;
    expect(menuItems(row, { live: true }).map((i) => (i === "hr" ? "hr" : i.a))).toEqual(["settings", "export", "hr", "delete"]);
  });
});
