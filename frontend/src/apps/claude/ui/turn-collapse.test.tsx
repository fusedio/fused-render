// A FOLDED REPLY (design.md §B): the ✻ mark is the toggle, the collapsed row is
// one ellipsized line, and which turns land folded is the log's rule rather than
// the row's.
//
// The bug this pins is the one the rule is written to avoid: "every settled turn
// but the last" re-evaluated on every render folds the reply a reader is in the
// middle of the moment the next turn starts.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { readFileSync } from "node:fs";
import { join } from "node:path";

import type { ChatState, Turn as TurnRow } from "../protocol/controller-api";
import type { Segment, ToolSegment } from "../protocol/types";
import { CardPolicyProvider, createCardPolicy } from "./cardPolicy";
// DYNAMIC, after the shim (the `AnnStrip.test.tsx` pattern): `Transcript`
// reaches `platform/lib/router` through the card stack, and that module reads
// `location` at module init — static imports are hoisted above the
// `installDomShim()` call above.
const { Transcript } = await import("./Transcript");
const { collapsedLine, firstLine, INTERRUPT_MARK, isOneLiner, Turn } = await import("./Turn");
const { historyToTurns } = await import("../protocol/history");

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
/** The folded one-liners on screen, in order. */
const folded = (r: ReturnType<typeof create>) => byClass(r, "turn-collapsed").map((n) => words(n));
/** Every assistant turn's mark, in order. */
const marks = (r: ReturnType<typeof create>) => byClass(r, "dot");

const tool = (id: string, name: string): ToolSegment => ({
  kind: "tool",
  id,
  name,
  input: { file_path: "/tmp/a.ts" },
  status: "ok",
  output: "",
  images: [],
});
const text = (t: string): Segment => ({ kind: "text", text: t });

// TWO LINES, because a ONE-line reply is never foldable (`Turn`'s
// `isOneLiner`) and every test below is about the fold. The folded row shows
// the FIRST line, so every `folded(...)` expectation is still `reply <key>`.
const assistant = (key: string, over: Partial<TurnRow> = {}): TurnRow =>
  ({ role: "assistant", key, text: "reply " + key + "\nand the rest of it", ...over }) as TurnRow;

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

const log = (turns: TurnRow[], over: Partial<ChatState> = {}) =>
  mount(<Transcript state={state({ turns, ...over })} actions={actions} />);

/** One unanswered permission row. `toolUseId` is the CLIENT annotation
 *  `syncPermissions` stamps from the request's `tool_use_id` — the id of the
 *  call that asked, and the only thing that says WHICH turn is blocked. */
const card = (over: Record<string, unknown> = {}) =>
  [
    {
      id: "p1",
      tool: "Bash",
      input: { command: "ls" },
      created_at: 0,
      decision: "",
      scope: "",
      mode: "",
      answers: {},
      ...over,
    },
  ] as ChatState["permissions"];

