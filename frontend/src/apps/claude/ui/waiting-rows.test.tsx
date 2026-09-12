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
//   * One summary card over the composer carries the count, what is in front, and
//     Run next — and Run next is offered only when there is something to get in
//     front of.
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
  it("reads `queued · behind TASK-038 · delete`, with the id as a link", () => {
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
    expect(out).toContain("behind ");
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
});

describe("the card over the composer", () => {
  it("counts the messages and names what is in front", () => {
    const r = render(
      <WaitingCard count={2} facts={{ queue_ahead: "TASK-038" }} onRunNext={() => {}} />,
    );
    const out = textOf(r);
    expect(out).toContain("2 messages waiting");
    expect(out).toContain("TASK-038");
    expect(out).toContain("Run next");
    act(() => r.unmount());
  });

  it("says '1 message waiting' for one, and draws nothing for none", () => {
    const one = render(<WaitingCard count={1} facts={{}} onRunNext={() => {}} />);
    expect(textOf(one)).toContain("1 message waiting");
    expect(textOf(one)).not.toContain("messages");
    act(() => one.unmount());
    // "0 messages waiting" is a card about nothing sitting on top of the box.
    const none = render(<WaitingCard count={0} facts={{}} onRunNext={() => {}} />);
    expect(none.toJSON()).toBe(null);
    act(() => none.unmount());
  });

  it("offers Run next ONLY while another task is actually in front", () => {
    // A control whose only possible outcome is the state you are already in
    // teaches the reader it does nothing.
    const free = render(<WaitingCard count={2} facts={{}} onRunNext={() => {}} />);
    expect(free.root.findAllByType("button")).toHaveLength(0);
    expect(textOf(free)).toContain("next in this folder");
    act(() => free.unmount());
  });

  it("reads 'next in this folder' after the press, and stops offering it", () => {
    // Run next sets `queue_priority`: TASK-038 is STILL holding the folder, and
    // nothing is in front of this any more. Both halves have to move together or
    // the card would keep offering a press that has already happened.
    const after = render(
      <WaitingCard
        count={2}
        facts={{ queue_ahead: "TASK-038", queue_priority: true }}
        onRunNext={() => {}}
      />,
    );
    const out = textOf(after);
    expect(out).toContain("2 messages waiting");
    expect(out).toContain("next in this folder");
    expect(out).not.toContain("Run next");
    act(() => after.unmount());
  });

  it("calls back once and goes dead while the press is in flight", () => {
    let pressed = 0;
    const r = render(
      <WaitingCard
        count={1}
        facts={{ queue_ahead: "TASK-038" }}
        onRunNext={() => { pressed += 1; }}
      />,
    );
    act(() => r.root.findAllByType("button")[0].props.onClick());
    expect(pressed).toBe(1);
    act(() => r.unmount());
    const busy = render(
      <WaitingCard count={1} facts={{ queue_ahead: "TASK-038" }} busy onRunNext={() => {}} />,
    );
    expect(busy.root.findAllByType("button")[0].props.disabled).toBe(true);
    act(() => busy.unmount());
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
    // TWO ADDRESSES. A chat WITH a session reads the session-filtered pending
    // list; a chat with NO session — its first message queued, so nothing has run
    // — has no such list and finds its messages by the leader entry they were
    // admitted behind.
    expect(CHAT).toContain("? sched.waitingHere");
    expect(CHAT).toContain("schedFollowing(sched.pendingRows, leaderId)");
    expect(WATCHER).toContain("export function schedFollowing(");
    expect(WATCHER).toContain("deps.onPendingRows?.(pending);");
  });

  it("is ONE answer about what is in front, and the server's when it has one", () => {
    // A folder is held by one task, so three messages in one line are behind the
    // same thing. The authority is this conversation's `/api/tasks` row, which
    // survives a reload; the admission's answer is the fallback for the first
    // paint and for a chat that has no row at all.
    expect(CHAT).toContain("waitingFacts(sched.rec, admitAhead)");
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
    expect(del).toContain("await cancelScheduledMessage(entryId);");
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
      CHAT.indexOf("}, [adoptSession, controller, cardPolicy]);"),
    );
    expect(adopt).not.toContain("setWaitingSeeds");
    expect(adopt).not.toContain("setDroppedEntries");
  });
});

describe("Run next, from the card", () => {
  it("promotes the TASK when there is a fresh key, and one entry otherwise", () => {
    // `sched.rec.key` is read off a listing the poll just took, so it cannot be
    // the stale `pending:<leader>` key the admission answered and the store has
    // since rekeyed — and the task form promotes every message this chat has
    // waiting, which is what the card is a summary of.
    const run = CHAT.slice(
      CHAT.indexOf("const runNext = useCallback("),
      CHAT.indexOf("const deleteWaiting = useCallback("),
    );
    expect(run).toContain('const key = sched.rec?.key || "";');
    expect(run).toContain("await skipQueue(key ? { key } : { entry_id: first });");
    // The claim is painted onto the card: this pane has no listing to correct it
    // from, and the server's answer is the position it just set.
    expect(run).toContain("queue_priority: true");
    // …and a refusal is said out loud, in the verb's own words.
    expect(run).toContain('"Run next did not go through: " + t.message');
  });

  it("posts exactly that body, and only that", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const realFetch = globalThis.fetch;
    (globalThis as { fetch: unknown }).fetch = async (url: string, init: RequestInit) => {
      bodies.push({ url, ...(JSON.parse(String(init.body)) as Record<string, unknown>) });
      return { ok: true, json: async () => ({ ok: true, position: 1 }) } as unknown as Response;
    };
    try {
      const { skipQueue } = await import("@platform/lib/api");
      await skipQueue({ entry_id: "e7" });
      expect(bodies[0].url).toBe("/api/tasks/queue/skip");
      expect(bodies[0].entry_id).toBe("e7");
      expect(bodies[0]).not.toHaveProperty("key");
      await skipQueue({ key: "TASK-041" });
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
    expect(USE_SCHEDULE).toContain(
      "const blocked = queueOn ? calendarHere.length > 0 && !!sessionId : hasCard;",
    );
    expect(WATCHER).toContain("export function schedIsCalendar(");
    expect(WATCHER).toContain("return !!entry && !entry.origin;");
    // …and under the flag the old block card draws NOTHING: the same messages are
    // drawn as messages, and one card over the composer summarises them. Two
    // shapes for one fact a few pixels apart is what browser QA sent back.
    expect(USE_SCHEDULE).toContain("const blockers = useMemo(");
    expect(USE_SCHEDULE).toContain("(queueOn ? EMPTY_ROWS : allBlockers)");
    const block = readFileSync(join(HERE, "SchedBlock.tsx"), "utf8");
    expect(block).toContain("if (!next) return null;");
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
