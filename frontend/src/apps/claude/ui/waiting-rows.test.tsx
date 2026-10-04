// THE CHAT'S HALF OF THE PROJECT QUEUE (prefs `queue.enabled`).
//
// The shape it settled into on 2026-09-12, and what each half replaced:
//
//   * A waiting message is the reader's OWN BUBBLE at its place in the
//     transcript, transparent with a dashed edge, with one muted line under it —
//     not a chip with a ring, a status word, a place, a Skip and a Cancel parked
//     under the log.
//   * Those rows are drawn FROM THE SERVER, so a reload paints the same picture.
//     The chip was client state; a refresh, a navigation or the session adoption
//     this feature itself causes dropped every card while the entries sat in the
//     line.
//   * One summary card over the composer carries the count, what is in front,
//     and Force start — which is offered on EVERY waiting card, including one
//     standing 1st, because it does not reorder the line, it leaves it. (Run
//     next held that seat until 2026-09-21 and was hidden at position 1: it
//     could not get in front of the run holding the folder.)
//   * A follow-up into this chat's OWN running turn draws nothing at all: no row,
//     no card, and under the flag not even the composer's old footnote.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { WaitingCard, WaitingRow } from "./Waiting";
import type { WaitingRowData } from "../sched/waiting";

const HERE = new URL(".", import.meta.url).pathname;
const CHAT = readFileSync(join(HERE, "../ClaudeChat.tsx"), "utf8");
const CONTROLLER = readFileSync(join(HERE, "../protocol/run-controller.ts"), "utf8");
const USE_SCHEDULE = readFileSync(join(HERE, "../sched/useSchedule.ts"), "utf8");
const WATCHER = readFileSync(join(HERE, "../sched/scheduled.ts"), "utf8");
const WAITING = readFileSync(join(HERE, "Waiting.tsx"), "utf8");
const COMPOSER = readFileSync(join(HERE, "Composer.tsx"), "utf8");
const PERM = readFileSync(join(HERE, "PermCard.tsx"), "utf8");
const ASK_CARD = readFileSync(join(HERE, "QuestionCard.tsx"), "utf8");
const PLAN_CARD = readFileSync(join(HERE, "PlanCard.tsx"), "utf8");
const SCHED_CSS = readFileSync(join(HERE, "../styles/sched.css"), "utf8");

/** Everything the render put on screen, as one string. */
function textOf(node: ReactTestRenderer): string {
  return JSON.stringify(node.toJSON());
}

function render(el: React.ReactElement): ReactTestRenderer {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(el);
  });
  return r;
}

const row = (extra: Partial<WaitingRowData> = {}): WaitingRowData => ({
  entryId: "e1",
  text: "count the rows",
  due: "2026-09-12T09:59:00Z",
  word: "queued",
  optimistic: false,
  repeat: false,
  stopId: "",
  ...extra,
});

describe("a waiting message is a bubble, not a card", () => {
  it("draws the reader's words in the transcript's own user bubble", () => {
    const r = render(<WaitingRow row={row()} facts={{}} onDelete={() => {}} />);
    const bubbles = r.root.findAll(
      (n) => typeof n.type === "string" && String(n.props.className).includes("bubble"),
    );
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0]!.props.children).toBe("count the rows");
    // The transcript's OWN classes, not a second skin: `.turn.user .bubble`
    // dresses it and this block sits in the log's own 720px column.
    expect(
      r.root.findAll(
        (n) => typeof n.type === "string" && n.props.className === "turn user c-waiting-turn",
      ),
    ).toHaveLength(1);
    act(() => r.unmount());
  });

  it("is DASHED AND TRANSPARENT, and is not faded", () => {
    // A dashed edge is a shape that says "not yet". Dimming says "less
    // important", and these are the reader's own words, which are not (Akshil,
    // 2026-09-12). So the bubble keeps full-strength ink and loses only its fill;
    // the LINE under it is the muted one.
    const css = SCHED_CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    const rule = css.slice(
      css.indexOf(".c-waiting .bubble.c-waiting-bubble {"),
      css.indexOf("}", css.indexOf(".c-waiting .bubble.c-waiting-bubble {")),
    );
    expect(rule).toContain("background: none");
    expect(rule).toContain("border: 1px dashed");
    expect(rule).toContain("color: var(--c-fg)");
    expect(rule).not.toContain("opacity");
    const line = css.slice(
      css.indexOf(".c-waiting-line {"),
      css.indexOf("}", css.indexOf(".c-waiting-line {")),
    );
    expect(line).toContain("color: var(--c-dim)");
    // No width and no breakpoint anywhere in the block: this pane is 340px in a
    // sidebar and 900px in a canvas.
    expect(css).not.toMatch(/@media[^{]*\{[^}]*c-waiting/);
    expect(line).not.toMatch(/(?<!-)\bwidth:\s*\d/);
  });

  it("draws no bubble for a WORDLESS send, but still says it is waiting", () => {
    // Pictures or notes alone have no typed line, and their real bubble is
    // markers only the controller can compose.
    const r = render(<WaitingRow row={row({ text: "" })} facts={{}} onDelete={() => {}} />);
    expect(
      r.root.findAll((n) => typeof n.type === "string" && String(n.props.className) === "bubble"),
    ).toHaveLength(0);
    expect(textOf(r)).toContain("queued");
    act(() => r.unmount());
  });
});