describe("which replies land folded", () => {
  test("every settled reply but the LAST one", () => {
    const r = log([assistant("a:1"), assistant("a:2"), assistant("a:3")]);
    expect(folded(r)).toEqual(["reply a:1", "reply a:2"]);
  });

  test("the live turn is never folded", () => {
    const r = log([assistant("a:1"), assistant("a:2", { streaming: true })]);
    // The streaming one is open, and its mark cannot be pressed.
    expect(folded(r)).toEqual(["reply a:1"]);
    expect((marks(r)[1]!.props as { disabled?: boolean }).disabled).toBe(true);
  });

  test("A NEW RESPONSE FOLDS THE ONE THE RULE LEFT OPEN (Akshil 2026-09-15)", () => {
    // The landing's last reply is open because the RULE opened it. A new
    // response starting is the rule closing its own door: one reply on screen,
    // the one being written.
    const r = log([assistant("a:1")]);
    expect(folded(r)).toEqual([]);
    act(() => {
      r.update(
        <Transcript
          state={state({ turns: [assistant("a:1"), assistant("a:2", { streaming: true })] })}
          actions={actions}
        />,
      );
    });
    expect(folded(r)).toEqual(["reply a:1"]);
    // …and the new one is open, with a mark that cannot be pressed while it
    // streams.
    expect((marks(r)[1]!.props as { disabled?: boolean }).disabled).toBe(true);
  });

  test("A REPLY THAT GOES AWAY HANDS THE FOLD BACK (bugbot)", () => {
    // The new response folded the one before it — then the new row itself
    // disappeared: a failed poll dropped its chunk, or `runEnding` discarded
    // it. The previous reply is the newest again, so it is open again. A
    // one-way sweep left it folded and made the reader click to get back the
    // answer they were part-way through.
    const r = log([assistant("a:1")]);
    expect(folded(r)).toEqual([]);
    act(() => {
      r.update(
        <Transcript
          state={state({ turns: [assistant("a:1"), assistant("a:2", { streaming: true })] })}
          actions={actions}
        />,
      );
    });
    expect(folded(r)).toEqual(["reply a:1"]);
    act(() => {
      r.update(<Transcript state={state({ turns: [assistant("a:1")] })} actions={actions} />);
    });
    expect(folded(r)).toEqual([]);
  });

  test("A REPLY THE READER OPENED SURVIVES EVERY LATER RESPONSE", () => {
    const r = log([assistant("a:1"), assistant("a:2")]);
    expect(folded(r)).toEqual(["reply a:1"]);
    // They click the old folded one open. That is a MANUAL open, and nothing
    // but another click closes it.
    act(() => (marks(r)[0]!.props as { onClick?: () => void }).onClick!());
    expect(folded(r)).toEqual([]);
    const turns = [assistant("a:1"), assistant("a:2")];
    for (const next of ["a:3", "a:4"]) {
      turns.push(assistant(next, { streaming: true }));
      act(() => {
        r.update(<Transcript state={state({ turns: [...turns] })} actions={actions} />);
      });
      // The rule's own open reply folded; the reader's did not.
      expect(folded(r)).not.toContain("reply a:1");
      turns[turns.length - 1] = assistant(next);
    }
    expect(folded(r)).toEqual(["reply a:2", "reply a:3"]);
  });

  test("A REPLY THE READER SHUT STAYS SHUT", () => {
    // The landing's last turn, closed on purpose: a later response must not
    // hand it back, and neither must anything else.
    const r = log([assistant("a:1"), assistant("a:2")]);
    act(() => (marks(r)[1]!.props as { onClick?: () => void }).onClick!());
    expect(folded(r)).toEqual(["reply a:1", "reply a:2"]);
    act(() => {
      r.update(
        <Transcript
          state={state({
            turns: [assistant("a:1"), assistant("a:2"), assistant("a:3", { streaming: true })],
          })}
          actions={actions}
        />,
      );
    });
    expect(folded(r)).toEqual(["reply a:1", "reply a:2"]);
  });

  test("OPENED THEN CLOSED IS CLOSED — the last click is the one that counts", () => {
    const r = log([assistant("a:1"), assistant("a:2")]);
    const press = () => act(() => (marks(r)[0]!.props as { onClick?: () => void }).onClick!());
    press(); // manual-open
    press(); // manual-closed
    expect(folded(r)).toEqual(["reply a:1"]);
    act(() => {
      r.update(
        <Transcript
          state={state({
            turns: [assistant("a:1"), assistant("a:2"), assistant("a:3", { streaming: true })],
          })}
          actions={actions}
        />,
      );
    });
    expect(folded(r)).toEqual(["reply a:1", "reply a:2"]);
  });

  test("a turn holding an unanswered card stays open, and its mark is dead", () => {
    const r = log([assistant("a:1"), assistant("a:2", { segments: [tool("t9", "Bash")] })], {
      permissions: card({ toolUseId: "t9" }),
    });
    expect(folded(r)).toEqual(["reply a:1"]);
    expect((marks(r)[1]!.props as { disabled?: boolean }).disabled).toBe(true);
  });

  test("THE CARD'S OWN TURN, not whichever reply is newest (review #2)", () => {
    // The card asks about a call in the FIRST turn. It used to unfold the last
    // one instead — a turn that has nothing to do with the block, and one the
    // reader may well have folded on purpose.
    const r = log([assistant("a:1", { segments: [tool("t9", "Bash")] }), assistant("a:2")], {
      permissions: card({ toolUseId: "t9" }),
    });
    expect(folded(r)).toEqual([]);
    expect((marks(r)[0]!.props as { disabled?: boolean }).disabled).toBe(true);
    expect((marks(r)[1]!.props as { disabled?: boolean }).disabled).toBe(false);
  });

  test("a card with no chip to point at protects the LIVE turn and nothing else", () => {
    // No `toolUseId` (an AskUserQuestion, a plan, an older agent.py): the only
    // turn it can be blocking that the reader could not have folded themselves
    // is the streaming one, so the guess stops there.
    const settled = log([assistant("a:1"), assistant("a:2")], { permissions: card() });
    expect((marks(settled)[1]!.props as { disabled?: boolean }).disabled).toBe(false);
    const live = log([assistant("a:1"), assistant("a:2", { streaming: true })], {
      permissions: card(),
    });
    expect((marks(live)[1]!.props as { disabled?: boolean }).disabled).toBe(true);
  });

  test("a card NEVER re-opens a reply the reader folded (review #2)", () => {
    const turns = [assistant("a:1"), assistant("a:2", { segments: [tool("t9", "Bash")] })];
    const r = log(turns);
    const press = () => {
      const onClick = (marks(r)[1]!.props as { onClick?: () => void }).onClick!;
      act(() => onClick());
    };
    press();
    expect(folded(r)).toHaveLength(2);
    // …and now the run blocks on a call inside the very turn they shut.
    act(() => {
      r.update(
        <Transcript
          state={state({ turns, permissions: card({ toolUseId: "t9" }) })}
          actions={actions}
        />,
      );
    });
    expect(folded(r)).toHaveLength(2);
  });

  test("AN UNANSWERED CARD HOLDS ITS TURN OPEN PAST THE NEXT REPLY (Akshil 2026-09-15)", () => {
    // The card is the one thing on screen to do, and it is answered against a
    // chip in `a:1`. A new reply starting made `a:1` "not the newest", so the
    // rule derived it CLOSED underneath — invisible only because `pendingCard`
    // kept it drawn open. The reply therefore snapped shut in the same gesture
    // that pressed Allow, which is the reader's own click taking the thing they
    // were reading away from them.
    const turns = [assistant("a:1", { segments: [tool("t9", "Bash") ] }), assistant("a:2")];
    const blocked = card({ toolUseId: "t9" });
    const r = log(turns, { permissions: blocked });
    expect(folded(r)).toEqual([]);
    act(() => {
      r.update(
        <Transcript
          state={state({
            turns: [...turns, assistant("a:3", { streaming: true })],
            permissions: blocked,
          })}
          actions={actions}
        />,
      );
    });
    // `a:2` folds as any settled reply does; the blocked one is untouched.
    expect(folded(r)).toEqual(["reply a:2"]);
    // …and it is only exempt while the card stands: answered, it is an ordinary
    // reply again and the newest one is the only one open.
    act(() => {
      r.update(
        <Transcript
          state={state({
            turns: [...turns, assistant("a:3", { streaming: true })],
            permissions: card({ toolUseId: "t9", decision: "allow" }),
          })}
          actions={actions}
        />,
      );
    });
    expect(folded(r)).toEqual(["reply a:1", "reply a:2"]);
  });

  test("THE FOLDS SURVIVE A HISTORY RE-READ (Akshil 2026-09-15)", () => {
    // `refreshHistory` re-emits the whole conversation whenever the transcript
    // grows underneath the page. A restored reply used to be keyed by its
    // POSITION in that payload, so any re-read that shifted the rows moved
    // every fold the reader had set one turn down the log: the reply they had
    // opened folded itself and its neighbour opened instead. agent.py now sends
    // the reply's own record id and `historyToTurns` keys by it.
    const stat = { path: "/t.jsonl", mtime: 1, size: 2 };
    // Two lines apiece, for the reason the `assistant` helper has two: a
    // one-line reply is never foldable, and this test is about folds.
    const body = (uuid: string) => "reply " + uuid + "\nand the rest of it";
    const rows = ["r1", "r2", "r3", "r4"].map((uuid) => ({
      role: "assistant" as const,
      text: body(uuid),
      uuid,
    }));
    const read = (extra: typeof rows) =>
      historyToTurns({ turns: [...extra, ...rows], transcript: stat }) as TurnRow[];
    const r = log(read([]));
    expect(folded(r)).toEqual(["reply r1", "reply r2", "reply r3"]);
    // Turn 3 opened on purpose, the last one shut on purpose.
    act(() => (marks(r)[2]!.props as { onClick?: () => void }).onClick!());
    act(() => (marks(r)[3]!.props as { onClick?: () => void }).onClick!());
    expect(folded(r)).toEqual(["reply r1", "reply r2", "reply r4"]);
    // The re-read carries an EARLIER row this page had not seen — the shift a
    // positional key cannot survive. Same conversation, so the same generation.
    act(() => {
      r.update(
        <Transcript
          state={state({
            turns: read([{ role: "assistant" as const, text: body("r0"), uuid: "r0" }]),
          })}
          actions={actions}
        />,
      );
    });
    expect(folded(r)).toEqual(["reply r0", "reply r1", "reply r2", "reply r4"]);
  });

  test("ANOTHER CONVERSATION IS ANOTHER MAP (review #1)", () => {
    // A restored turn's key is POSITIONAL (`protocol/history.ts`, "h:" + i) and
    // this component is not remounted between two sessions — so a 20-turn
    // history's folds applied themselves row for row to the 12-turn one that
    // replaced it, and the reply the reader came back for landed folded.
    const r = log([assistant("h:0"), assistant("h:1"), assistant("h:2")]);
    expect(folded(r)).toEqual(["reply h:0", "reply h:1"]);
    act(() => {
      r.update(
        <Transcript
          state={state({ turns: [assistant("h:0"), assistant("h:1")], transcriptGen: 1 })}
          actions={actions}
        />,
      );
    });
    // The new conversation's LAST turn is open, and only the ones before it are
    // folded.
    expect(folded(r)).toEqual(["reply h:0"]);
  });
});

