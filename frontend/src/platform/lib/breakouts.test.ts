import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";
import {
  buildHandoffDraft,
  closeBreakout,
  getBreakouts,
  openBreakout,
  ownTurns,
  resetBreakouts,
  sendBackBreakout,
} from "./breakouts";

// The shim's window is inert; route its event methods through a real target for this suite only,
// then restore (bun shares one globalThis across files).
const bus = new EventTarget();
const stubs: Record<string, unknown> = {
  addEventListener: bus.addEventListener.bind(bus),
  removeEventListener: bus.removeEventListener.bind(bus),
  dispatchEvent: bus.dispatchEvent.bind(bus),
};
const w = window as unknown as Record<string, unknown>;
const saved = new Map<string, { had: boolean; value: unknown }>();
beforeAll(() => {
  for (const [k, v] of Object.entries(stubs)) {
    saved.set(k, { had: k in w, value: w[k] });
    w[k] = v;
  }
});
afterAll(() => {
  for (const [k, { had, value }] of saved) {
    if (had) w[k] = value;
    else delete w[k];
  }
  saved.clear();
});

afterEach(() => resetBreakouts());

const req = { sessionId: "s1", file: "/w/proj", forkAt: "u2" };

describe("breakout store", () => {
  test("open adds a card with a unique id and the parent's session", () => {
    const a = openBreakout(req);
    const b = openBreakout({ ...req, forkAt: "u3", forkKey: "k3" });
    expect(a).not.toBe(b);
    const cards = getBreakouts();
    expect(cards.map((c) => c.id)).toEqual([a, b]);
    expect(cards[0]).toMatchObject({ parentSessionId: "s1", file: "/w/proj", forkAt: "u2" });
  });
  test("a second open of the same message focuses the existing card", () => {
    const a = openBreakout({ ...req, forkKey: "k2" });
    const b = openBreakout({ ...req, forkKey: "k2" });
    expect(b).toBe(a);
    expect(getBreakouts()).toHaveLength(1);
  });
  test("close removes", () => {
    const a = openBreakout(req);
    openBreakout({ ...req, forkAt: "u9" });
    closeBreakout(a);
    expect(getBreakouts().map((c) => c.id)).not.toContain(a);
    expect(getBreakouts()).toHaveLength(1);
  });
  test("sendBack dispatches the composer event and clears the card", () => {
    const seen: unknown[] = [];
    const on = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener("fused:composer-draft", on);
    const saved: Array<[string, string]> = [];
    const a = openBreakout(req);
    sendBackBreakout(a, "the summary", (key, text) => saved.push([key, text]));
    window.removeEventListener("fused:composer-draft", on);
    expect(seen).toEqual([{ sessionId: "s1", file: "/w/proj", text: "the summary" }]);
    expect(saved).toEqual([["s1", "the summary"]]);
    expect(getBreakouts()).toHaveLength(0);
  });
});

const turns: any[] = [
  { role: "user", key: "k1", text: "dig into X", uuid: "u1" },
  {
    role: "assistant",
    key: "a1",
    text: "Early thought",
    segments: [
      {
        kind: "tool",
        id: "t1",
        name: "Write",
        input: { file_path: "/w/proj/.claude/handoffs/x.md", content: "..." },
        status: "ok",
        output: null,
        images: [],
      },
    ],
  },
  { role: "assistant", key: "a2", text: "Findings: X is Y.", segments: [] },
];

describe("buildHandoffDraft", () => {
  test("last assistant text plus the handoff file line", () => {
    expect(buildHandoffDraft(turns, "/w/proj/.claude/handoffs")).toBe(
      "Findings: X is Y.\n\nHandoff file: /w/proj/.claude/handoffs/x.md",
    );
  });
  test("no handoff write: just the text", () => {
    expect(buildHandoffDraft([turns[0]!, turns[2]!], "/w/proj/.claude/handoffs")).toBe(
      "Findings: X is Y.",
    );
  });
  test("a write outside the handoff dir is ignored", () => {
    const t = structuredClone(turns);
    (t[1] as any).segments[0].input.file_path = "/w/proj/src/x.md";
    expect(buildHandoffDraft(t, "/w/proj/.claude/handoffs")).toBe("Findings: X is Y.");
  });
  test("a trailing-slash dir keeps the whole filename; nested .md counts", () => {
    const w = (path: string) => [
      turns[0]!,
      {
        role: "assistant" as const,
        segments: [{ kind: "tool" as const, name: "Write", input: { file_path: path } }],
      } as unknown as (typeof turns)[number],
    ];
    expect(buildHandoffDraft(w("/d/handoffs/a.md"), "/d/handoffs/")).toContain("Handoff file: /d/handoffs/a.md");
    expect(buildHandoffDraft(w("/d/handoffs/a/b.md"), "/d/handoffs/")).toContain("Handoff file: /d/handoffs/a/b.md");
    expect(buildHandoffDraft(w("/d/other/a.md"), "/d/handoffs/")).not.toContain("Handoff file");
  });
  test("without a dir, any .claude/handoffs markdown counts; empty transcript is empty", () => {
    expect(buildHandoffDraft(turns)).toContain("Handoff file: /w/proj/.claude/handoffs/x.md");
    expect(buildHandoffDraft([])).toBe("");
  });
});

describe("ownTurns", () => {
  const parent = [
    { role: "user", key: "h-1", text: "parent q" },
    { role: "assistant", key: "h-2", text: "parent answer" },
  ];
  test("no sends means no own turns, so no draft", () => {
    expect(ownTurns(parent, 0)).toEqual([]);
    expect(buildHandoffDraft(ownTurns(parent, 0))).toBe("");
  });
  test("slices from the sends-th user turn counted from the end", () => {
    const all = [
      ...parent,
      { role: "user", key: "u:1", text: "side q" },
      { role: "assistant", key: "h-3", text: "side answer" },
    ];
    expect(ownTurns(all, 1).map((t) => t.key)).toEqual(["u:1", "h-3"]);
    expect(buildHandoffDraft(ownTurns(all, 1))).toBe("side answer");
  });
  test("survives a history refresh: every row is uuid-keyed", () => {
    const refreshed = [
      ...parent,
      { role: "user", key: "uuid-a", text: "side q1" },
      { role: "assistant", key: "uuid-b", text: "side a1" },
      { role: "user", key: "uuid-c", text: "side q2" },
      { role: "assistant", key: "uuid-d", text: "side a2" },
    ];
    expect(ownTurns(refreshed, 1).map((t) => t.key)).toEqual(["uuid-c", "uuid-d"]);
    expect(ownTurns(refreshed, 2).map((t) => t.key)).toEqual(["uuid-a", "uuid-b", "uuid-c", "uuid-d"]);
    expect(ownTurns(refreshed, 9)).toEqual([]);
  });
});