describe("the one line under it", () => {
  it("reads `queued · after TASK-038 · delete`, with the id as a link", () => {
    const r = render(
      <WaitingRow
        row={row()}
        facts={{
          queue_ahead: "TASK-038",
          queue_ahead_title: "Pull the news",
          queue_ahead_session: "sess-1",
          queue_ahead_target: "/w/app",
        }}
        onDelete={() => {}}
      />,
    );
    const out = textOf(r);
    expect(out).toContain("queued");
    expect(out).toContain("after ");
    expect(out).toContain("TASK-038");
    expect(out).toContain("delete");
    // THE ID IS THE PRESS, into the conversation that is in the way — the one
    // question a person has about the thing in front of them.
    const link = r.root.findAllByType("a")[0];
    expect(link.props.href).toBe("/explorer/view/w/app?_side=claude&session_id=sess-1");
    // …and the holder's title is on the pointer, where it stopped being ink: a
    // quoted title inside the caption was the first thing to push the id off the
    // end of a narrow pane.
    expect(link.props.title).toBe("Pull the news");
    // …and it is ONLY on the pointer: the id is the link's whole text.
    expect(link.props.children).toBe("TASK-038");
    expect(out).not.toContain('children":["Pull the news');
    act(() => r.unmount());
  });

  it("says NOTHING about being behind anything when the folder is free", () => {
    // "behind a run in this folder" was the shipped empty case and it is a
    // sentence with a hole in it. A second message waiting behind the reader's
    // own first one is the ordinary shape of this, and the folder is genuinely
    // free.
    const r = render(<WaitingRow row={row()} facts={{ queue_ahead: "" }} onDelete={() => {}} />);
    const out = textOf(r);
    expect(out).toContain("queued");
    expect(out).not.toContain("behind");
    expect(out).toContain("delete");
    act(() => r.unmount());
  });

  it("says `scheduled · <when>` before the due time instead", () => {
    // A calendar message aimed at this chat, drawn the same way as everything
    // else waiting — and the interesting fact before its due time is WHEN, not
    // what is in the way, because nothing is.
    const when = new Date(2026, 8, 12, 5, 12);
    const r = render(
      <WaitingRow
        row={row({ word: "scheduled", due: when.toISOString() })}
        facts={{ queue_ahead: "TASK-038" }}
        onDelete={() => {}}
      />,
    );
    const out = textOf(r);
    expect(out).toContain("scheduled");
    expect(out).toContain("Sat 12 Sep, 05:12");
    expect(out).not.toContain("behind");
    act(() => r.unmount());
  });

  it("deletes on one press, with no arming, and goes dead while it is in flight", () => {
    // One press because this drops ONE message, whose words are in the bubble
    // directly above the word pressed — a repeat's stop spends every future run
    // and is the thing that needs a confirm.
    let deleted = 0;
    const r = render(
      <WaitingRow row={row()} facts={{}} onDelete={() => { deleted += 1; }} />,
    );
    const buttons = r.root.findAllByType("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0].props.children).toBe("delete");
    act(() => buttons[0].props.onClick());
    expect(deleted).toBe(1);
    act(() => r.unmount());
    // Dead rather than GONE for the round trip: a control that disappears on the
    // press that worked is how a reader ends up unsure anything happened.
    const busy = render(<WaitingRow row={row()} facts={{}} deleting onDelete={() => {}} />);
    expect(busy.root.findAllByType("button")[0].props.disabled).toBe(true);
    act(() => busy.unmount());
  });

  it("sits UNDER ITS OWN BUBBLE, on the right, and wraps that way too", () => {
    // A user turn is right-aligned in this column. The line was flush LEFT, two
    // hundred pixels from the message it is about, so it read as a caption for
    // the assistant turn above it as readily as for the reader's own (Akshil,
    // 2026-09-12). `justify-content: flex-end` puts the sentence under its
    // bubble; `text-align: end` carries every WRAPPED line the same way, so a
    // narrow pane stacks the clauses against the right edge instead of fanning
    // them out from the left.
    const css = SCHED_CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    const line = css.slice(css.indexOf(".c-waiting-line {"), css.indexOf("}", css.indexOf(".c-waiting-line {")));
    expect(line).toContain("justify-content: flex-end");
    expect(line).toContain("text-align: end");
    // …and NOT `justify-self`, which did nothing here: this element is a flex
    // ITEM in a column, and `justify-self` is ignored on one (🟡 review
    // 2026-09-12). Two properties that look like a pair and are not.
    expect(line).not.toContain("justify-self");
    // STILL WRAPS AND STILL MEASURES NOTHING: the sentence's length is the
    // data's, and no width or breakpoint decides it.
    expect(line).toContain("flex-wrap: wrap");
    expect(line).not.toMatch(/\bwidth:/);
    expect(css).not.toContain("@media (max-width");
    // …AND THE ELLIPSIS IS NEVER ON `delete`. It is the one token here a press
    // lands on, so it neither shrinks nor breaks: a verb cut to `dele…` is a
    // control a reader cannot be sure of. What may give way is the prose.
    const del = css.slice(css.indexOf(".c-waiting-del {"), css.indexOf("}", css.indexOf(".c-waiting-del {")));
    expect(del).toContain("flex: 0 0 auto");
    expect(del).toContain("white-space: nowrap");
    expect(del).not.toContain("text-overflow: ellipsis");
  });
});

describe("the card over the composer", () => {
  it("counts the messages and names what is in front", () => {
    const r = render(
      <WaitingCard
        count={2}
        facts={{ queue_position: 2, queue_ahead: "TASK-038" }}
        onForceStart={() => {}}
      />,
    );
    const out = textOf(r);
    expect(out).toContain("2 messages queued");
    expect(out).toContain("TASK-038");
    act(() => r.unmount());
  });

  it("offers Force start at the head of the line AND behind another waiting task", () => {
    // THE WHOLE DIFFERENCE FROM RUN NEXT (Akshil, 2026-09-21). Position 1 is the
    // arrangement the old verb was hidden in — it could not get in front of the
    // run holding the folder — and it is the one a reader most wants this press
    // for: standing 1st still means waiting on a turn that may have an hour left.
    for (const at of [1, 2]) {
      const r = render(
        <WaitingCard
          count={2}
          facts={{ queue_position: at, queue_ahead: "TASK-038" }}
          onForceStart={() => {}}
        />,
      );
      const buttons = r.root.findAllByType("button");
      expect(buttons).toHaveLength(1);
      expect(textOf(r)).toContain("Force start");
      // …AND NEVER THE DEAD VERB, on either surface it used to be said on.
      expect(textOf(r)).not.toContain("Run next");
      expect(textOf(r)).not.toContain("⤒");
      // The sentence the card exists for is untouched by the button.
      expect(textOf(r)).toContain("2 messages queued");
      expect(textOf(r)).toContain("TASK-038");
      act(() => r.unmount());
    }
  });

  it("calls back once, and goes dead while the press is in flight", () => {
    let pressed = 0;
    const r = render(
      <WaitingCard
        count={1}
        facts={{ queue_position: 1, queue_ahead: "TASK-038" }}
        onForceStart={() => { pressed += 1; }}
      />,
    );
    act(() => r.root.findAllByType("button")[0].props.onClick());
    expect(pressed).toBe(1);
    act(() => r.unmount());
  });

  it("says '1 message queued' for one, and draws nothing for none", () => {
    const one = render(<WaitingCard count={1} facts={{}} onForceStart={() => {}} />);
    expect(textOf(one)).toContain("1 message queued");
    expect(textOf(one)).not.toContain("messages");
    act(() => one.unmount());
    // "0 messages queued" is a card about nothing sitting on top of the box.
    const none = render(<WaitingCard count={0} facts={{}} onForceStart={() => {}} />);
    expect(none.toJSON()).toBe(null);
    act(() => none.unmount());
  });

  it("names what is in front, and offers no press where the server placed nothing", () => {
    // POSITION 0 IS THE ONE ARM WITH NO BUTTON (`canForceStart`): the server
    // placed this row nowhere — an older server, or a row it could not place —
    // and a press whose subject the server could not name can only 400.
    const free = render(<WaitingCard count={2} facts={{}} onForceStart={() => {}} />);
    expect(free.root.findAllByType("button")).toHaveLength(0);
    expect(textOf(free)).toContain("next in this folder");
    act(() => free.unmount());

    // AT THE HEAD OF THE LINE the only thing in front is the run HOLDING the
    // folder. The sentence still says so, because it is true (Akshil,
    // 2026-09-12) — and the press is offered, because this verb starts beside
    // that run rather than trying to get in front of it.
    const head = render(
      <WaitingCard
        count={1}
        facts={{ queue_position: 1, queue_ahead: "TASK-056" }}
        onForceStart={() => {}}
      />,
    );
    expect(head.root.findAllByType("button")).toHaveLength(1);
    expect(textOf(head)).toContain("1 message queued");
    expect(textOf(head)).toContain("after ");
    expect(textOf(head)).toContain("TASK-056");
    expect(textOf(head)).not.toContain("next in this folder");
    act(() => head.unmount());
  });

  it("reads 'next in this folder' once the spot is claimed", () => {
    // A skip sets `queue_priority`: TASK-038 is STILL holding the folder, and
    // nothing WAITING is in front of this any more, which is the one fact that
    // replaces the "after …" half of the sentence.
    const after = render(
      <WaitingCard
        count={2}
        facts={{ queue_position: 2, queue_ahead: "TASK-038", queue_priority: true }}
        onForceStart={() => {}}
      />,
    );
    const out = textOf(after);
    expect(out).toContain("2 messages queued");
    expect(out).toContain("next in this folder");
    expect(out).not.toContain("Run next");
    act(() => after.unmount());
  });

  it("is drawn and DEAD while a press of it is in flight", () => {
    // Dead rather than dropped: the card would otherwise lose its control on
    // the very press that worked, and the reader would have nothing to read the
    // outcome against.
    const busy = render(
      <WaitingCard
        count={1}
        facts={{ queue_position: 3, queue_ahead: "TASK-038" }}
        busy
        onForceStart={() => {}}
      />,
    );
    const button = busy.root.findAllByType("button")[0];
    expect(button.props.disabled).toBe(true);
    act(() => busy.unmount());
  });

  it("re-reads the facts it is handed and derives nothing of its own", () => {
    // The sentence is ROW-DRIVEN, and this is the case that proves it: a task
    // standing 1st is behind the run holding its folder, and the moment somebody
    // else's skip puts it 2nd the very same card names the new holder — because
    // it derives nothing (Akshil, 2026-09-12).
    const r = render(
      <WaitingCard
        count={1}
        facts={{ queue_position: 1, queue_ahead: "TASK-056" }}
        onForceStart={() => {}}
      />,
    );
    expect(textOf(r)).toContain("TASK-056");
    act(() => {
      r.update(
        <WaitingCard
          count={1}
          facts={{ queue_position: 2, queue_ahead: "TASK-041" }}
          onForceStart={() => {}}
        />,
      );
    });
    // The button is there at both positions — it is the SENTENCE this case is
    // about, and the press does not depend on who is in front.
    expect(r.root.findAllByType("button")).toHaveLength(1);
    expect(textOf(r)).toContain("after ");
    expect(textOf(r)).toContain("TASK-041");
    act(() => r.unmount());
  });

  it("reads `N messages queued · next in this folder` once the claim has landed", () => {
    // What a reader sees once the server has moved this to the head of the line:
    // the claim on the spot (`queue_priority`) is the ONLY thing that replaces
    // "after …".
    const r = render(
      <WaitingCard
        count={2}
        facts={{ queue_position: 2, queue_ahead: "TASK-041" }}
        onForceStart={() => {}}
      />,
    );
    expect(textOf(r)).toContain("after ");
    act(() => {
      r.update(
        <WaitingCard
          count={2}
          facts={{ queue_position: 1, queue_ahead: "TASK-041", queue_priority: true }}
          onForceStart={() => {}}
        />,
      );
    });
    expect(textOf(r)).toContain("2 messages queued");
    expect(textOf(r)).toContain("next in this folder");
    expect(textOf(r)).not.toContain("after ");
    // A CLAIMED SPOT IS STILL A WAITING ONE: `queue_priority` silences the
    // "after …" half and takes no button with it, because "next" is not "now".
    expect(r.root.findAllByType("button")).toHaveLength(1);
    act(() => r.unmount());
  });
});

describe("the rows come from the server, which is what a reload reads", () => {
  it("draws from the poll's own lists and never from a client-only store", () => {
    // The chip this replaces was client state: a refresh, a navigation, or the
    // session adoption this very feature causes dropped every card while the
    // entries sat in the line — messages safe on the server and invisible on
    // screen, which is the worse of the two possible failures.
    expect(CHAT).toContain("const waiting = useMemo(");
    expect(CHAT).toContain("waitingRows(serverWaiting, liveSeeds, droppedEntries)");
    // ONE ADDRESS, not two. It was two — the session-filtered pending list with
    // a session, the leader's followers without one — and adoption flips exactly
    // the fact they were swapped on, so both missed the followers in the instant
    // it did (Bugbot PR #1124). `waitingFor` asks the whole listing instead.
    expect(CHAT).toContain("const serverWaiting = sched.waitingHere;");
    expect(CHAT).not.toContain("schedFollowing(");
    expect(USE_SCHEDULE).toContain("waitingFor(allRows, sessionId, leaderId)");
    // …and the list it publishes is the rows that can ANSWER one of the two
    // questions it exists for: still in the line, or holding the link to a
    // session a chat is about to adopt (🔴 review 2026-09-12).
    expect(WATCHER).toContain(
      "deps.onAllRows?.(entries.filter((e) => schedIsWaiting(e) || !!(e && e.claude_session_id)));",
    );
  });

  it("is ONE answer about what is in front, and the server's when it has one", () => {
    // A folder is held by one task, so three messages in one line are behind the
    // same thing. The authority is this conversation's `/api/tasks` row, which
    // survives a reload; the admission's answer is the fallback for the first
    // paint and for a chat that has no row at all.
    expect(CHAT).toContain("waitingFacts(sched.rec, admitAhead, claimedNext)");
    expect(WATCHER).toContain("queue_ahead_session?: string;");
    expect(USE_SCHEDULE).toContain("if (!hasCard || !nextId) return;");
  });

  it("keeps the optimistic row for the poll's own latency and no longer", () => {
    // The window between the admission creating the entry and the next 15 s tick
    // listing it: without it, a message the reader just pressed Enter on is
    // nowhere on screen for up to a poll interval. Same entry id as the server's
    // row, so the swap is never two rows (sched/waiting has the merge's tests).
    const send = CHAT.slice(CHAT.indexOf("const dispatchSend = useCallback("));
    const queued = send.indexOf("if (verdict && verdict.run === false) {");
    expect(queued).toBeGreaterThan(0);
    expect(send.slice(queued, queued + 5000)).toContain("setWaitingSeeds((cur) => [");
    expect(CHAT).toContain("useLiveSeeds(waitingSeeds, sched.pendingIds)");
    // NOT A PLAIN FILTER against the poll: that set is a photograph older than
    // the send it is being asked about.
    expect(CHAT).not.toContain("waitingSeeds.filter((q) => sched.pendingIds?.has(q.entryId))");
  });

  it("takes a deleted row down at once, and does not let the poll put it back", () => {
    const del = CHAT.slice(
      CHAT.indexOf("const deleteWaiting = useCallback("),
      CHAT.indexOf("const adoptSession ="),
    );
    expect(del).toContain("await cancelScheduledMessage(stopId || entryId);");
    // Remembered BEFORE the seed is dropped, so no paint sits between the two —
    // and the memory keeps filtering the SERVER's list, which is up to a lap
    // older than the press.
    expect(del.indexOf("setDroppedEntries((cur) => new Set(cur).add(entryId));")).toBeLessThan(
      del.indexOf("setWaitingSeeds((cur) => cur.filter((q) => q.entryId !== entryId));"),
    );
    expect(del).toContain("schedRefresh();");
    expect(CHAT).toContain("setDroppedEntries((cur) => pruneDropped(cur, sched.pendingIds));");
    // A refusal is said out loud rather than swallowed: a press with a visible
    // control behind it that answers with silence is a control that did nothing.
    expect(del).toContain('"This message was not deleted: " + t.message');
  });

  it("keeps the rows through the adoption that replaces the transcript", () => {
    // A new chat's first message queues, a second queues behind it, the leader
    // runs, and the chat adopts the session its run opened — `openSession`
    // REPLACES the transcript with the JSONL, which holds neither follower. The
    // rows survive because they are the SERVER's: those entries are still
    // pending, so the very next poll lists them again.
    const adopt = CHAT.slice(
      CHAT.indexOf("const adoptSession = leaderSession("),
      CHAT.indexOf("}, [adoptSession, controller, cardPolicy, params, file]);"),
    );
    expect(adopt).not.toContain("setWaitingSeeds");
    expect(adopt).not.toContain("setDroppedEntries");
  });
});

// FORCE START, FROM THE CARD — the wiring, which is what this file can see of
// it: which id the press names, that it paints no claim, and the body it posts.
describe("Force start, from the card", () => {
  it("names the OLDEST waiting entry, and the task key only with no rows", () => {
    // THE ENTRY AND NOT THE TASK, because an entry id is minted once and never
    // rekeyed: a queued chat's key is `pending:<leader>` until the leader's run
    // mints a session, and the whole row then rekeys onto it. The server
    // resolves the oldest due message either way, so the name only has to be
    // one it can still find — and the key is the honest fallback for a chat
    // drawing no rows yet.
    const run = CHAT.slice(
      CHAT.indexOf("const forceStartNow = useCallback("),
      CHAT.indexOf("const deleteWaiting = useCallback("),
    );
    expect(run).toContain('waiting.find((r) => r.word === "queued")?.entryId');
    expect(run).toContain('const key = sched.rec?.key || "";');
    expect(run).toContain("await forceStart(first ? { entry_id: first } : { key });");
    // NO CLAIM IS PAINTED, unlike the skip this replaced (which had to: it
    // produced a POSITION no listing would report for a while). This produces a
    // RUN, and the schedule poll is what draws one — so the press asks for a lap
    // and lets the row say "starting" on its own.
    expect(run).toContain("schedRefresh();");
    expect(run).not.toContain("queue_priority: true");
    expect(run).not.toContain("setNextClaim(");
    // …and a refusal is said out loud, in the words the queue uses for it.
    expect(run).toContain('"The queue did not take that: " + t.message');
    // AND NEVER IN THE DEAD VERB'S NAME (Akshil, 2026-09-21).
    expect(CHAT).not.toContain("Run next did not go through");
    expect(CHAT).not.toContain("skipQueue");
  });

  it("posts exactly that body, and only that", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const realFetch = globalThis.fetch;
    (globalThis as { fetch: unknown }).fetch = async (url: string, init: RequestInit) => {
      bodies.push({ url, ...(JSON.parse(String(init.body)) as Record<string, unknown>) });
      return {
        ok: true,
        json: async () => ({ ok: true, started: true, run_id: "r1", session_id: "s1" }),
      } as unknown as Response;
    };
    try {
      const { forceStart } = await import("@platform/lib/api");
      await forceStart({ entry_id: "e7" });
      expect(bodies[0].url).toBe("/api/tasks/queue/force");
      expect(bodies[0].entry_id).toBe("e7");
      expect(bodies[0]).not.toHaveProperty("key");
      await forceStart({ key: "TASK-041" });
      expect(bodies[1].key).toBe("TASK-041");
      expect(bodies[1]).not.toHaveProperty("entry_id");
    } finally {
      (globalThis as { fetch: unknown }).fetch = realFetch;
    }
  });
});