describe("the toggle", () => {
  test("a click folds the newest reply, and a second click opens it again", () => {
    const r = log([assistant("a:1"), assistant("a:2")]);
    const press = () => {
      const onClick = (marks(r)[1]!.props as { onClick?: () => void }).onClick!;
      act(() => onClick());
    };
    expect(folded(r)).toEqual(["reply a:1"]);
    press();
    expect(folded(r)).toEqual(["reply a:1", "reply a:2"]);
    press();
    expect(folded(r)).toEqual(["reply a:1"]);
  });

  test("the click drops the follow before the log changes height (review #3)", () => {
    // Folding a reply makes `.chat-log` shorter and unfolding makes it taller;
    // the scrollport answers a resize by writing `scrollTop = scrollHeight`, so
    // the reply the reader just opened was yanked off the bottom of the screen.
    // The toggle turns the follow off first, exactly as the `?msg=` anchor does.
    // The same hook reaches the disclosures INSIDE a turn, which is what the
    // policy's `holdTail` is (ui/cardPolicy).
    const policy = createCardPolicy();
    const r = mount(
      <CardPolicyProvider value={policy}>
        <Transcript state={state({ turns: [assistant("a:1")] })} actions={actions} />
      </CardPolicyProvider>,
    );
    expect(typeof policy.holdTail).toBe("function");
    // And it is gone with the scrollport it belongs to.
    act(() => r.unmount());
    expect(policy.holdTail).toBeUndefined();
  });

  test("the mark says what it controls, and says nothing when it controls nothing (#7)", () => {
    const turn = assistant("a:1", { segments: [text("The answer.\nAnd the rest.")] });
    const live = mount(<Turn turn={assistant("a:2", { streaming: true })} />);
    const dead = marks(live)[0]!.props as Record<string, unknown>;
    expect(dead.disabled).toBe(true);
    expect("aria-expanded" in dead).toBe(false);
    expect("aria-controls" in dead).toBe(false);

    const r = mount(<Turn turn={turn} onToggleCollapse={() => {}} />);
    const mark = marks(r)[0]!.props as Record<string, unknown>;
    expect(mark["aria-expanded"]).toBe(true);
    const body = byClass(r, "body")[0]!.props as { id?: string };
    expect(typeof body.id).toBe("string");
    expect(mark["aria-controls"]).toBe(body.id);
  });

  test("it says which way it goes, and nothing else renders when it is shut", () => {
    const turn = assistant("a:1", { segments: [text("The answer."), tool("t1", "Read")] });
    const shut = mount(<Turn turn={turn} collapsed onToggleCollapse={() => {}} />);
    expect((marks(shut)[0]!.props as { "aria-label": string })["aria-label"]).toBe(
      "Expand response",
    );
    expect(folded(shut)).toEqual(["The answer."]);
    // No chips, no triggers, nothing but the line.
    expect(byClass(shut, "run-trigger")).toHaveLength(0);
    expect(byClass(shut, "toolchip")).toHaveLength(0);

    const open = mount(<Turn turn={turn} onToggleCollapse={() => {}} />);
    expect((marks(open)[0]!.props as { "aria-label": string })["aria-label"]).toBe(
      "Collapse response",
    );
    expect(byClass(open, "run-trigger")).toHaveLength(1);
  });

  test("the folded mark greys and says what a click will do (Akshil 2026-09-15)", () => {
    const turn = assistant("a:1", { segments: [text("The answer.\nAnd the rest.")] });
    const shut = mount(<Turn turn={turn} collapsed onToggleCollapse={() => {}} />);
    // GREY IS CSS, off `.turn.is-folded` — what the row owes the stylesheet is
    // the class.
    expect(
      (byClass(shut, "turn")[0]!.props as { className: string }).className,
    ).toContain("is-folded");
    expect((marks(shut)[0]!.props as Record<string, unknown>)["data-hint"]).toBe(
      "Expand response",
    );
    const open = mount(<Turn turn={turn} onToggleCollapse={() => {}} />);
    expect((byClass(open, "turn")[0]!.props as { className: string }).className).not.toContain(
      "is-folded",
    );
    expect((marks(open)[0]!.props as Record<string, unknown>)["data-hint"]).toBe(
      "Collapse response",
    );
    // NOTHING while the mark is dead: a hint over a `disabled` control names an
    // action that will not happen.
    const live = mount(<Turn turn={assistant("a:2", { streaming: true })} />);
    expect("data-hint" in (marks(live)[0]!.props as Record<string, unknown>)).toBe(false);
  });

  test("THE FOLDED LINE ITSELF OPENS THE REPLY (Akshil 2026-09-15)", () => {
    // The mark is a 12px glyph in the gutter; the row a reader aims at is the
    // words. Same handler, same state — a bigger target for the one control.
    const turn = assistant("a:1", { segments: [text("The answer."), tool("t1", "Read")] });
    const hit: string[] = [];
    const shut = mount(<Turn turn={turn} collapsed onToggleCollapse={(k) => hit.push(k)} />);
    const line = byClass(shut, "turn-collapsed")[0]!.props as Record<string, unknown>;
    act(() => (line["onClick"] as () => void)());
    expect(hit).toEqual(["a:1"]);
    // A POINTER TARGET AND NOTHING MORE (PR5 review #7). The mark beside it is
    // a real `<button>` carrying the state; announcing the words as a SECOND
    // button gave a keyboard reader two stops for one action and read the
    // reply's own first sentence out as a control's label.
    expect("role" in line).toBe(false);
    expect("tabIndex" in line).toBe(false);
    expect("aria-expanded" in line).toBe(false);
    expect("onKeyDown" in line).toBe(false);
    // ONE WAY ONLY: the open body is not a control — only the mark folds.
    const open = mount(<Turn turn={turn} onToggleCollapse={(k) => hit.push(k)} />);
    expect((byClass(open, "body")[0]!.props as Record<string, unknown>)["onClick"]).toBe(undefined);
    expect(byClass(open, "turn-collapsed")).toHaveLength(0);
  });

  test("the muted line still answers the pointer (review #4)", () => {
    // `.is-muted` outranked `.turn-collapsed:hover`, so the one row whose words
    // are its only affordance was the one row that looked dead under the
    // cursor — an all-tool-calls reply.
    const sheet = readFileSync(join(import.meta.dir, "../styles/transcript.css"), "utf8");
    const at = sheet.indexOf(".chat-root .turn.assistant .turn-collapsed.is-muted:hover {");
    expect(at).toBeGreaterThan(-1);
    expect(sheet.slice(at, sheet.indexOf("}", at))).toContain("color: var(--c-dim)");
    // …and it comes AFTER the rule it has to beat.
    expect(at).toBeGreaterThan(
      sheet.indexOf(".chat-root .turn.assistant .turn-collapsed.is-muted {"),
    );
  });

  test("with no handler at all the mark is inert — the fold is the log's to offer", () => {
    const r = mount(<Turn turn={assistant("a:1")} collapsed />);
    expect((marks(r)[0]!.props as { disabled?: boolean }).disabled).toBe(true);
    expect(folded(r)).toEqual([]);
  });
});

