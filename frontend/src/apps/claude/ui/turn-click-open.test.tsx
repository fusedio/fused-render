// CLICK A USER MESSAGE, OPEN ITS REPLY (prefs `turn_click_open`, off by
// default). It only ever opens: a reply already open stays open, and a click
// that is the end of a text selection is not a click on the message.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import type { ChatState, Turn as TurnRow } from "../protocol/controller-api";
const { Transcript } = await import("./Transcript");
const { publishTurnClickOpensReplyEnabled, resetFlagsForTests } = await import("../feature-flag");

const realFetch = globalThis.fetch;
const mounted: Array<ReturnType<typeof create>> = [];
beforeEach(() => {
  resetFlagsForTests();
  // Any prefs read answers "off": only the publish in `mount` turns it on.
  (globalThis as { fetch: unknown }).fetch = async () =>
    ({ ok: true, json: async () => ({}) }) as unknown as Response;
});
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetFlagsForTests();
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

type Json = ReactTestRendererJSON;
function byClass(r: ReturnType<typeof create>, cls: string): Json[] {
  const out: Json[] = [];
  const walk = (node: Json | Json[] | null) => {
    for (const n of Array.isArray(node) ? node : [node]) {
      if (!n || typeof n === "string") continue;
      const c = (n.props as { className?: string } | undefined)?.className;
      if (typeof c === "string" && c.split(/\s+/).includes(cls)) out.push(n);
      for (const k of n.children ?? []) walk(k as Json);
    }
  };
  walk(r.toJSON() as Json | Json[]);
  return out;
}

const user = (key: string): TurnRow => ({ role: "user", key, text: "ask " + key }) as TurnRow;
const assistant = (key: string): TurnRow =>
  ({ role: "assistant", key, text: "reply " + key + "\nand the rest of it" }) as TurnRow;

const state = (turns: TurnRow[]): ChatState =>
  ({
    file: "/proj",
    sessionId: "s1",
    runId: null,
    status: "idle",
    turns,
    permissions: [],
    appState: [],
    skills: [],
    working: null,
    trouble: null,
    permissionMode: "prompt",
    queued: [],
    historyLoading: false,
    adopting: false,
    adoptingRun: false,
    transcript: null,
    ownRunEndedAt: 0,
    repaired: 0,
    transcriptGen: 0,
    rev: 1,
  }) as unknown as ChatState;

const actions = {
  decidePermission: async () => {},
  answerQuestion: async () => {},
  decidePlan: async () => {},
  dismissCard: () => {},
  stopRun: async () => {},
  addNote: () => {},
};

async function mount(on: boolean) {
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(
      <Transcript
        state={state([user("u:1"), assistant("a:1"), user("u:2"), assistant("a:2")])}
        actions={actions}
      />,
    );
    await new Promise((res) => setTimeout(res, 0));
  });
  // Set the flag the way a Settings PUT does, not through the async prefs read:
  // that read is process-global and other suites in the same bun run share it,
  // so its timing is not this test's to rely on. A publish beats any read still
  // in flight (feature-flag `turnClickPublishedAt`).
  act(() => publishTurnClickOpensReplyEnabled(on));
  mounted.push(r);
  return r;
}

const folded = (r: ReturnType<typeof create>) => byClass(r, "turn-collapsed").length;
const click = (r: ReturnType<typeof create>, i: number) => {
  const onClick = (byClass(r, "bubble")[i]!.props as { onClick?: () => void }).onClick;
  expect(onClick).toBeDefined();
  act(() => onClick!());
};

describe("a click on a user message", () => {
  test("opens the folded reply under it, and a second click leaves it open", async () => {
    const r = await mount(true);
    expect(folded(r)).toBe(1); // a:1 folded, the newest (a:2) open
    click(r, 0);
    expect(folded(r)).toBe(0);
    click(r, 0);
    expect(folded(r)).toBe(0);
  });

  test("a message whose reply is already open changes nothing", async () => {
    const r = await mount(true);
    click(r, 1);
    expect(folded(r)).toBe(1);
  });

  test("with the flag off the bubble is inert", async () => {
    const r = await mount(false);
    expect((byClass(r, "bubble")[0]!.props as { onClick?: unknown }).onClick).toBeUndefined();
    expect(folded(r)).toBe(1);
  });
});