describe("admission, in the send window", () => {
  it("is asked BEFORE anything is spent, and before start/send", () => {
    const send = CHAT.slice(CHAT.indexOf("const dispatchSend = useCallback("));
    const ask = send.indexOf("await admitQueueSend({");
    const capture = send.indexOf("await beginSend(opts)");
    expect(ask).toBeGreaterThan(0);
    // `beginSend` PHOTOGRAPHS the pane, empties the attachment tray and stamps
    // the round of notes as sent. A message that turns out to be queued took
    // none of it.
    expect(ask).toBeLessThan(capture);
    expect(send.indexOf("if (queueEnabled()) {")).toBeLessThan(capture);
  });

  it("hands the words to the row, never lost and never doubled", () => {
    const send = CHAT.slice(CHAT.indexOf("const dispatchSend = useCallback("));
    const queued = send.indexOf("if (verdict && verdict.run === false) {");
    const road = send.slice(queued, queued + 5000);
    // The TYPED line goes on the seed, because the stored entry holds the
    // COMPOSED message and swapping one for the other on the next poll would be
    // the bubble rewriting itself under the reader.
    expect(road).toContain("text, due:");
    // The optimistic row does NOT stay: `taken` is left false, so the `finally`
    // drops it and the waiting row is the one copy on screen.
    expect(road).not.toContain("taken = true;");
    expect(send).toContain(
      "if (!taken && optimisticKey) controller.dropOptimisticUser(optimisticKey);",
    );
  });

  it("sends NOTHING when the ask does not come back with a clean answer", () => {
    // Falling through turns every refusal into the second run in a busy folder
    // the whole feature exists to prevent.
    const send = CHAT.slice(CHAT.indexOf("const dispatchSend = useCallback("));
    const admit = send.indexOf("await admitQueueSend({");
    const catchBlock = send.indexOf("} catch (err) {", admit);
    const spawn = send.indexOf("await beginSend(opts)");
    expect(catchBlock).toBeGreaterThan(0);
    expect(catchBlock).toBeLessThan(spawn);
    expect(send.slice(catchBlock, spawn)).toContain("refuseQueuedSend(text, err);");
    expect(send).toContain("if (run !== true && run !== false) {");
    expect(send).toContain('refuseQueuedSend(text, new Error("the queue gave no answer."));');
  });

  it("puts a refused send's words back in the box and its reason on the card", () => {
    const at = CHAT.indexOf("const refuseQueuedSend = useCallback(");
    const refuse = CHAT.slice(at, CHAT.indexOf("[controller, strand],", at));
    expect(refuse).toContain("strand(text);");
    expect(refuse).toContain("controller.reportTrouble({");
    expect(CONTROLLER).toContain("reportTrouble,");
    // The tray is NOT emptied on this road: `beginSend` was never reached, so
    // the pictures are still attached for whatever the reader does next.
    expect(refuse).not.toContain("spendTrayForQueue");
  });

  it("puts the pictures ON the entry, because the send fires without the tray", () => {
    const send = CHAT.slice(CHAT.indexOf("const dispatchSend = useCallback("));
    const carry = send.indexOf("await carryForQueue(");
    const admit = send.indexOf("await admitQueueSend({");
    expect(carry).toBeGreaterThan(0);
    expect(carry).toBeLessThan(admit);
    expect(send).toContain("images: carried.map((a) => a.path), attachments: carried");
    expect(CHAT).toContain("return copyToTaskShots(tray).catch(() => []);");
    const queued = send.indexOf("if (verdict && verdict.run === false) {");
    expect(send.slice(queued, queued + 5000)).toContain("spendTrayForQueue();");
  });

  it("names the draft it is spending, so the queued task keeps the reader's TASK number", () => {
    // A session-less composer drafts (and is NUMBERED) under `new:<file>`. A send
    // into a FREE folder spends that key through the run it starts — the start
    // request's `draft_key`, read back off `meta.json` — and a send that QUEUES
    // starts no run at all, so it has to say so here or the entry mints a SECOND
    // number and the reader watches TASK-057 become TASK-058 on the send (review,
    // PR #1124).
    const send = CHAT.slice(CHAT.indexOf("const dispatchSend = useCallback("));
    // …AND THE KEY IT NAMES IS THE ONE THE LANDING COMPOSER WAS HOLDING
    // (2026-09-17): that box is open on a `draft:<id>` picked off the listing,
    // so the send spends THAT record. `chatDraftKey` is the fallback for a
    // landing that never held one.
    expect(send).toContain(
      '...(sid ? {} : { draft_key: heldRef.current?.key ?? chatDraftKey(null, file || "") }),',
    );
    // The same function the composer keys on, so the two spellings cannot drift.
    expect(CHAT).toMatch(
      /import \{[^}]*\bchatDraftKey\b[^}]*\} from "@platform\/lib\/drafts";/,
    );
  });

  it("joins a follow-up to the message already in the line, not to a new task", () => {
    const send = CHAT.slice(CHAT.indexOf("const dispatchSend = useCallback("));
    expect(send).toContain('const sid = live.sessionId || params.get("session_id") || "";');
    expect(send).toContain("const follow = leader.followOf(sid);");
    expect(send).toContain("...(follow ? { follow_of: follow } : {}),");
    expect(send).toContain("session_id: sid,");
    const queued = send.indexOf("if (verdict && verdict.run === false) {");
    expect(send.slice(queued, queued + 5000)).toContain("leader.remember(sid, entryId);");
  });

  it("names the live run beside the session it may not have yet", () => {
    // A folder's holder is a RUN. Asking "is that me?" by session id alone left
    // the second line typed into a brand-new chat queueing behind its own first
    // turn — the reader waiting for themselves.
    const send = CHAT.slice(CHAT.indexOf("const dispatchSend = useCallback("));
    expect(send).toContain('const rid = live.runId || live.lastRunId || "";');
    expect(send).toContain("...(rid ? { run_id: rid } : {}),");
    expect(send).toContain("const live = controller.getState();");
    expect(
      send.indexOf('const sid = live.sessionId || params.get("session_id") || "";'),
    ).toBeLessThan(send.indexOf('const rid = live.runId || live.lastRunId || "";'));
  });

  it("is never anonymous after a turn has run — the id outlives the run", () => {
    const api = readFileSync(join(HERE, "../protocol/controller-api.ts"), "utf8");
    expect(api).toContain("lastRunId: string | null;");
    expect(CONTROLLER).toContain(
      "? { status, runId: activeRun, ...(activeRun ? { lastRunId: activeRun } : {}) }",
    );
    expect(CONTROLLER).toContain("{ status, runId: null }");
    expect(CONTROLLER).toContain("lastRunId: null,");
    // …AND THE SEND PATH NAMES IT TOO, before it ever polls (Akshil,
    // 2026-09-12): a stop landing between `start` answering and the first poll
    // frame otherwise left the id unwritten and the next admit anonymous.
    expect(CONTROLLER).toContain("emit({ lastRunId: runId });");
    expect(CONTROLLER.indexOf("emit({ lastRunId: runId });")).toBeLessThan(
      CONTROLLER.indexOf("await pollLoop(runId, gen, { ownTurn: true, prompt: spoken });"),
    );
  });

  it("is on the wire, and absent when nothing is running", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const realFetch = globalThis.fetch;
    (globalThis as { fetch: unknown }).fetch = async (url: string, init: RequestInit) => {
      bodies.push({ url, ...(JSON.parse(String(init.body)) as Record<string, unknown>) });
      return { ok: true, json: async () => ({ run: true }) } as unknown as Response;
    };
    try {
      const { admitQueueSend } = await import("@platform/lib/api");
      await admitQueueSend({ project: "/w/app", session_id: "", message: "go", run_id: "r-9" });
      expect(bodies[0].url).toBe("/api/tasks/queue/admit");
      expect(bodies[0].run_id).toBe("r-9");
      await admitQueueSend({ project: "/w/app", session_id: "s1", message: "go" });
      expect(bodies[1]).not.toHaveProperty("run_id");
    } finally {
      (globalThis as { fetch: unknown }).fetch = realFetch;
    }
  });
});

