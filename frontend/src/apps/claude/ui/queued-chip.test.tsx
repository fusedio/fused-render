// THE CHAT'S HALF OF THE PROJECT QUEUE (prefs `queue.enabled`).
//
// What has to be true, and none of it is visible from a unit test of the
// caption: the admission is asked BEFORE anything is spent, a queued send's
// words move from the transcript onto the chip (the one place that survives a
// history refresh and the leader's adoption), the composer stays open, the chip
// reuses the scheduled-message block's own row rather than inventing a second
// one, and a decide on a parked card goes through the queue's door only while
// the flag is on.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { QueuedChip } from "./QueuedChip";

const HERE = new URL(".", import.meta.url).pathname;
const CHAT = readFileSync(join(HERE, "../ClaudeChat.tsx"), "utf8");
const CONTROLLER = readFileSync(join(HERE, "../protocol/run-controller.ts"), "utf8");
const USE_SCHEDULE = readFileSync(join(HERE, "../sched/useSchedule.ts"), "utf8");
const WATCHER = readFileSync(join(HERE, "../sched/scheduled.ts"), "utf8");
const CHIP = readFileSync(join(HERE, "QueuedChip.tsx"), "utf8");
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

describe("the chip under a queued bubble", () => {
  it("says the word, the place and who is in front", () => {
    const r = render(
      <QueuedChip
        send={{
          entryId: "e1",
          text: "",
          queue_position: 2,
          queue_ahead: "TASK-041",
          queue_ahead_title: "Pull today's news",
        }}
        onSkip={() => {}}
      />,
    );
    const out = textOf(r);
    // "Queued" leads, because without it the rest reads as a note about
    // somebody ELSE's run rather than about the message just sent.
    expect(out).toContain("Queued");
    expect(out).toContain("#2 in line · behind TASK-041");
    expect(out).toContain("Skip");
    act(() => r.unmount());
  });

  it("offers Skip everywhere but on a spot already claimed", () => {
    const mid = render(
      <QueuedChip
        send={{ text: "", entryId: "e1", queue_position: 3 }}
        onSkip={() => {}}
      />,
    );
    expect(textOf(mid)).not.toContain('"disabled":true');
    act(() => mid.unmount());
    // #1 IS NOT THE HEAD. It is where this stood when the server last looked,
    // and anything else in the folder can be skipped over it a second later —
    // so the row that most wants Skip still has it (browser QA, 2026-09-12).
    const first = render(
      <QueuedChip
        send={{ text: "", entryId: "e1", queue_position: 1 }}
        onSkip={() => {}}
      />,
    );
    const at1 = textOf(first);
    expect(at1).toContain("#1 in line");
    expect(at1).not.toContain('"disabled":true');
    act(() => first.unmount());
    // Skipped: the spot is claimed, and the button STAYS and is dead rather than
    // disappearing on the press that worked.
    const head = render(
      <QueuedChip
        send={{ text: "", entryId: "e1", queue_position: 1, queue_priority: true }}
        onSkip={() => {}}
      />,
    );
    const out = textOf(head);
    expect(out).toContain("Skip");
    expect(out).toContain("runs next");
    expect(out).toContain('"disabled":true');
    act(() => head.unmount());
  });

  it("says 'after your previous message' when what is in front is the reader's own", () => {
    // A follow-up into a chat whose first message is still waiting. The folder
    // may be perfectly free — what this is behind is the line above it — so
    // "behind TASK-041" would send the reader looking for a task they do not
    // have, and a position the server could not give is already answered by the
    // phrase itself.
    const r = render(
      <QueuedChip
        send={{ text: "", entryId: "e2", behind_own: true, queue_ahead: "" }}
        onSkip={() => {}}
      />,
    );
    const out = textOf(r);
    expect(out).toContain("Queued");
    expect(out).toContain("after your previous message");
    expect(out).not.toContain("in line");
    expect(out).not.toContain("behind a run in this folder");
    // Skip is still live: a follower can claim the head of its folder's line
    // exactly as any other queued message can.
    expect(out).toContain("Skip");
    expect(out).not.toContain('"disabled":true');
    act(() => r.unmount());
    // …AND A SERVER THAT CAN PLACE IT STILL SAYS ONLY THE ONE HALF (browser QA
    // round 2). `#1 in line · after your previous message` was the chip that
    // sent this back: the phrase already names the exact thing in front, so the
    // number either repeats it or contradicts it — the folder's line counts
    // strangers' tasks this message is not standing behind.
    const placed = render(
      <QueuedChip
        send={{ text: "", entryId: "e2", behind_own: true, queue_position: 2 }}
        onSkip={() => {}}
      />,
    );
    const placedOut = textOf(placed);
    expect(placedOut).toContain("Queued");
    expect(placedOut).toContain("after your previous message");
    expect(placedOut).not.toContain("in line");
    expect(placedOut).not.toContain("#");
    act(() => placed.unmount());
    // Skipped, the one head a follow-up keeps: a claim on the spot, not a count.
    const next = render(
      <QueuedChip
        send={{ text: "", entryId: "e2", behind_own: true, queue_position: 2, queue_priority: true }}
        onSkip={() => {}}
      />,
    );
    expect(textOf(next)).toContain("runs next · after your previous message");
    act(() => next.unmount());
  });

  it("calls back exactly once per press", () => {
    let pressed = 0;
    const r = render(
      <QueuedChip
        send={{ text: "", entryId: "e1", queue_position: 4 }}
        onSkip={() => {
          pressed += 1;
        }}
      />,
    );
    const button = r.root.findAllByType("button")[0];
    act(() => button.props.onClick());
    expect(pressed).toBe(1);
    act(() => r.unmount());
  });

  it("is the scheduled block's own row, not a second card that looks like one", () => {
    // A queued message IS a pending scheduled message — the server created
    // exactly that — so a shape of its own would be two cards for one fact,
    // sitting a few pixels apart in the same column.
    expect(CHIP).toContain('className="c-schedblock c-queuechip"');
    expect(CHIP).toContain('className="sb-card sb-card--chip"');
    expect(CHIP).toContain('className="sb-ring sb-ring--queued"');
    expect(CHIP).toContain('<span className="sb-acts">');
    expect(SCHED_CSS).toContain(".c-schedblock .sb-card--chip {");
  });

  it("wraps instead of being told a width — this pane is 340px in a sidebar", () => {
    const css = SCHED_CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    const card = css.slice(
      css.indexOf(".c-schedblock .sb-card--chip {"),
      css.indexOf("}", css.indexOf(".c-schedblock .sb-card--chip {")),
    );
    expect(card).toContain("flex-wrap: wrap");
    expect(card).not.toMatch(/(?<!-)\bwidth:\s*\d/);
    const line = css.slice(
      css.indexOf(".c-schedblock .sb-queued-line {"),
      css.indexOf("}", css.indexOf(".c-schedblock .sb-queued-line {")),
    );
    // The CAPTION is the cell that gives way, never the button.
    expect(line).toContain("min-width: 0");
    expect(line).toContain("text-overflow: ellipsis");
    expect(css).not.toMatch(/@media[^{]*\{[^}]*sb-queued/);
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

  it("hands the words to the chip when the folder was busy — never lost, never doubled", () => {
    const send = CHAT.slice(CHAT.indexOf("const dispatchSend = useCallback("));
    const queued = send.indexOf("if (verdict && verdict.run === false) {");
    expect(queued).toBeGreaterThan(0);
    const road = send.slice(queued, queued + 5000);
    // THE WORDS GO ON THE SEND, which is what survives the two things that
    // replace the live transcript under a queued message: the standing watch's
    // `refreshHistory` and the adoption of the leader's session. The optimistic
    // row does NOT stay — `taken` is left false, so the `finally` drops it and
    // the chip's own bubble is the one copy on screen (Bugbot, PR #1124).
    expect(road).toContain("text,");
    expect(road).not.toContain("taken = true;");
    expect(send).toContain("if (!taken && optimisticKey) controller.dropOptimisticUser(optimisticKey);");
  });

  it("draws those words in the transcript's own bubble, above the chip", () => {
    const r = render(
      <QueuedChip
        send={{ entryId: "e1", text: "count the rows", queue_position: 2 }}
        onSkip={() => {}}
      />,
    );
    const bubbles = r.root.findAll(
      (n) => typeof n.type === "string" && n.props.className === "bubble",
    );
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0]!.props.children).toBe("count the rows");
    // The transcript's OWN classes, not a second skin: `.turn.user .bubble` is
    // what dresses it, and this block sits in the log's own column.
    const row = r.root.findAll(
      (n) => typeof n.type === "string" && n.props.className === "turn user c-queuesaid",
    );
    expect(row).toHaveLength(1);
    expect(SCHED_CSS).toContain(".c-schedblock.c-queuechip .turn.user.c-queuesaid {");
    // …and the caption is still there, under the words.
    expect(textOf(r)).toContain("#2 in line");
    act(() => r.unmount());
  });

  it("draws no row for a WORDLESS send, whose bubble is markers only the controller can build", () => {
    const r = render(
      <QueuedChip send={{ entryId: "e1", text: "", queue_position: 2 }} onSkip={() => {}} />,
    );
    expect(
      r.root.findAll((n) => typeof n.type === "string" && n.props.className === "bubble"),
    ).toHaveLength(0);
    expect(textOf(r)).toContain("Queued");
    act(() => r.unmount());
  });

  it("keeps every queued message's words through the adoption that replaces the transcript", () => {
    // THE FAILURE (Bugbot, PR #1124): a new chat's first message queues, a
    // second queues behind it, the leader runs, and the chat adopts the session
    // its run opened — `openSession` REPLACES the transcript with the JSONL,
    // which holds neither follower. The chips stayed and said "Queued" over a
    // conversation showing none of the words they were about.
    //
    // The chips are drawn from `queuedSends`, which the adoption does not touch
    // (only Back and an explicit session switch clear it), so the words come
    // with them — one source, no re-posting of bubbles the next refresh would
    // drop all over again.
    const sends = [
      { entryId: "e1", text: "count the rows", queue_position: 1 },
      { entryId: "e2", text: "and the columns?", behind_own: true },
    ];
    const r = render(
      <>
        {sends.map((s) => (
          <QueuedChip key={s.entryId} send={s} onSkip={() => {}} />
        ))}
      </>,
    );
    const out = textOf(r);
    expect(out).toContain("count the rows");
    expect(out).toContain("and the columns?");
    act(() => r.unmount());
    // …and the row goes with the chip when the entry fires, because they are one
    // component: `liveQueued` is what the chat maps over.
    expect(CHAT).toContain("useQueuedLiveness(queuedSends, sched.pendingIds)");
    expect(CHAT).toContain("{liveQueued.map((q) => (");
    // The adoption leaves the list alone — only Back clears it (the leader's own
    // suite asserts that half).
    const adopt = CHAT.slice(
      CHAT.indexOf("const adoptSession = leaderSession("),
      CHAT.indexOf("}, [adoptSession, controller, cardPolicy]);"),
    );
    expect(adopt).not.toContain("setQueuedSends");
  });

  it("sends NOTHING when the ask does not come back with a clean answer", () => {
    // This fell through to a spawn once. But the failure that actually happens
    // is the server refusing the send — a wordless one it will not queue, a 400
    // of any kind — and falling through turns every refusal into the second run
    // in a busy folder the whole feature exists to prevent.
    const send = CHAT.slice(CHAT.indexOf("const dispatchSend = useCallback("));
    const admit = send.indexOf("await admitQueueSend({");
    const catchBlock = send.indexOf("} catch (err) {", admit);
    const spawn = send.indexOf("await beginSend(opts)");
    expect(catchBlock).toBeGreaterThan(0);
    expect(catchBlock).toBeLessThan(spawn);
    expect(send.slice(catchBlock, spawn)).toContain("refuseQueuedSend(text, err);");
    // …and a shape this build cannot read is not a yes either.
    expect(send).toContain("if (run !== true && run !== false) {");
    expect(send).toContain('refuseQueuedSend(text, new Error("the queue gave no answer."));');
  });

  it("puts a refused send's words back in the box and its reason on the card", () => {
    // The composer cleared the box on the keystroke, so unless the words come
    // back they are simply gone — and the reason goes in the slot a failed
    // `start` writes, because from the reader's side this IS a failed send.
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
    // A queued send runs minutes later out of the scheduler: whatever is not on
    // the entry is not in the message that eventually goes. The bytes travel the
    // same road "Schedule this as a task" already uses.
    const send = CHAT.slice(CHAT.indexOf("const dispatchSend = useCallback("));
    const carry = send.indexOf("await carryForQueue(");
    const admit = send.indexOf("await admitQueueSend({");
    expect(carry).toBeGreaterThan(0);
    expect(carry).toBeLessThan(admit);
    expect(send).toContain("images: carried.map((a) => a.path), attachments: carried");
    expect(CHAT).toContain("return copyToTaskShots(tray).catch(() => []);");
    // Queued: the tray is spent, exactly as a send that ran spends it — the
    // entry has its own copies now, and chips left behind would ride the NEXT
    // message a second time.
    const queued = send.indexOf("if (verdict && verdict.run === false) {");
    expect(send.slice(queued, queued + 5000)).toContain("spendTrayForQueue();");
  });

  it("leaves the composer OPEN under the flag, and shut without it", () => {
    // Flag off, a pending entry aimed at this conversation shuts the box: it is
    // about to be claimed and sent into this very session, and a line typed over
    // it is two messages racing into one run.
    //
    // Flag on, that ordering is the scheduler's job — admission queues a send
    // into a session that has due pending entries of its own (`behind_own`), so
    // the second line becomes the next entry in this conversation's line instead
    // of a race. Shutting the box would refuse a message the server will take.
    // Driven for real, both ways, in sched/useSchedule.test.tsx.
    expect(USE_SCHEDULE).toContain("const blocked = !queueOn && hasCard;");
    expect(USE_SCHEDULE).toContain("const queueOn = opts.queueEnabled ?? queuePref;");
    // The CARD is not the block: it says "a message of yours is waiting", which
    // is true with the box open, so it keeps its row number and its scroll
    // correction under the flag.
    expect(USE_SCHEDULE).toContain("const hasCard = blockers.length > 0 && !!sessionId;");
    expect(USE_SCHEDULE).toContain("if (!hasCard || !nextId) return;");
  });

  it("joins a follow-up to the message already in the line, not to a new task", () => {
    // A chat whose first message queued has NO session — nothing has run — so a
    // second send would be admitted as another session-less message and open a
    // SECOND task in the same folder, forking the conversation on screen. The
    // leader's entry id is what joins them (`follow_of`); the rule and both its
    // edges are unit-tested in sched/queue-leader.test.tsx.
    const send = CHAT.slice(CHAT.indexOf("const dispatchSend = useCallback("));
    expect(send).toContain("const sid = controller.getState().sessionId ?? \"\";");
    expect(send).toContain("const follow = leader.followOf(sid);");
    expect(send).toContain("...(follow ? { follow_of: follow } : {}),");
    // The session id on the body is the SAME read the leader was asked with, so
    // the two halves cannot disagree about what the message is addressed to.
    expect(send).toContain("session_id: sid,");
    // …and the entry just queued becomes the leader for everything typed after
    // it, while this chat still has no session.
    const queued = send.indexOf("if (verdict && verdict.run === false) {");
    expect(send.slice(queued, queued + 5000)).toContain("leader.remember(sid, entryId);");
  });

  it("takes a chip down when its entry stops being pending — and not one poll sooner", () => {
    // `pendingIds` is the UNFILTERED pending set: `blockers` is filtered by
    // session, and the chat that most needs a chip is the brand-new one that has
    // no session yet.
    expect(WATCHER).toContain("onPending?(ids: string[]): void;");
    expect(WATCHER).toContain('entries.filter((e) => e && e.state === "pending").map((e) => String(e.id))');
    // …and it is NOT a plain filter: that set is a photograph older than the
    // send it is being asked about, so a just-queued entry is legitimately
    // missing from it (sched/queued-sends, which is unit-tested on its own).
    expect(CHAT).toContain("useQueuedLiveness(queuedSends, sched.pendingIds)");
    expect(CHAT).not.toContain("queuedSends.filter((q) => sched.pendingIds?.has(q.entryId))");
  });
});

describe("Skip, from the chip", () => {
  it("names the ENTRY on the wire, never the task key the admission answered", () => {
    // THE ROUND-2 BUG. A chat with no session queues as `pending:<leader id>`,
    // and the store rekeys that task onto the Claude session the leader's run
    // opens — so a key frozen at admission time 404s from the first run onwards,
    // and the chip's catch swallowed it. The entry id is minted once and never
    // rekeyed. Read off the real `postJson` road, X-Fused guard included, rather
    // than off the type.
    const send = CHAT.slice(CHAT.indexOf("const skipQueued = useCallback("));
    expect(send).toContain("await skipQueue({ entry_id: send.entryId })");
    // …and nothing in this pane posts the stale name any more: the admission
    // answer's `key` is not even kept on the send.
    expect(send).not.toContain("skipQueue(send.key)");
    expect(CHIP).not.toContain("key: string;");
  });

  it("says so when the server refuses, instead of swallowing it", () => {
    // A press with a visible control behind it that answers with silence is a
    // button that did nothing — and the failure this actually hid was a 404 on a
    // stale key, invisible for as long as nobody read the network tab. The
    // server's own sentence goes in the slot a failed send writes.
    const send = CHAT.slice(
      CHAT.indexOf("const skipQueued = useCallback("),
      CHAT.indexOf("const adoptSession ="),
    );
    expect(send).toContain("} catch (err) {");
    expect(send).toContain("const t = troubleFromError(err);");
    expect(send).toContain(
      'controller.reportTrouble({ ...t, message: "Skip did not go through: " + t.message });',
    );
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
      // …and the Tasks row's own name still goes through the same door.
      await skipQueue({ key: "TASK-041" });
      expect(bodies[1].key).toBe("TASK-041");
      expect(bodies[1]).not.toHaveProperty("entry_id");
    } finally {
      (globalThis as { fetch: unknown }).fetch = realFetch;
    }
  });
});

