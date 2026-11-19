// The specialist's pane (SpecialistPane.tsx): Memory rendered with Edit opening memory.md in the explorer (no
// editor here); Skills read-only; a waiting note's Delete is its route's DELETE; a change opens to its diff.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import type { Specialist } from "@platform/lib/specialists";

const { SpecialistPane } = await import("./SpecialistPane");

const realFetch = globalThis.fetch;
const calls: string[] = [];
const S = "/api/specialists/canvas";
const answers: Record<string, unknown> = {
  [`${S}/memory`]: { text: "Pull before you push." },
  [`${S}/skills`]: { skills: [{ name: "layout", description: "How a canvas is laid out", text: "Two columns." }] },
  [`${S}/notes`]: { notes: [{ name: "n1.md", text: "Pins are 4px.", at: 1 }] },
  [`${S}/changes`]: { changes: [{ sha: "abc", at: 1, label: "Update" }] },
  [`${S}/changes/abc`]: { diff: "@@ -0,0 +1 @@\n+Pull before you push.\n" },
};
(globalThis as { fetch: unknown }).fetch = (url: string | URL, init?: RequestInit) => {
  const u = String(url);
  calls.push(`${init?.method || "GET"} ${u}`);
  const body = answers[u] ?? (u.startsWith("/api/bots?") ? { ts: 1, usage: null, imessage: null, bots: [] } : { ok: true });
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
};
afterAll(() => { (globalThis as { fetch: unknown }).fetch = realFetch; });

const s: Specialist = { id: "canvas", name: "Canvas", about: "Canvases", folder: "", face: null, home: "/w/canvas", working: false, last: null, new: false };
const settle = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
const text = (r: ReactTestRenderer) => JSON.stringify(r.toJSON());
const byText = (r: ReactTestRenderer, t: string) => r.root.find((n) => typeof n.type === "string" && n.children.includes(t));

test("tabs, Edit, note delete, a change's diff", async () => {
  let r!: ReactTestRenderer;
  act(() => { r = create(createElement(SpecialistPane, { s })); });
  await settle();
  expect(text(r)).toContain("Pull before you push.");
  expect(r.root.find((n) => n.props.className === "xp-edit").props.href).toBe("/explorer/view/w/canvas/memory.md");
  expect(r.root.findAll((n) => n.type === "textarea")).toHaveLength(0);

  act(() => byText(r, "Skills").props.onClick());
  expect(text(r)).toContain("How a canvas is laid out");
  expect(r.root.findAll((n) => n.type === "textarea")).toHaveLength(0);

  act(() => byText(r, "Delete").props.onClick());
  await settle();
  expect(calls).toContain(`DELETE ${S}/notes/n1.md`);

  expect(calls).not.toContain(`GET ${S}/changes`);  // the log is read only while its box is open
  act(() => r.root.find((n) => n.type === "details" && !!n.props.onToggle).props.onToggle({ currentTarget: { open: true } }));
  await settle();
  act(() => byText(r, "Update").parent!.parent!.props.onClick());
  await settle();
  expect(calls).toContain(`GET ${S}/changes/abc`);
  expect(r.root.findAll((n) => n.props.className === "xp-dr add")).toHaveLength(1);
  act(() => r.unmount());
});