describe("a follow-up into this chat's own running turn", () => {
  it("draws no row, no card and no footnote under the flag", () => {
    // Three plain bubbles and a composer footnote counting them was the shipped
    // state; then a chip counting them was the fix; and both were chrome for the
    // ONE waiting state with nothing to act on — no entry to be behind, nothing
    // to run next, nothing to delete, and a live host draining them in seconds.
    expect(CHAT).not.toContain("state.queued.length");
    expect(CHAT).not.toContain('kind="inbox"');
    expect(WAITING).not.toContain("follow-up");
    // The composer's note is gated on the flag, and the words are untouched for
    // the build that still shows them.
    expect(COMPOSER).toContain("{count > 0 && !queueOn ? (");
    expect(COMPOSER).toContain('"1 follow-up is queued for this turn."');
    expect(COMPOSER).toContain("follow-ups are queued for this turn.");
    expect(CHAT).toContain("queueOn,");
  });
});

describe("what still shuts the composer", () => {
  it("is a message the READER scheduled, and nothing else, under the flag", () => {
    // A calendar entry aimed at this session is a turn the scheduler is about to
    // start HERE, and a line typed over it is two messages racing into one run.
    // A chat-origin entry is the reader's own line, already admitted into this
    // conversation's own order — it never shuts anything. Driven for real, both
    // ways, in sched/useSchedule.test.tsx.
    // THE SERVER IS ASKED FIRST (`queue_blocking`) and the entry rule is the
    // fallback for the paint before the row lands — one rule, server first
    // (design.md, UI), rather than a client opinion running beside the server's.
    expect(USE_SCHEDULE).toContain(
      "const rowBlocking = rec && typeof rec.queue_blocking === \"boolean\" ? rec.queue_blocking : null;",
    );
    expect(USE_SCHEDULE).toContain(
      "? (rowBlocking === null ? calendarHere.length > 0 : rowBlocking) && !!sessionId",
    );
    expect(WATCHER).toContain("export function schedIsCalendar(");
    expect(WATCHER).toContain("return !!entry && !entry.origin;");
    // …and under the flag the old block card draws NOTHING: the same messages are
    // drawn as messages, and one card over the composer summarises them. Two
    // shapes for one fact a few pixels apart is what browser QA sent back.
    expect(USE_SCHEDULE).toContain("const blockers = useMemo(");
    expect(USE_SCHEDULE).toContain("(queueOn ? EMPTY_ROWS : pendingBlockers)");
    const block = readFileSync(join(HERE, "SchedBlock.tsx"), "utf8");
    expect(block).toContain("if (!next) return null;");
  });

  it("is PENDING-ONLY with the flag off — main, byte for byte", () => {
    // `schedIsWaiting` was widened to `pending` OR `sending` so the queue's ROWS
    // would not blink out for the second between a claim and the turn landing.
    // The same list is what the flag-OFF block reads, so the widening reached a
    // composer main never shut: a claimed entry briefly closed the box, with the
    // banner naming a message that was already on its way (regression found
    // 2026-09-12). Narrowed at the seam, not in the poller — `sending` is
    // genuinely a row the queue draws.
    expect(WATCHER).toContain("export function schedIsPending(");
    expect(WATCHER).toContain('return !!entry && entry.state === "pending";');
    expect(WATCHER).toContain("export function schedPendingOnly(");
    expect(USE_SCHEDULE).toContain("const pendingBlockers = useMemo(");
    expect(USE_SCHEDULE).toContain("(queueOn ? allBlockers : schedPendingOnly(allBlockers))");
    // EVERY flag-off consumer reads the narrowed list, or the fix is one place
    // wide: `waitingHere` (and therefore `next`, `hasCard` and `blocked`) and the
    // calendar half both take it.
    expect(USE_SCHEDULE).toContain(
      "(queueOn ? waitingFor(allRows, sessionId, leaderId) : pendingBlockers)",
    );
    expect(USE_SCHEDULE).toContain("schedCalendarHere(pendingBlockers)");
    // …and the ROWS keep the wide rule, which is the whole reason the two exist.
    expect(readFileSync(join(HERE, "../sched/waiting.ts"), "utf8")).toContain(
      "!schedIsWaiting(e)",
    );
  });
});

