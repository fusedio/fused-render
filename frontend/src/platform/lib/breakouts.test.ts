import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";
import {
  BREAKOUT_CTX_TAG,
  breakoutOpener,
  buildBreakoutContext,
  buildHandoffDraft,
  closeBreakout,
  exchangeKeys,
  getBreakouts,
  liveKeys,
  openBreakout,
  openBreakoutSettled,
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

const context = { block: "<breakout-context>\nx\n</breakout-context>", count: 2, title: "Parent" };
const req = { sessionId: "s1", file: "/w/proj", context };

describe("breakout folder", () => {
  const wt = "/w/proj/.claude/worktrees/x";
  test("a cwd that does not exist is dropped: the card stays on the chat's target", async () => {
    await openBreakoutSettled({ ...req, cwd: wt }, async () => false);
    const [card] = getBreakouts();
    expect(card?.file).toBe("/w/proj");
    expect("cwd" in card!).toBe(false);
  });
  test("a cwd that exists is kept", async () => {
    await openBreakoutSettled({ ...req, cwd: wt }, async (p) => p === wt);
    expect(getBreakouts()[0]?.cwd).toBe(wt);
  });
  test("a request without a cwd is never probed", async () => {
    let probed = 0;
    await openBreakoutSettled(req, async () => (probed++, true));
    expect(probed).toBe(0);
    expect(getBreakouts()).toHaveLength(1);
  });
});

describe("breakout store", () => {
  test("open adds a card with a unique id and the parent's session", () => {
    const a = openBreakout(req);
    const b = openBreakout({ ...req, forkKey: "k3" });
    expect(a).not.toBe(b);
    const cards = getBreakouts();
    expect(cards.map((c) => c.id)).toEqual([a, b]);
    expect(cards[0]).toMatchObject({ parentSessionId: "s1", file: "/w/proj", context });
  });
  test("a second open of the same message focuses the existing card", () => {
    const a = openBreakout({ ...req, forkKey: "k2" });
    const b = openBreakout({ ...req, forkKey: "k2" });
    expect(b).toBe(a);
    expect(getBreakouts()).toHaveLength(1);
  });
  test("reopening raises the existing card to the end of the list", () => {
    const a = openBreakout({ ...req, forkKey: "k2" });
    const b = openBreakout({ ...req, forkKey: "k3" });
    let ticks = 0;
    const off = subscribeBreakouts(() => ticks++);
    expect(openBreakout({ ...req, forkKey: "k2" })).toBe(a);
    expect(getBreakouts().map((c) => c.id)).toEqual([b, a]);
    expect(ticks).toBe(1);
    openBreakout({ ...req, forkKey: "k2" }); // already on top: no churn
    expect(ticks).toBe(1);
    off();
  });
  test("cwd round-trips onto the card, and is absent when not given", () => {
    openBreakout({ ...req, forkKey: "w1", cwd: "/w/x" });
    openBreakout({ ...req, forkKey: "w2" });
    const [a, b] = getBreakouts().slice(-2);
    expect(a!.cwd).toBe("/w/x");
    expect(b && "cwd" in b).toBe(false);
  });
  test("close removes", () => {
    const a = openBreakout(req);
    openBreakout({ ...req, forkKey: "k9" });
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

describe("breakoutOpener", () => {
  test("no opener inside an embedded document", () => {
    expect(breakoutOpener(true)).toBeUndefined();
    expect(breakoutOpener(false)).toBe(openBreakoutSettled);
  });
});

describe("exchangeKeys", () => {
  const turns = [
    { role: "user", key: "u1", text: "q1" },
    { role: "assistant", key: "a1", text: "r1" },
    { role: "user", key: "u2", text: "q2" },
    { role: "assistant", key: "a2", text: "r2" },
    { role: "assistant", key: "a3", text: "r3" },
  ] as const;
  test("any turn of an exchange selects the prompt and every answer to it", () => {
    expect(exchangeKeys(turns, "u1")).toEqual(["u1", "a1"]);
    for (const k of ["u2", "a2", "a3"]) expect(exchangeKeys(turns, k)).toEqual(["u2", "a2", "a3"]);
  });
  test("assistant turns before any prompt are an exchange of their own", () => {
    const lead = [{ role: "assistant", key: "x1", text: "a" }, { role: "assistant", key: "x2", text: "b" }, ...turns] as const;
    expect(exchangeKeys(lead, "x2")).toEqual(["x1", "x2"]);
  });
  test("an assistant turn with no prompt before it, or an unknown key, stands alone", () => {
    expect(exchangeKeys([turns[1]!], "a1")).toEqual(["a1"]);
    expect(exchangeKeys(turns, "nope")).toEqual([]);
  });
});

describe("liveKeys", () => {
  test("keeps only the keys of turns still in the transcript", () => {
    const turns = [{ key: "a" }, { key: "b" }];
    expect([...liveKeys(turns, new Set(["a", "gone"]))]).toEqual(["a"]);
    expect(liveKeys([], new Set(["a"])).size).toBe(0);
  });
});

describe("buildBreakoutContext", () => {
  const turns = [
    { role: "user", key: "u1", text: "How does the cache work?" },
    {
      role: "assistant",
      key: "a1",
      text: "It keys on path.",
      segments: [
        { kind: "text" },
        { kind: "tool", name: "Read", input: { file_path: "/w/cache.py" } },
        { kind: "tool", name: "Read", input: { file_path: "/w/cache.py" } },
        { kind: "tool", name: "Grep", input: { pattern: "x", path: "/w/src" } },
        { kind: "tool", name: "NotebookEdit", input: { notebook_path: "/w/n.ipynb" } },
      ],
    },
    { role: "user", key: "u2", text: "And eviction?" },
    { role: "assistant", key: "a2", text: "LRU.", segments: [{ kind: "tool", name: "Read", input: { file_path: "/w/other.py" } }] },
  ] as const;

  test("quotes the selected messages in turn order, tool cards dropped", () => {
    const { block, count } = buildBreakoutContext(turns, ["a1", "u1"], "Cache chat");
    expect(count).toBe(2);
    expect(block.startsWith(`<${BREAKOUT_CTX_TAG}>`)).toBe(true);
    expect(block.endsWith(`</${BREAKOUT_CTX_TAG}>`)).toBe(true);
    expect(block).toContain("User: How does the cache work?");
    expect(block).toContain("Assistant: It keys on path.");
    expect(block.indexOf("User: How")).toBeLessThan(block.indexOf("Assistant: It keys"));
    expect(block).not.toContain("And eviction?");
    expect(block).not.toContain('"pattern"');
  });
  test("lists the files the selected assistant turns touched, deduped", () => {
    const { block } = buildBreakoutContext(turns, ["u1", "a1"], "t");
    expect(block).toContain("Files these messages touched:");
    expect(block.match(/\/w\/cache\.py/g)).toHaveLength(1);
    expect(block).toContain("- /w/src");
    expect(block).toContain("- /w/n.ipynb");
    expect(block).not.toContain("/w/other.py");
  });
  test("the header names the parent title and first prompt; the title is optional", () => {
    const withTitle = buildBreakoutContext(turns, ["u2", "a2"], "Cache chat").block;
    expect(withTitle).toContain('"Cache chat"');
    expect(withTitle).toContain('"How does the cache work?"');
    expect(buildBreakoutContext(turns, ["u2", "a2"], "Cache chat").title).toBe("Cache chat");
    expect("title" in buildBreakoutContext(turns, ["u2", "a2"])).toBe(false);
    const bare = buildBreakoutContext(turns, ["u2", "a2"], undefined).block;
    expect(bare).not.toContain("titled");
    expect(bare).toContain('"How does the cache work?"');
  });
  test("a selected 10,000-char user prompt appears in full", () => {
    const big = "p".repeat(10000);
    const t = [
      { role: "user", key: "u", text: big },
      { role: "assistant", key: "a", text: "ok" },
    ] as const;
    const { block } = buildBreakoutContext(t, ["u", "a"], "t");
    expect(block).toContain(big);
    expect(block).not.toContain("…");
  });
  test("30 turns of 4,000 chars are all kept", () => {
    const many = Array.from({ length: 30 }, (_, i) => ({
      role: i % 2 ? "assistant" : "user",
      key: "k" + i,
      text: `m${i} ` + "z".repeat(3995),
    }));
    const { block, count } = buildBreakoutContext(many, many.map((t) => t.key), "t");
    expect(count).toBe(30);
    expect(block).toContain("User: m0 ");
    expect(block).toContain("Assistant: m29 ");
    expect(block).not.toContain("…");
  });
  test("a long title and a long first prompt appear in full", () => {
    const title = "T".repeat(500);
    const first = "f".repeat(500);
    const t = [
      { role: "user", key: "u0", text: first },
      { role: "assistant", key: "a", text: "reply" },
    ] as const;
    const r = buildBreakoutContext(t, ["a"], title);
    expect(r.block).toContain(JSON.stringify(title));
    expect(r.block).toContain(JSON.stringify(first));
    expect(r.title).toBe(title);
  });
  test("a tool-only assistant turn still contributes its files, not a message", () => {
    const t = [
      { role: "user", key: "u", text: "look" },
      { role: "assistant", key: "a", text: "", segments: [{ kind: "tool", name: "Read", input: { file_path: "/w/only.py" } }] },
    ] as const;
    const { block, count } = buildBreakoutContext(t, ["u", "a"], "t");
    expect(count).toBe(1);
    expect(block).toContain("- /w/only.py");
    expect(block).not.toContain("Assistant:");
  });
  test("a tool-only turn before the first text message still contributes its files", () => {
    const t = [
      { role: "assistant", key: "a", text: "", segments: [{ kind: "tool", name: "Read", input: { file_path: "/a/early.ts" } }] },
      { role: "user", key: "u", text: "then this" },
    ] as const;
    const { block, count } = buildBreakoutContext(t, ["a", "u"], "t");
    expect(count).toBe(1);
    expect(block).toContain("- /a/early.ts");
  });
  test("a selection of only tool-only turns still counts 0", () => {
    const t = [
      { role: "assistant", key: "a", text: "", segments: [{ kind: "tool", name: "Read", input: { file_path: "/a/early.ts" } }] },
    ] as const;
    expect(buildBreakoutContext(t, ["a"], "t").count).toBe(0);
  });
  test("the files list is still capped and tool inputs stay out", () => {
    const segs = Array.from({ length: 200 }, (_, i) => ({
      kind: "tool",
      name: "Read",
      input: { file_path: "/w/" + "d".repeat(150) + i, pattern: "SECRET_PATTERN" },
    }));
    const t = [
      { role: "user", key: "u", text: "q" },
      { role: "assistant", key: "a", text: "x", segments: segs },
    ];
    const { block } = buildBreakoutContext(t, ["u", "a"], "t");
    const files = block.split("Files these messages touched:\n")[1]!.split("\n</breakout-files>")[0]!;
    expect(files.length).toBeLessThanOrEqual(4000);
    expect(files.split("\n").length).toBeLessThan(200);
    expect(block).not.toContain("SECRET_PATTERN");
  });
  test("an annotations-only prompt quotes its annotations, not the invisible marker", () => {
    const t = [
      { role: "user", key: "u", text: "\u2063annotations", raw: "<annotations>\nA: the button is misaligned\n</annotations>" },
      { role: "user", key: "p", text: "\u2063pane screenshot" },
    ];
    const { block } = buildBreakoutContext(t, ["u", "p"], "t");
    expect(block).toContain("the button is misaligned");
    expect(block).toContain("sent pane screenshot");
    expect(block).not.toContain("\u2063");
  });
  test("selecting nothing readable yields no messages", () => {
    expect(buildBreakoutContext(turns, ["nope"], "t").count).toBe(0);
  });
});
