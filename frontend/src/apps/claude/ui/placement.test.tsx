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

// Owner feedback R4-3, the transcript half. The controller keeps a LANDED
// reply's row key for life and gives the follow-up's answer a new one
// (`landedWindow` / the per-slot `chunks` map). This pins that the transcript
// renders that as two bubbles in send order with the user bubble between them,
// and — the part a final-state assertion cannot see — that re-rendering with
// the follow-up appended leaves the earlier bubble's own node alone rather
// than rebuilding it under the newer text.
test("a landed reply keeps its own bubble when a follow-up's answer arrives (R4-3)", () => {
  const first = {
    role: "assistant" as const,
    key: "a:1",
    text: "Reply A, all of it.",
  };
  const asked = { role: "user" as const, key: "u:2", text: "now say done" };
  const second = {
    role: "assistant" as const,
    key: "a:2",
    text: "Reply B.",
    streaming: true as const,
  };
  const r = mount(<Transcript state={state({ turns: [first] })} actions={actions} />);
  const before = JSON.stringify(
    (() => {
      let node: Json | null = null;
      walk(r.toJSON() as Json, (n) => {
        if (cls(n).includes("assistant") && !node) node = n;
      });
      return node;
    })(),
  );

  act(() => {
    r.update(
      <Transcript state={state({ turns: [first, asked, second] })} actions={actions} />,
    );
  });

  // Three rows, in send order: the landed reply, the follow-up, its answer.
  // Bodies ride `dangerouslySetInnerHTML` (markdown is rendered, not a text
  // node), so a row's content is read off the serialized subtree.
  const rows: string[] = [];
  walk(r.toJSON() as Json, (n) => {
    const c = cls(n);
    if (c.includes("turn") && (c.includes("assistant") || c.includes("user"))) {
      rows.push(JSON.stringify(n));
    }
  });
  expect(rows.length).toBe(3);
  expect(rows[0]).toContain("Reply A, all of it.");
  expect(rows[1]).toContain("now say done");
  expect(rows[2]).toContain("Reply B.");
  // The answer to the follow-up is ONLY the answer to the follow-up: reply A
  // is not typed a second time under it.
  expect(rows[2]).not.toContain("Reply A");
  // …and the landed bubble is untouched — same node, same content, not rebuilt
  // beneath the newer one.
  let after: Json | null = null;
  walk(r.toJSON() as Json, (n) => {
    if (cls(n).includes("assistant") && !after) after = n;
  });
  expect(JSON.stringify(after)).toBe(before);
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

/** The `.chat-logwrap` scroller's classes (R3-4's lock rides here). */
function wrap(r: ReturnType<typeof create>): string[] {
  let found: Json | undefined;
  walk(r.toJSON() as Json, (n) => {
    if (cls(n).includes("chat-logwrap")) found = n;
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

// ── R3-4: ONE scroller, and R2-4's 70% cap is gone ─────────────────────────
test("a pinned card may fill the scrollport, and past it nothing else scrolls (R3-4)", () => {
  const r = mount(
    <Transcript
      state={state({
        turns: [turn("a:1", true)],
        permissions: [row({ placement: "open", parkedIn: null })],
      })}
      actions={actions}
    />,
  );
  // The ceiling and the inner scroll are one rule keyed off `is-pinned` (see
  // styles/transcript.css) — a card that is not blocking the run must not take
  // a ceiling, because nothing is sticking it anywhere.
  expect(pin(r)).toContain("is-pinned");
  const sheet = readFileSync(join(import.meta.dir, "../styles/transcript.css"), "utf8");
  const rule = sheet.slice(sheet.indexOf(".chat-root .chat-tailpin.is-pinned {"));
  const body = rule.slice(0, rule.indexOf("}"));
  // R2-4's 70% ceiling is exactly what put a second scrollbar beside the
  // transcript's, so it is gone and the pin may be the whole port.
  expect(body).not.toContain("70cqh");
  expect(body).toContain("max-height: 100cqh");
  expect(body).toContain("overflow-y: auto");
  // …and past that the transcript behind it stops scrolling altogether, which
  // is the half of the rule that makes "no double scroll" TRUE rather than
  // merely quieter.
  const lock = sheet.slice(sheet.indexOf(".chat-root .chat-logwrap.is-locked {"));
  expect(lock).not.toBe("");
  expect(lock.slice(0, lock.indexOf("}"))).toContain("overflow-y: hidden");
  // `cqh` only means anything if the scrollport is a size container.
  expect(sheet).toContain("container-type: size");
  // The lock is MEASURED and starts OFF: this renderer has no layout to
  // overflow, and a transcript locked because nothing could be measured would
  // be the worse of the two bugs.
  expect(wrap(r)).not.toContain("is-locked");
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

// ---- the receipt's scroll never outranks an OPEN card --------------------

/** `Transcript`'s scroll effects need real nodes. `createNodeMock` gives every
 *  host element the same stand-in, which is enough here: the assertion is WHICH
 *  calls happen, not where they land. */
function mountWithNodes(el: React.ReactElement) {
  const calls: string[] = [];
  const wheelHandlers: Array<(e: unknown) => void> = [];
  /** THE CARD NODES `findCard` walks. Real ones, with a `dataset.permId` — a
   *  mock whose `querySelectorAll` answered `[]` made the whole assertion
   *  vacuous (the lookup found nothing, so nothing ever scrolled either way).
   *  IN VIEW by their rect, which is the other half of the condition. */
  const cardNodes = ["p1", "p2"].map((id) => ({
    dataset: { permId: id },
    getBoundingClientRect: () => ({ top: 20, bottom: 60, width: 300, height: 40 }),
    scrollIntoView: () => calls.push("scrollIntoView:" + id),
  }));
  const node = {
    scrollIntoView: () => calls.push("scrollIntoView"),
    getBoundingClientRect: () => ({ top: 10, bottom: 400, width: 300, height: 390 }),
    // The port reads and writes these on every follow pass.
    scrollTop: 0,
    scrollHeight: 1000,
    clientHeight: 400,
    dataset: {},
    querySelectorAll: (sel: string) =>
      sel === "[data-perm-id]" ? cardNodes : [],
    querySelector: () => null,
    classList: { add() {}, remove() {}, contains: () => false },
    // The port binds a wheel listener on the scrollport and a ResizeObserver on
    // the pin; a stand-in has to answer both surfaces or the mount throws
    // before any effect this test is about can run. The wheel handler is KEPT,
    // because dispatching it is the only door to `followTail` from out here —
    // and both scrolls this file tests are gated on that flag, so a mock that
    // swallowed it made every assertion vacuous.
    addEventListener(type: string, fn: (e: unknown) => void) {
      if (type === "wheel") wheelHandlers.push(fn);
    },
    removeEventListener() {},
    isConnected: true,
    ownerDocument: globalThis.document,
    parentNode: null,
    children: [],
  };
  // The pin's own measure uses one; the suite has no DOM.
  const G = globalThis as Record<string, unknown>;
  const realRO = G.ResizeObserver;
  G.ResizeObserver = class {
    observe() {}
    disconnect() {}
  };
  restores.push(() => {
    if (realRO === undefined) delete G.ResizeObserver;
    else G.ResizeObserver = realRO;
  });

  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el, { createNodeMock: () => node });
  });
  mounted.push(r);
  return {
    calls,
    /** The scrollport's own follow flag, which BOTH scrolls read. A wheel-up is
     *  what drops it in the real component — "an unambiguous 'let me read', so
     *  it drops the follow with NO distance threshold" — so that is what this
     *  dispatches, rather than reaching for a ref it cannot see. */
    setFollowTail(on: boolean) {
      if (on) return;
      act(() => {
        for (const fn of wheelHandlers) fn({ deltaY: -120 });
      });
    },
    update(next: React.ReactElement) {
      act(() => r.update(next));
    },
  };
}


const restores: Array<() => void> = [];
afterEach(() => {
  for (const undo of restores.splice(0)) undo();
});

test("answering a card while ANOTHER opens does not scroll to the receipt", () => {
  // One poll can both answer a card and open the next one, and the open-card
  // effect runs FIRST — so the receipt pass would land last and pull the
  // viewport back to it, hiding the card the run is blocked on. An open card is
  // the hard block (T:14652-14663 scrolls to it unconditionally); a receipt is
  // a courtesy. T reaches the same answer by another road: `parkResolvedCard`
  // runs `followBottom()` first, and with a card open the log is following.
  // (Bugbot, PR #1074.)
  const open = row({ id: "p1", decision: "", placement: "open" });
  const view = (perms: PermissionRow[]) => (
    <Transcript state={state({ turns: [turn("a:1")], permissions: perms })} actions={actions} />
  );
  const h = mountWithNodes(view([open]));
  // Not following the tail: the reader has scrolled up to re-read the reply,
  // which is the only state either scroll is about.
  h.setFollowTail(false);
  h.calls.length = 0;

  // p1 answered and filed, p2 opens in the SAME update.
  h.update(
    view([
      row({ id: "p1", decision: "allow", placement: "parked", parkedIn: "a:1" }),
      row({ id: "p2", decision: "", placement: "open" }),
    ]),
  );
  expect(h.calls).not.toContain("scrollIntoView:p1");
});

test("…but with nothing open, the receipt IS brought into view", () => {
  // The courtesy still happens on the ordinary path — otherwise the guard above
  // would have quietly disabled the whole feature (T:14738-14742).
  const view = (perms: PermissionRow[]) => (
    <Transcript state={state({ turns: [turn("a:1")], permissions: perms })} actions={actions} />
  );
  const h = mountWithNodes(view([row({ id: "p1", decision: "", placement: "open" })]));
  h.setFollowTail(false);
  h.calls.length = 0;

  h.update(view([row({ id: "p1", decision: "allow", placement: "parked", parkedIn: "a:1" })]));
  expect(h.calls).toContain("scrollIntoView:p1");
});