describe("a card answered while the folder is busy", () => {
  it("routes the decision through the queue's door, and only under the flag", () => {
    expect(CONTROLLER).toContain("if (queueEnabled()) {");
    expect(CONTROLLER).toContain("const held = await decideThroughQueue({");
    const decide = CONTROLLER.slice(CONTROLLER.indexOf("const decide = async ("));
    expect(decide.indexOf('"decide",')).toBeGreaterThan(decide.indexOf("if (queueEnabled()) {"));
  });

  it("latches the card exactly as a delivered answer does", () => {
    expect(CONTROLLER).toContain(
      'resolveLocally(id, fields.decision, "", "", optimistic, held.ahead || "");',
    );
    expect(CONTROLLER).toContain("...(queuedAhead === undefined ? {} : { queuedAhead }),");
  });

  it("says 'runs next after TASK-041' rather than claiming a verdict", () => {
    // "✓ Allowed" would be a claim about something that has not happened: the
    // tool has not seen the answer yet.
    const { queuedAnswerText } = require("./PermCard") as {
      queuedAnswerText: (a: string) => string;
    };
    expect(queuedAnswerText("TASK-041")).toBe("◷ Answer queued — runs next after TASK-041");
    expect(queuedAnswerText("")).toBe("◷ Answer queued — runs next in this folder");
    const statusFor = PERM.slice(PERM.indexOf("function statusFor("));
    expect(statusFor.indexOf("row.queuedAhead !== undefined")).toBeLessThan(
      statusFor.indexOf('row.decision === "allow"'),
    );
  });

  it("is EVERY card kind, because every card kind goes through the one door", () => {
    for (const caller of ["decidePermission", "answerQuestion", "decidePlan", "dismissCard"]) {
      const at = CONTROLLER.indexOf("function " + caller + "(");
      expect(at).toBeGreaterThan(-1);
      expect(CONTROLLER.slice(at, at + 1400)).toContain("decide(");
    }
    for (const card of [ASK_CARD, PLAN_CARD]) {
      expect(card).toContain('import { answerHeld, queuedAnswerText } from "./PermCard";');
      expect(card).toContain("const held = answerHeld(row);");
      expect(card).toContain("const resolved = !!row.decision || held;");
      expect(card.indexOf("const status = held")).toBeGreaterThan(-1);
      expect(card.indexOf("const status = held")).toBeLessThan(card.indexOf('"allow", '));
    }
  });
});

