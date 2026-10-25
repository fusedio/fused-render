// THE `Worked for 2m 10s` LINE under a finished reply: drawn on a settled
// assistant turn only, once per user message (the last reply before the next
// user turn), measured from that user turn.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import type { ChatState, Turn as TurnRow } from "../protocol/controller-api";
import type { Segment } from "../protocol/types";
// DYNAMIC, after the shim (the `AnnStrip.test.tsx` pattern): `Transcript`
// reaches `platform/lib/router` through the card stack, and that module reads
// `location` at module init — static imports are hoisted above the
// `installDomShim()` call above.
const { Transcript } = await import("./Transcript");

const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement) {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

type Json = ReactTestRendererJSON;
function walk(node: Json | Json[] | null, hit: (n: Json) => void): void {
  for (const n of Array.isArray(node) ? node : [node]) {
    if (!n || typeof n === "string") continue;
    hit(n);
    for (const k of n.children ?? []) walk(k as Json, hit);
  }
}
function byClass(r: ReturnType<typeof create>, cls: string): Json[] {
  const out: Json[] = [];
  walk(r.toJSON() as Json | Json[], (n) => {
    const c = (n.props as { className?: string } | undefined)?.className;
    if (typeof c === "string" && c.split(/\s+/).includes(cls)) out.push(n);
  });
  return out;
}
function words(node: Json | null): string {
  const out: string[] = [];
  const go = (n: Json | string | null) => {
    if (!n) return;
    if (typeof n === "string") {
      out.push(n);
      return;
    }
    // The folded line is RENDERED MARKDOWN now (Akshil, 2026-09-15): the text
    // sits in `dangerouslySetInnerHTML`, so read it back with the tags
    // stripped — the assertions below are about the words, not the markup.
    const h = (n.props as { dangerouslySetInnerHTML?: { __html: string } } | undefined)
      ?.dangerouslySetInnerHTML;
    if (h) out.push(h.__html.replace(/<[^>]+>/g, "").trim());
    for (const k of n.children ?? []) go(k as Json | string);
  };
  go(node);
  return out.join("");
}

function state(over: Partial<ChatState> = {}): ChatState {
  return {
    file: "/proj",
    sessionId: "s1",
    runId: null,
    status: "idle",
    turns: [],
    permissions: [],
    appState: [],
    skills: [],
    working: null,
    trouble: null,
    permissionMode: "prompt",
    queued: [],
    historyLoading: false,
    adopting: false,
    transcript: null,
    ownRunEndedAt: 0,
    repaired: 0,
    transcriptGen: 0,
    rev: 1,
    ...over,
  } as ChatState;
}

const actions = {
  decidePermission: async () => {},
  answerQuestion: async () => {},
  decidePlan: async () => {},
  dismissCard: () => {},
  stopRun: async () => {},
};


const T0 = 1_791_108_000;
const user = (key: string, ts?: number, over: Record<string, unknown> = {}): TurnRow =>
  ({ role: "user", key, text: "q " + key, ...(ts !== undefined ? { ts } : {}), ...over }) as TurnRow;
const reply = (key: string, over: Record<string, unknown> = {}): TurnRow =>
  ({ role: "assistant", key, text: "reply " + key + "\nmore", ...over }) as TurnRow;
const log = (turns: TurnRow[], over: Partial<ChatState> = {}) =>
  mount(<Transcript state={state({ turns, ...over })} actions={actions} />);
const worked = (r: ReturnType<typeof create>) => byClass(r, "turn-worked").map((n) => words(n));

describe("Worked for line", () => {
  test("a settled reply wears it, measured from the user turn off the segment clocks", () => {
    const segs: Segment[] = [{ kind: "text", text: "a\nb", ts: T0 + 3, ended: T0 + 130 }];
    const r = log([user("u1", T0), reply("a1", { segments: segs })]);
    expect(worked(r)).toEqual(["Worked for 2m 10s"]);
  });
  test("a wait on the user is shown apart from the work", () => {
    const segs = [{ kind: "tool", id: "t", name: "Bash", input: {}, status: "ok", output: "", images: [], ts: T0 + 3, ended: T0 + 2530, waited: 2400 }] as unknown as Segment[];
    const r = log([user("u1", T0), reply("a1", { segments: segs })]);
    expect(worked(r)).toEqual(["Worked for 2m 10s · waited 40m on you"]);
  });
  test("a live turn's client end stamp is the fallback", () => {
    const r = log([user("u1", T0), reply("a1", { endedAt: T0 + 65 })]);
    expect(worked(r)).toEqual(["Worked for 1m 5s"]);
  });
  test("not on the streaming turn, nor while the run is going", () => {
    expect(worked(log([user("u1", T0), reply("a1", { streaming: true, endedAt: T0 + 65 })]))).toEqual([]);
    expect(worked(log([user("u1", T0), reply("a1", { endedAt: T0 + 65 })], { status: "running" }))).toEqual([]);
  });
  test("a reply split in two shows it once, on the last, from the user turn", () => {
    const r = log([user("u1", T0), reply("a1", { endedAt: T0 + 10 }), reply("a2", { endedAt: T0 + 100 })]);
    expect(worked(r)).toEqual(["Worked for 1m 40s"]);
  });
  test("one line per user message", () => {
    // a one-liner is never folded; a folded older reply draws no line
    const r = log([
      user("u1", T0),
      reply("a1", { text: "ok", endedAt: T0 + 10 }),
      user("u2", T0 + 100),
      reply("a2", { endedAt: T0 + 160 }),
    ]);
    expect(worked(r)).toEqual(["Worked for 10s", "Worked for 1m"]);
  });
  test("nothing when no span can be computed", () => {
    expect(worked(log([user("u1"), reply("a1")]))).toEqual([]);
  });
});