describe("A ONE-LINE REPLY NEVER FOLDS (Akshil 2026-09-15)", () => {
  /** The shape a one-line answer actually arrives in: history gives a text-only
   *  turn no `segments` key at all (`protocol/history.ts`). */
  const oneLiner = (key: string, body = "Done.") =>
    ({ role: "assistant", key, text: body }) as TurnRow;

  test("what counts as one line", () => {
    expect(isOneLiner(oneLiner("a:1"))).toBe(true);
    // A single `text` SEGMENT is the same reply, differently delivered.
    expect(isOneLiner(assistant("a:1", { segments: [text("Yes — it passes.")] }))).toBe(true);
    // A trailing newline off markdown is not a second line.
    expect(isOneLiner(oneLiner("a:1", "Done.\n"))).toBe(true);
    // Two lines, too many characters, or anything else in the turn — all of
    // which have something under the fold.
    expect(isOneLiner(oneLiner("a:1", "Done.\nAnd here is why."))).toBe(false);
    expect(isOneLiner(oneLiner("a:1", "x".repeat(81)))).toBe(false);
    expect(isOneLiner(assistant("a:1", { segments: [text("Done."), tool("t1", "Read")] }))).toBe(
      false,
    );
    expect(
      isOneLiner(assistant("a:1", { text: "", segments: [{ kind: "thinking", text: "hm" } as Segment] })),
    ).toBe(false);
    // A turn with no words at all is not a one-line reply — its fold shows
    // machinery, and that is still worth folding away.
    expect(isOneLiner(oneLiner("a:1", ""))).toBe(false);
    // Nothing else in the log is one.
    expect(isOneLiner({ role: "user", key: "u:1", text: "hi" } as TurnRow)).toBe(false);
  });

  test("the mark is a plain seat, not a disclosure", () => {
    const r = mount(<Turn turn={oneLiner("a:1")} onToggleCollapse={() => {}} />);
    const mark = marks(r)[0]!.props as Record<string, unknown>;
    expect(mark.disabled).toBe(true);
    expect("aria-expanded" in mark).toBe(false);
    expect("aria-controls" in mark).toBe(false);
    expect("data-hint" in mark).toBe(false);
    // Never greyed, because it is never folded — `collapsed` is ignored.
    const shut = mount(<Turn turn={oneLiner("a:1")} collapsed onToggleCollapse={() => {}} />);
    expect((byClass(shut, "turn")[0]!.props as { className: string }).className).not.toContain(
      "is-folded",
    );
    expect(folded(shut)).toEqual([]);
  });

  test("IT STAYS OPEN WHEN THE NEXT RESPONSE STREAMS IN", () => {
    const r = log([oneLiner("a:1"), assistant("a:2")]);
    // `a:2` is the newest and open; `a:1` is older and would ordinarily fold.
    expect(folded(r)).toEqual([]);
    act(() => {
      r.update(
        <Transcript
          state={state({
            turns: [oneLiner("a:1"), assistant("a:2"), assistant("a:3", { streaming: true })],
          })}
          actions={actions}
        />,
      );
    });
    // The two-paragraph reply folds. The one-liner does not.
    expect(folded(r)).toEqual(["reply a:2"]);
  });
});