describe("the queue's ink in this pane", () => {
  it("is the faded yellow, and `--activity` is gone from every queued surface", () => {
    // Blue was the queue's colour for a day. The lane fold retired it: `queued`
    // is drawn INSIDE In Progress now, so a blue row under an amber one said the
    // two were unrelated kinds of work. They are one kind at two strengths.
    const css = SCHED_CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    expect(css).not.toContain("--activity");
    expect(css).toContain("var(--status-queued)");
    // …and no hex was minted for it in this stylesheet either.
    expect(css).not.toMatch(/c-waiting[^}]*#[0-9a-f]{3,6}/);
  });
});

describe("a claimed message is still on screen", () => {
  it("says `starting` and offers nothing to take back", () => {
    // The second between the scheduler taking the entry and the turn appearing
    // above this row. Drawn only because `schedIsWaiting` keeps a `sending` entry
    // in the list: reading `pending` alone took the row down on the poll that saw
    // the claim and left a hole until the turn landed (Bugbot PR #1124).
    const r = render(
      <WaitingRow row={row({ word: "starting" })} facts={{ queue_ahead: "TASK-038" }} onDelete={() => {}} />,
    );
    const text = textOf(r);
    expect(text).toContain("starting");
    expect(text).not.toContain("delete");
    // …and nothing about being behind anything: it is not in the line any more.
    expect(r.root.findAllByType("button")).toHaveLength(0);
    act(() => r.unmount());
  });
});

