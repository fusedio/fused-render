// The specialist's pane (SpecialistPane.tsx): Memory rendered with Edit opening memory.md in the explorer (no
// editor here); Skills read-only; Learning lists what waits (kinds, no paths) with Learn now, and what a run read.
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
  [`${S}/notes`]: { notes: [
    { name: "n1.md", text: '# Note from task "Sync"\n\nPins are 4px.\n', at: 1, kind: "note", title: "Sync" },
    { name: "b1.md", text: '# Breakout from "Fix it"\n\nTranscript: /t/b.jsonl\nFrom line: 1\n', at: 2, kind: "breakout", title: "Fix it" },
  ] },
  [`${S}/changes`]: { changes: [{ sha: "abc", at: 1, label: "Update", read: 2, changed: false }] },
  [`${S}/changes/abc`]: { diff: "", reply: "Nothing new.", read: [{ kind: "note", title: "Sync" }, { kind: "breakout", title: "Fix it" }] },
};
(globalThis as { fetch: unknown }).fetch = (url: string | URL, init?: RequestInit) => {
  const u = String(url);
  calls.push(`${init?.method || "GET"} ${u}`);
  const body = answers[u] ?? (u.startsWith("/api/bots?") ? { ts: 1, usage: null, imessage: null, bots: [] } : { ok: true });
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
};
afterAll(() => { (globalThis as { fetch: unknown }).fetch = realFetch; });

const s: Specialist = { id: "canvas", name: "Canvas", about: "Canvases", folder: "", face: null, home: "/w/canvas", working: false, last: null, updating: false, last_update: null, new: true };
const settle = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
const text = (r: ReactTestRenderer) => JSON.stringify(r.toJSON());
const byText = (r: ReactTestRenderer, t: string) => r.root.find((n) => typeof n.type === "string" && n.children.includes(t));

test("Memory with Edit, read-only Skills, and Learning: what waits, Learn now, and what a run read and said", async () => {
  let r!: ReactTestRenderer;
  act(() => { r = create(createElement(SpecialistPane, { s })); });
  await settle();
  expect(text(r)).toContain("Pull before you push.");
  expect(r.root.find((n) => n.props.className === "xp-edit").props.href).toBe("/explorer/view/w/canvas/memory.md");
  expect(r.root.findAll((n) => n.type === "textarea")).toHaveLength(0);

  act(() => byText(r, "Skills").props.onClick());
  expect(text(r)).toContain("How a canvas is laid out");
  expect(calls).not.toContain(`GET ${S}/changes`);  // the history is read only on the Learning tab

  act(() => byText(r, "Learning · 2").props.onClick());
  await settle();
  expect(calls).toContain(`POST ${S}/seen`);  // opening it clears the row's dot
  expect(text(r)).toContain("Fix it");
  expect(text(r)).not.toContain("/t/b.jsonl");  // a conversation shows no path
  act(() => byText(r, "Breakout").parent!.props.onClick());
  expect(text(r)).toContain("It will read this conversation from where it last stopped.");
  act(() => byText(r, "Delete").props.onClick());
  await settle();
  expect(calls).toContain(`DELETE ${S}/notes/b1.md`);
  act(() => byText(r, "Learn now").props.onClick());
  await settle();
  expect(calls).toContain(`POST ${S}/update`);

  act(() => byText(r, "Read 2 entries, changed nothing").parent!.parent!.props.onClick());
  await settle();
  const read = r.root.findAll((n) => n.props.className === "xp-head static", { deep: false });
  expect(read.map((n) => n.findByProps({ className: "xp-origin" }).props.children)).toEqual(["Sync", "Fix it"]);  // one per line
  expect(text(r)).toContain("Nothing new.");
  expect(text(r)).toContain("It changed nothing.");
  act(() => r.unmount());
});
