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
  subscribeBreakouts,
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
  test("reopening raises the existing card to the end of the list", () => {
    const a = openBreakout({ ...req, forkKey: "k2" });
    const b = openBreakout({ ...req, forkAt: "u3", forkKey: "k3" });
    let ticks = 0;
    const off = subscribeBreakouts(() => ticks++);
    expect(openBreakout({ ...req, forkKey: "k2" })).toBe(a);
    expect(getBreakouts().map((c) => c.id)).toEqual([b, a]);
    expect(ticks).toBe(1);
    openBreakout({ ...req, forkKey: "k2" }); // already on top: no churn
    expect(ticks).toBe(1);
    off();
  });
  test("close removes", () => {
    const a = openBreakout(req);
    openBreakout({ ...req, forkAt: "u9" });
    closeBreakout(a);
    expect(getBreakouts().map((c) => c.id)).not.toContain(a);
    expect(getBreakouts()).toHaveLength(1);
  });
  test("a handled event: the composer owns the draft, nothing is saved", async () => {
    const seen: unknown[] = [];
    const on = (e: Event) => {
      seen.push((e as CustomEvent).detail);
      e.preventDefault();
    };
    window.addEventListener("fused:composer-draft", on);
    const saved: unknown[] = [];
    const a = openBreakout(req);
    const ok = await sendBackBreakout(
      a,
      "the summary",
      (...args) => saved.push(args),
      () => {
        throw new Error("must not read the draft");
      },
    );
    window.removeEventListener("fused:composer-draft", on);
    expect(ok).toBe(true);
    expect(seen).toEqual([{ sessionId: "s1", file: "/w/proj", text: "the summary" }]);
    expect(saved).toEqual([]);
    expect(getBreakouts()).toHaveLength(0);
  });
  test("no parent mounted: appended to the saved draft, attachments kept", async () => {
    const saved: unknown[][] = [];
    const att = [{ name: "a.png" }] as never;
    const a = openBreakout(req);
    const ok = await sendBackBreakout(
      a,
      "the summary",
      (...args) => (saved.push(args), { ok: true }),
      async () => ({ text: "half typed", attachments: att }) as never,
    );
    expect(ok).toBe(true);
    expect(saved).toEqual([["s1", "half typed\n\nthe summary", att]]);
    expect(getBreakouts()).toHaveLength(0);
  });
  test("no parent mounted and no saved draft: just the text", async () => {
    const saved: unknown[][] = [];
    const a = openBreakout(req);
    await sendBackBreakout(a, "the summary", (...args) => (saved.push(args), { ok: true }), async () => null);
    expect(saved).toEqual([["s1", "the summary", []]]);
  });
  test("a refused save keeps the card open and answers false", async () => {
    const a = openBreakout(req);
    const ok = await sendBackBreakout(a, "x", () => ({ ok: false }), async () => null);
    expect(ok).toBe(false);
    expect(getBreakouts().map((c) => c.id)).toEqual([a]);
  });
  test("an unreadable draft keeps the card open without writing", async () => {
    const a = openBreakout(req);
    let wrote = false;
    const ok = await sendBackBreakout(a, "x", () => ((wrote = true), { ok: true }), async () => undefined);
    expect(ok).toBe(false);
    expect(wrote).toBe(false);
    expect(getBreakouts()).toHaveLength(1);
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
  test("a failed write is not the handoff file; an earlier good one still is", () => {
    const seg = (path: string, status: string) => ({
      role: "assistant" as const,
      key: path,
      text: "",
      segments: [{ kind: "tool" as const, name: "Write", status, input: { file_path: path } }],
    });
    const base = [turns[0]!, turns[2]!];
    const failedOnly = [...base, seg("/d/handoffs/bad.md", "error")];
    expect(buildHandoffDraft(failedOnly, "/d/handoffs")).not.toContain("Handoff file");
    const both = [...base, seg("/d/handoffs/good.md", "ok"), seg("/d/handoffs/bad.md", "error")];
    expect(buildHandoffDraft(both, "/d/handoffs")).toContain("Handoff file: /d/handoffs/good.md");
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