describe("a repeating message's two verbs", () => {
  const repeatRow = row({ repeat: true, stopId: "tpl-1" });

  it("says `skip this run`, because that is what the press does", () => {
    // `delete` promised something it does not do: the template arms the next
    // occurrence the moment this one is skipped.
    let skipped = 0;
    const r = render(
      <WaitingRow
        row={repeatRow}
        facts={{}}
        onDelete={() => { skipped += 1; }}
        onStopRepeat={() => {}}
      />,
    );
    expect(textOf(r)).toContain("skip this run");
    expect(textOf(r)).not.toContain(">delete<");
    act(() => r.root.findAllByType("button")[0].props.onClick());
    expect(skipped).toBe(1);
    act(() => r.unmount());
  });

  it("keeps an ARMED stop for the repeat itself, two presses and no fewer", () => {
    // The only way to stop a repeating message from inside the chat now that the
    // block draws nothing under the flag — and it spends every future run, so the
    // first press only arms and the label it arms into names the loss.
    let stopped = 0;
    const r = render(
      <WaitingRow
        row={repeatRow}
        facts={{}}
        onDelete={() => {}}
        onStopRepeat={() => { stopped += 1; }}
      />,
    );
    const stop = () => r.root.findAllByType("button")[1];
    expect(stop().children.join("")).toBe("stop repeating");
    act(() => stop().props.onClick());
    expect(stopped).toBe(0);
    expect(stop().children.join("")).toBe("stop every future run");
    act(() => stop().props.onClick());
    expect(stopped).toBe(1);
    act(() => r.unmount());
  });

  it("offers no stop at all on an ordinary message", () => {
    const r = render(<WaitingRow row={row()} facts={{}} onDelete={() => {}} />);
    expect(textOf(r)).toContain("delete");
    expect(textOf(r)).not.toContain("stop repeating");
    expect(r.root.findAllByType("button")).toHaveLength(1);
    act(() => r.unmount());
  });

  it("posts the TEMPLATE id, which is the only id that stops the repeat", () => {
    // Cancelling the occurrence moves the repeat rather than lifting it
    // (`schedStopTarget`), so the row carries the template and the chat spends it.
    expect(CHAT).toContain("onStopRepeat={() => void deleteWaiting(row.entryId, row.stopId)}");
    expect(WATCHER).toContain("export function schedStopTarget(");
    const waiting = readFileSync(join(HERE, "../sched/waiting.ts"), "utf8");
    expect(waiting).toContain('stopId: schedIsRepeat(entry) ? schedStopTarget(entry) : "",');
  });
});