describe("a card answered while the folder is busy", () => {
  it("routes the decision through the queue's door, and only under the flag", () => {
    expect(CONTROLLER).toContain("if (queueEnabled()) {");
    expect(CONTROLLER).toContain("const held = await decideThroughQueue({");
    // Flag off: the agent's own action, byte for byte as before.
    const decide = CONTROLLER.slice(CONTROLLER.indexOf("const decide = async ("));
    expect(decide.indexOf('"decide",')).toBeGreaterThan(decide.indexOf("if (queueEnabled()) {"));
  });

  it("latches the card exactly as a delivered answer does", () => {
    // From the reader's side the decision is MADE: first-writer-wins, a second
    // click ignored. Only the delivery is still ahead.
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
    // Read AHEAD of the three verdicts, or a held Allow would print as allowed.
    const statusFor = PERM.slice(PERM.indexOf("function statusFor("));
    expect(statusFor.indexOf("row.queuedAhead !== undefined"))
      .toBeLessThan(statusFor.indexOf('row.decision === "allow"'));
  });

  it("is EVERY card kind, because every card kind goes through the one door", () => {
    // The door is the controller's `decide`, and all four callers funnel into
    // it — so a question and a plan can be held exactly as an approval can, and
    // a card that read `row.decision` alone was a card that could not say so.
    // QA found the question card doing precisely that: "✓ Answered" over a
    // choice the model has not been told, and OPTIONS BACK after a reload
    // (2026-09-12).
    for (const caller of ["decidePermission", "answerQuestion", "decidePlan", "dismissCard"]) {
      const at = CONTROLLER.indexOf("function " + caller + "(");
      expect(at).toBeGreaterThan(-1);
      expect(CONTROLLER.slice(at, at + 1400)).toContain("decide(");
    }
    // …and both of the other cards read the SAME latch rather than re-deriving
    // one: `answerHeld` is `queuedAhead` (this document) OR `held` (the server,
    // which is the half that survives a reload).
    for (const card of [ASK_CARD, PLAN_CARD]) {
      expect(card).toContain('import { answerHeld, queuedAnswerText } from "./PermCard";');
      expect(card).toContain("const held = answerHeld(row);");
      expect(card).toContain("const resolved = !!row.decision || held;");
      // Ahead of the verdicts, for the reason PermCard's own branch is.
      expect(card.indexOf("const status = held")).toBeGreaterThan(-1);
      expect(card.indexOf("const status = held")).toBeLessThan(card.indexOf('"allow", '));
    }
  });
});