describe("the line a folded reply shows", () => {
  test("the FIRST LINE of the first text segment", () => {
    const turn = assistant("a:1", {
      text: "ignored",
      segments: [tool("t1", "Read"), text("## Heading\nand the rest"), text("later")],
    });
    expect(collapsedLine(turn)).toEqual({ text: "## Heading", muted: false });
    const r = mount(<Turn turn={turn} collapsed onToggleCollapse={() => {}} />);
    expect(folded(r)).toEqual(["## Heading"]);
  });

  test("a turn that was ALL tool calls shows the first call's summary, muted", () => {
    const turn = assistant("a:1", { text: "", segments: [tool("t1", "Read"), tool("t2", "Bash")] });
    const line = collapsedLine(turn)!;
    expect(line.muted).toBe(true);
    expect(line.text.length).toBeGreaterThan(0);
    const r = mount(<Turn turn={turn} collapsed onToggleCollapse={() => {}} />);
    expect((byClass(r, "turn-collapsed")[0]!.props as { className: string }).className).toContain(
      "is-muted",
    );
    expect(folded(r)).toEqual([line.text]);
  });

  test("a segment-less turn falls back to its flat body", () => {
    expect(collapsedLine(assistant("a:1", { text: "flat reply\nmore" }))).toEqual({
      text: "flat reply",
      muted: false,
    });
  });

  test("IT IS NEVER NULL — a folded row always has a word on it (Akshil 2026-09-15)", () => {
    // A folded turn draws this line and nothing else, so a null answer is a row
    // with a mark, a fold, and no words at all: a control the reader can only
    // work by pressing it to find out. The last resort is the KIND of the first
    // thing in the turn, muted — it says what the row IS, not what it said.
    const only = (segments: Segment[]) =>
      collapsedLine(assistant("a:1", { text: "", segments }));
    expect(only([{ kind: "thinking", text: "" } as Segment])).toEqual({
      text: "Thinking",
      muted: true,
    });
    expect(only([{ kind: "notice", text: "" } as Segment])).toEqual({
      text: "Notice",
      muted: true,
    });
    // A turn the reader ended reads as what happened to it, not as the notice
    // that recorded it.
    expect(only([{ kind: "notice", text: INTERRUPT_MARK } as Segment])).toEqual({
      text: "Interrupted",
      muted: true,
    });
    // And a turn with nothing in it whatsoever still names itself.
    const empty = collapsedLine(assistant("a:1", { text: "" }));
    expect(empty.muted).toBe(true);
    expect(empty.text.length).toBeGreaterThan(0);
    const r = mount(<Turn turn={assistant("a:1", { text: "" })} collapsed onToggleCollapse={() => {}} />);
    expect(folded(r)).toEqual([empty.text]);
  });
});

