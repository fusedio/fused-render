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
import { INTERRUPT_MARK, isInterruptMark } from "./Turn";
import { readFileSync } from "node:fs";
import { join } from "node:path";

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
function textOf(node: Json | string | null): string {
  if (!node) return "";
  if (typeof node === "string") return node;
  return (node.children ?? []).map((k) => textOf(k as Json)).join("");
}
/** The log box's classes — `is-settling` is on it while the first paint is
 *  being held back (R2-11). */
function logCls(r: ReturnType<typeof create>): string[] {
  let out: string[] = [];
  walk(r.toJSON() as Json, (n) => {
    if (cls(n).includes("chat-log")) out = cls(n);
  });
  return out;
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
    adopting: false,
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

// ── the tail pin, and where a card goes when it is answered (#17, #18) ──────

/** The `.chat-tailpin` wrapper's classes. */
function pin(r: ReturnType<typeof create>): string[] {
  let found: Json | undefined;
  walk(r.toJSON() as Json, (n) => {
    if (cls(n).includes("chat-tailpin")) found = n;
  });
  return cls(found);
}

const toolTurn = (key: string, tools: Array<{ id: string; name: string }>) => ({
  role: "assistant" as const,
  key,
  text: "reply " + key,
  segments: [
    { kind: "text" as const, text: "before" },
    ...tools.map((t) => ({
      kind: "tool" as const,
      id: t.id,
      name: t.name,
      input: {},
      status: "ok" as const,
      output: null,
      images: [],
    })),
    { kind: "text" as const, text: "after" },
  ],
});

test("an OPEN card is STICKY above the composer, whatever the scroll (#17)", () => {
  const r = mount(
    <Transcript
      state={state({
        turns: [turn("a:1", true)],
        permissions: [row({ placement: "open", parkedIn: null })],
      })}
      actions={actions}
    />,
  );
  expect(pin(r)).toContain("is-pinned");
  // …and still in the bottom stack, not filed into a turn.
  expect(cards(r)[0].inTurn).toBe(null);
});

test("answering it UNSTICKS the pin and moves the card into the transcript (#17)", () => {
  const open = mount(
    <Transcript
      state={state({
        turns: [toolTurn("a:1", [{ id: "t1", name: "Bash" }])],
        permissions: [row({ id: "p1", tool: "Bash", placement: "open", parkedIn: null })],
      })}
      actions={actions}
    />,
  );
  expect(pin(open)).toContain("is-pinned");
  expect(cards(open)[0].inTurn).toBe(null);

  // The same card, answered: the controller parks it in the turn it was
  // answered in, and nothing is pinned any more.
  const done = mount(
    <Transcript
      state={state({
        turns: [toolTurn("a:1", [{ id: "t1", name: "Bash" }])],
        permissions: [
          row({ id: "p1", tool: "Bash", decision: "allow", placement: "parked", parkedIn: "a:1" }),
        ],
      })}
      actions={actions}
    />,
  );
  expect(pin(done)).not.toContain("is-pinned");
  expect(cards(done)[0].inTurn).not.toBe(null);
});

test("a resolved card parks right after the tool chip it answered (#18)", () => {
  const r = mount(
    <Transcript
      state={state({
        turns: [
          toolTurn("a:1", [
            { id: "t1", name: "Read" },
            { id: "t2", name: "Bash" },
          ]),
        ],
        permissions: [
          row({ id: "p1", tool: "Bash", decision: "allow", placement: "parked", parkedIn: "a:1" }),
        ],
      })}
      actions={actions}
    />,
  );
  // The card is a SIBLING of the chips, immediately after the Bash one — not at
  // the end of the turn behind the trailing paragraph.
  const body: Json[] = [];
  walk(r.toJSON() as Json, (n) => {
    if (cls(n).includes("body")) body.push(n);
  });
  const kids = (body[0].children ?? []) as Json[];
  const at = kids.findIndex((k) => cls(k).includes("perm"));
  expect(at).toBeGreaterThan(0);
  // The chip before it is the one it answered; a paragraph still follows.
  const before = JSON.stringify(kids[at - 1]);
  expect(before).toContain("Bash");
  expect(before).not.toContain("Read");
  expect(at).toBeLessThan(kids.length - 1);
});

test("an error row AND the trouble card, both (#27)", () => {
  const r = mount(
    <Transcript
      state={state({
        turns: [
          { role: "user", key: "u:1", text: "go" },
          { role: "error", key: "e:1", text: "Claude usage limit reached", kind: "generic" },
        ],
        trouble: { kind: "generic", message: "Claude usage limit reached" },
      })}
      actions={actions}
    />,
  );
  // The red row is the log entry, in `--c-error` via `.turn.error`; the card
  // beside it is the actionable surface. The port suppressed the row, which
  // left an API error with no last line saying the turn had stopped.
  const rows: Json[] = [];
  const troubles: Json[] = [];
  walk(r.toJSON() as Json, (n) => {
    if (cls(n).includes("error") && cls(n).includes("turn")) rows.push(n);
    if (cls(n).includes("trouble") && cls(n).includes("turn")) troubles.push(n);
  });
  expect(rows).toHaveLength(1);
  expect(troubles).toHaveLength(1);
});

// ── R2-2: the CLI's interrupt marker is a status line, not a bubble ─────────
test("`[Request interrupted by user]` renders as a note, not a user bubble (R2-2)", () => {
  const r = mount(
    <Transcript
      state={state({
        turns: [
          { role: "user", key: "u:1", text: "do the thing" },
          { role: "user", key: "u:2", text: INTERRUPT_MARK },
          { role: "user", key: "u:3", text: "  " + INTERRUPT_MARK + "\n" },
        ],
      })}
      actions={actions}
    />,
  );
  // The real prompt keeps its bubble; the marker gets neither a bubble nor the
  // `.user` class that right-aligns one.
  const bubbles: string[] = [];
  const notes: string[] = [];
  walk(r.toJSON() as Json, (n) => {
    if (cls(n).includes("bubble")) bubbles.push(textOf(n));
    if (cls(n).includes("note")) notes.push(textOf(n));
  });
  expect(bubbles).toEqual(["do the thing"]);
  // Both spellings of the marker land as notes — the record has carried a
  // trailing newline in some CLI builds.
  expect(notes).toEqual(["⏹Interrupted by you", "⏹Interrupted by you"]);
  expect(isInterruptMark(INTERRUPT_MARK)).toBe(true);
  // …and a prompt that merely TALKS about interrupts is still a prompt.
  expect(isInterruptMark("why did [Request interrupted by user] appear?")).toBe(false);
});

// ── R2-4: the pin is capped and scrolls itself ─────────────────────────────
test("the pinned tail is capped at a fraction of the scrollport and scrolls (R2-4)", () => {
  const r = mount(
    <Transcript
      state={state({
        turns: [turn("a:1", true)],
        permissions: [row({ placement: "open", parkedIn: null })],
      })}
      actions={actions}
    />,
  );
  // The cap and the inner scroll are one rule keyed off `is-pinned` (see
  // styles/transcript.css) — a card that is not blocking the run must not take
  // a ceiling, because nothing is sticking it anywhere.
  expect(pin(r)).toContain("is-pinned");
  const sheet = readFileSync(join(import.meta.dir, "../styles/transcript.css"), "utf8");
  const rule = sheet.slice(sheet.indexOf(".chat-root .chat-tailpin.is-pinned {"));
  const body = rule.slice(0, rule.indexOf("}"));
  expect(body).toContain("max-height: 70cqh");
  expect(body).toContain("overflow-y: auto");
  // `cqh` only means anything if the scrollport is a size container.
  expect(sheet).toContain("container-type: size");
});

// ── R2-11: one paint, not two ─────────────────────────────────────────────
test("the log stays hidden while a live run is still being adopted (R2-11)", () => {
  // `adopting` is the controller saying "there may be a run to attach to here",
  // and a tile that painted its turns first, scrolled, and THEN grew the card
  // flashed twice for one open.
  //
  // A ResizeObserver has to EXIST for the hold to arm at all: a host with no
  // layout to measure (this renderer, SSR) would be hidden for good, so
  // `settled` starts true there. Stubbed for the length of this test, which is
  // what puts the component on the browser's own path.
  const realRO = (globalThis as { ResizeObserver?: unknown }).ResizeObserver;
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  try {
  const adopting = mount(
    <Transcript
      state={{ ...state({ turns: [turn("a:1")] }), adopting: true } as ChatState}
      actions={actions}
    />,
  );
  expect(logCls(adopting)).toContain("is-settling");

  const settled = mount(
    <Transcript
      state={{ ...state({ turns: [turn("a:1")] }), adopting: false } as ChatState}
      actions={actions}
    />,
  );
  expect(logCls(settled)).not.toContain("is-settling");
  // A controller that never publishes the flag is unaffected: `undefined` is
  // not `true`.
  const legacy = mount(<Transcript state={state({ turns: [turn("a:1")] })} actions={actions} />);
  expect(logCls(legacy)).not.toContain("is-settling");
  } finally {
    (globalThis as { ResizeObserver?: unknown }).ResizeObserver = realRO;
  }
});
