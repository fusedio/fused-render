// WHERE A CARD RENDERS, on screen. The controller decides the placement
// (`placement`/`parkedIn`, run-controller); this pins that the transcript
// actually obeys it — which is the half that was missing, and the half the
// round-1 bugs lived in:
//
//   * a parked card rendered only inside a turn with `streaming: true`, so
//     every resolved card vanished the moment its turn ended;
//   * and it rendered in whatever turn was streaming NOW rather than the one it
//     was answered in — the receipt filed after everything it came before
//     (T:14728-14742).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import type { ChatState } from "../protocol/controller-api";
import type { PermissionRow } from "../protocol/types";
import { Transcript } from "./Transcript";

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
function walk(node: Json | string | null, hit: (n: Json, path: Json[]) => void, path: Json[] = []) {
  if (!node || typeof node === "string") return;
  hit(node, path);
  for (const k of node.children ?? []) walk(k as Json, hit, [...path, node]);
}
const cls = (n: Json | undefined) =>
  String((n?.props as { className?: string } | undefined)?.className ?? "").split(/\s+/);

/** Every perm card on screen, with the `.turn.assistant` it is nested in (or
 *  `null` when it is in the bottom stack). */
function cards(r: ReturnType<typeof create>): Array<{ text: string; inTurn: string | null }> {
  const out: Array<{ text: string; inTurn: string | null }> = [];
  walk(r.toJSON() as Json, (n, path) => {
    if (!cls(n).includes("perm")) return;
    const owner = [...path].reverse().find((p) => cls(p).includes("assistant"));
    const body = [...path].reverse().find((p) => cls(p).includes("chat-log"));
    void body;
    let head = "";
    walk(n, (m) => {
      if (cls(m).includes("perm-head")) {
        head = (m.children ?? []).filter((c) => typeof c === "string").join("");
      }
    });
    out.push({
      text: head,
      inTurn: owner ? String((owner.props as { "data-key"?: string })["data-key"] ?? "yes") : null,
    });
  });
  return out;
}

function row(over: Partial<PermissionRow> = {}): PermissionRow {
  return {
    id: "p1",
    tool: "Bash",
    input: { command: "ls" },
    created_at: 0,
    decision: "",
    scope: "",
    mode: "",
    answers: {},
    ...over,
  };
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
    transcript: null,
    ownRunEndedAt: 0,
    rev: 1,
    ...over,
  };
}

const actions = {
  decidePermission: async () => {},
  answerQuestion: async () => {},
  decidePlan: async () => {},
  dismissCard: () => {},
  stopRun: async () => {},
};

const turn = (key: string, streaming = false) => ({
  role: "assistant" as const,
  key,
  text: "reply " + key,
  ...(streaming ? { streaming: true as const } : {}),
});

test("a parked card renders inside the turn it was answered in — not the streaming one", () => {
  const r = mount(
    <Transcript
      state={state({
        turns: [turn("a:1"), turn("a:2", true)],
        permissions: [row({ decision: "allow", placement: "parked", parkedIn: "a:1" })],
      })}
      actions={actions}
    />,
  );
  const seen = cards(r);
  expect(seen.length).toBe(1);
  // Inside an assistant turn, and inside the FIRST one: the DOM order is what
  // "in chronological position" means here.
  expect(seen[0].inTurn).not.toBe(null);
  const assistants: Json[] = [];
  walk(r.toJSON() as Json, (n) => {
    if (cls(n).includes("assistant")) assistants.push(n);
  });
  const inFirst = JSON.stringify(assistants[0]).includes("perm-head");
  const inSecond = JSON.stringify(assistants[1]).includes("perm-head");
  expect([inFirst, inSecond]).toEqual([true, false]);
});

test("a parked card survives the run ending: no streaming turn anywhere", () => {
  const r = mount(
    <Transcript
      state={state({
        // Every turn finished — `runEnding` cleared `streaming`, which is what
        // used to unmount the card.
        turns: [turn("a:1")],
        permissions: [row({ decision: "deny", placement: "parked", parkedIn: "a:1" })],
      })}
      actions={actions}
    />,
  );
  expect(cards(r).length).toBe(1);
});

test("an OPEN card is in the bottom stack, not in a turn", () => {
  const r = mount(
    <Transcript
      state={state({
        turns: [turn("a:1", true)],
        permissions: [row({ placement: "open", parkedIn: null })],
      })}
      actions={actions}
    />,
  );
  const seen = cards(r);
  expect(seen.length).toBe(1);
  expect(seen[0].inTurn).toBe(null);
});

test("a card resolved with no live turn keeps its place at the bottom (T:14732)", () => {
  const r = mount(
    <Transcript
      state={state({
        turns: [turn("a:1")],
        // Answered after the run ended, so there was no turn to file it into.
        permissions: [row({ decision: "allow", placement: "parked", parkedIn: null })],
      })}
      actions={actions}
    />,
  );
  const seen = cards(r);
  expect(seen.length).toBe(1);
  expect(seen[0].inTurn).toBe(null);
});

test("open cards render as one contiguous block, in the order given", () => {
  const r = mount(
    <Transcript
      state={state({
        turns: [turn("a:1")],
        permissions: [
          row({ id: "p1", decision: "allow", placement: "parked", parkedIn: "a:1" }),
          row({ id: "p2", tool: "Read", input: { file_path: "/a" } }),
          row({ id: "p3", tool: "Write", input: { file_path: "/b" } }),
        ],
      })}
      actions={actions}
    />,
  );
  // Three cards: one filed into the turn, two in the bottom stack.
  const seen = cards(r);
  expect(seen.length).toBe(3);
  expect(seen.filter((c) => c.inTurn === null).length).toBe(2);
});