test("the folded line is the first line RENDERED, not its markdown source", () => {
  // "**Done.** two files" folds to bold "Done." — never a row of asterisks.
  const r = log([
    assistant("a:1", { text: "**Done.** two files\nmore below" }),
    assistant("a:2"),
  ]);
  const md = byClass(r, "turn-collapsed-md")[0]!;
  const html = (md.props as { dangerouslySetInnerHTML: { __html: string } })
    .dangerouslySetInnerHTML.__html;
  // The test shim's `renderMd` wraps its input in <pre> rather than parsing
  // it, so the check here is the wiring — the first line, and only the first
  // line, goes through the renderer the open body uses. Bold-not-asterisks is
  // that renderer's job, exercised by MarkdownView's own tests.
  expect(html).toContain("**Done.** two files");
  expect(html).not.toContain("more below");
  expect(byClass(r, "turn-collapsed")[0]!.children).toHaveLength(1);
});

test("the folded line skips scaffolding and drops link targets (bugbot on 69cdcb9)", () => {
  // A reply that opens on a fence folds to the first WORDS, not to an empty
  // <pre>; a thematic break likewise.
  expect(firstLine("```python\nprint(1)\n```\nDone.")).toBe("print(1)");
  expect(firstLine("---\n\nSummary here")).toBe("Summary here");
  // Links keep their words and lose the <a>: the row is one pointer target.
  expect(firstLine("See [the docs](https://x.y/z) now")).toBe("See the docs now");
  expect(firstLine("```c++\nint x;")).toBe("int x;");
  expect(firstLine("![alt text](img.png) after")).toBe("alt text after");
  // Bold survives — it is words, not scaffolding.
  expect(firstLine("**Done.** two files")).toBe("**Done.** two files");
});

test("the folded line carries no <a> even for GFM autolinks (bugbot on d4233e8)", async () => {
  const { renderMdInert } = await import("../protocol/markdown");
  const html = renderMdInert("see https://x.com and www.example.com or bob@example.com **now**");
  expect(html).not.toContain("<a");
  expect(html).not.toContain("<img");
  expect(html).toContain("https://x.com");
});