describe("the card's count is the server's", () => {
  it("reads `queue_waiting` and never the rows on screen", () => {
    // The rows include a message scheduled for next Tuesday and a seed no poll
    // has confirmed; neither is something anybody is waiting behind, and counting
    // them said "1 message queued" for six days (Bugbot PR #1124).
    expect(CHAT).toContain("const said = sched.rec?.queue_waiting;");
    // ZERO IS AN ANSWER, so the test is on the TYPE and not on the number: a row
    // saying nothing is waiting takes the card down, and only a server too old to
    // send the field at all falls through to the count below.
    expect(CHAT).toContain('if (typeof said === "number") return said;');
    // That fallback counts only the rows that really are in the line — the same
    // sentence, said with what is in hand.
    expect(CHAT).toContain('return waiting.filter((r) => r.word === "queued").length;');
    expect(CHAT).toContain("{queueOn && waitCount > 0 ? (");
    expect(CHAT).toContain("count={waitCount}");
    expect(WATCHER).toContain("queue_waiting?: number;");
  });
});

describe("a follow-up the live run is still holding", () => {
  it("is drawn from the RUN, so a reload paints it back", () => {
    // A line typed into a running chat is absorbed by the live host and sits in
    // the CLI's own queue. Until the model gets to it the only copy on screen
    // was this page's optimistic bubble — client memory — and the transcript
    // cannot help, because nothing has consumed the message and it is in no
    // JSONL row. So a reload, or the standing watch's four-a-minute
    // `refreshHistory`, wiped the reader's own words (Akshil, 2026-09-12).
    expect(CONTROLLER).toContain("const publishInbox = (rows: InboxMessage[]) => {");
    expect(CONTROLLER).toContain("publishInbox(Array.isArray(poll.inbox) ? poll.inbox : []);");
    // …AND FROM `history`, WHICH IS THE READ A RELOADING CHAT MAKES FIRST. The
    // poll's copy only keeps these up while a page stays open; a page that comes
    // BACK has no run to poll yet, and that window is exactly when the reader is
    // looking for the follow-up they typed. Asked beside `live_run`, which is the
    // field that says whether anything is running at all.
    expect(CONTROLLER).toContain(
      "if (live) publishInbox(Array.isArray(res.inbox) ? res.inbox : []);",
    );
    const land = CONTROLLER.slice(CONTROLLER.indexOf("function landHistory("));
    expect(land.indexOf("publishInbox(")).toBeLessThan(land.indexOf("turns: historyToTurns(res),"));
    // …and nothing is held once the run is over, the same rule `clearQueued`
    // states for the optimistic half.
    expect(CONTROLLER).toContain("const clearInbox = () => publishInbox([]);");
    expect(CONTROLLER).toContain("clearInbox();");
    // DEDUPED AT RENDER, because "does this still need a bubble" is a question
    // about what is on screen — and both answers move without the inbox moving.
    expect(CHAT).toContain("const inboxRows = useMemo(");
    expect(CHAT).toContain("inboxBubbles(");
    expect(CHAT).toContain("state.inbox,");
    expect(CHAT).toContain("state.queued,");
  });

  it("is an ORDINARY bubble with no chrome of any kind", () => {
    // It is not queued, it is not behind anything, and the host drains it in
    // seconds: a line saying so would be this feature narrating the app's normal
    // behaviour back at the reader (design.md, UI). So no `.c-waiting-line`, no
    // dashed edge, no delete — the transcript's own user turn, full strength.
    // Drawn only under the flag (review, 2026-09-16): flag off, a mid-turn
    // reload paints exactly what main paints.
    const at = CHAT.indexOf("{queueOn &&\n              inboxRows.map((row) => (");
    expect(at).toBeGreaterThan(-1);
    const block = CHAT.slice(at, CHAT.indexOf("))}", at));
    expect(block).toContain('<div className="c-inbox" key={row.id}>');
    expect(block).toContain('<div className="bubble">{row.text}</div>');
    expect(block).not.toContain("c-waiting-line");
    expect(block).not.toContain("delete");
    const css = SCHED_CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    const inbox = css.slice(css.indexOf(".c-inbox {"), css.indexOf("}", css.indexOf(".c-inbox {")));
    // The log's own column, so the bubble lands where the turns above it land.
    expect(inbox).toContain("max-width: 720px");
    expect(inbox).toContain("padding: 0 20px 6px");
    expect(inbox).not.toContain("dashed");
  });
});
