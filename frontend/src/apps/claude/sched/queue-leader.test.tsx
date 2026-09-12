// WHAT A SECOND MESSAGE JOINS when the first one is still waiting in the line
// and the chat has no session at all.
//
// The failure this prevents is not subtle and is invisible from any one render:
// the reader types twice into a new chat in a busy folder, and the folder ends
// up with TWO tasks — because a send with an empty `session_id` is how "start a
// brand-new task" is spelled everywhere else in this app. The leader's entry id
// is the whole fix, and these tests are about the three moments it has to be
// right in: taken, offered, and dropped.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, it, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { QueuedLeader } from "./queue-leader";

const { leaderAfterAdmit, leaderAfterSession, leaderFollowOf, leaderSession, useQueuedLeader } =
  await import("./queue-leader");
const { createScheduleWatcher, schedRanSessions } = await import("./scheduled");
const { readFileSync } = await import("node:fs");
const { join } = await import("node:path");
const HERE = new URL(".", import.meta.url).pathname;
const CHAT = readFileSync(join(HERE, "../ClaudeChat.tsx"), "utf8");
const USE_SCHEDULE = readFileSync(join(HERE, "useSchedule.ts"), "utf8");
// Deferred past `installDomShim()`, like every other value import of this
// module: `@platform/lib/api` reads its environment at module scope.
const { admitQueueSend } = await import("@platform/lib/api");

describe("the leader, as a rule", () => {
  it("is taken by the FIRST queued admission of a chat that has no session", () => {
    expect(leaderAfterAdmit("", "", "e1")).toBe("e1");
  });

  it("is never replaced, because followers name the LEADER and not each other", () => {
    // A chain — each entry naming the one before it — makes the grouping depend
    // on every link surviving: cancel the middle message and the third is
    // orphaned from a leader that is still perfectly alive. One name points
    // every follower at the entry the task is actually called after
    // (`pending:<leader id>`).
    expect(leaderAfterAdmit("e1", "", "e2")).toBe("e1");
    expect(leaderAfterAdmit("e1", "", "e3")).toBe("e1");
  });

  it("is never taken by a chat that HAS a session", () => {
    // There the queued entry already addresses a conversation by id, and a
    // leader would be a second answer to a question that has one.
    expect(leaderAfterAdmit("", "s9", "e1")).toBe("");
    expect(leaderAfterAdmit("e1", "s9", "e2")).toBe("");
  });

  it("ignores an admission that named no entry", () => {
    // An older server, or an answer whose `entry` did not survive the wire: an
    // empty `follow_of` is exactly "nothing to name", and remembering "" as the
    // leader would latch the chat into never taking a real one.
    expect(leaderAfterAdmit("", "", "")).toBe("");
  });

  it("is offered only while there is still no session", () => {
    expect(leaderFollowOf("e1", "")).toBe("e1");
    // THE SESSION OUTRANKS IT, and this is the guarantee rather than the
    // tidy-up: a `follow_of` beside a real `session_id` would ask the server to
    // group an addressed message under a task it predates.
    expect(leaderFollowOf("e1", "s9")).toBe("");
    expect(leaderFollowOf("", "")).toBe("");
  });

  it("is dropped the moment the chat knows its session", () => {
    // The leader's run opened one and the poll reported its
    // `claude_session_id`, or this chat acquired one the ordinary way. Either
    // way every later message addresses THAT.
    expect(leaderAfterSession("e1", "s9")).toBe("");
    expect(leaderAfterSession("e1", "")).toBe("e1");
  });
});

// ── the same rule, as the chat holds it ──────────────────────────────────────

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

async function mountLeader(session = ""): Promise<{
  leader(): QueuedLeader;
  setSession(id: string): Promise<void>;
  /** The handle's identity, which `dispatchSend` depends on. */
  handles: QueuedLeader[];
}> {
  let out: QueuedLeader | null = null;
  const handles: QueuedLeader[] = [];
  function Probe(props: { sessionId: string }) {
    out = useQueuedLeader(props.sessionId);
    handles.push(out);
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(Probe, { sessionId: session }));
  });
  mounted.push(r);
  return {
    leader: () => {
      if (!out) throw new Error("not rendered");
      return out;
    },
    async setSession(id: string) {
      await act(async () => {
        r.update(createElement(Probe, { sessionId: id }));
      });
    },
    handles,
  };
}

test("the first queued send becomes the leader every later one names", async () => {
  const h = await mountLeader();
  // Nothing queued yet: a first send names nothing, which is what makes it the
  // one that can become a leader.
  expect(h.leader().followOf("")).toBe("");
  h.leader().remember("", "e1");
  expect(h.leader().peek()).toBe("e1");
  // The second send, typed into the same composer while nothing has run.
  expect(h.leader().followOf("")).toBe("e1");
  h.leader().remember("", "e2");
  expect(h.leader().followOf("")).toBe("e1");
});

test("the leader is forgotten when the chat acquires a session", async () => {
  const h = await mountLeader();
  h.leader().remember("", "e1");
  expect(h.leader().followOf("")).toBe("e1");

  // The leader ran: the poll reported its `claude_session_id` and the chat now
  // has a conversation to address.
  await h.setSession("s9");
  expect(h.leader().peek()).toBe("");
  expect(h.leader().followOf("s9")).toBe("");
  // …and it does not come back on the next queued send either — that one is an
  // ordinary message into a session that exists.
  h.leader().remember("s9", "e3");
  expect(h.leader().peek()).toBe("");
});

test("a send that reads the session BEFORE the effect clears it still names nothing", async () => {
  // The window that actually happens: `dispatchSend` asks the controller for the
  // session id, which is fresher than any render, and fires before React has
  // run the clearing effect. `followOf` is asked with that same id, so the two
  // halves of the admit body can never disagree.
  const h = await mountLeader();
  h.leader().remember("", "e1");
  expect(h.leader().followOf("s9")).toBe("");
});

test("the leader goes with the conversation it belonged to (Back)", async () => {
  // Left standing it would file the NEXT chat's first line under a task it has
  // nothing to do with — and, now that a chat adopts the session its leader's
  // run opens, pull the reader back into the conversation they just left on the
  // very next poll.
  const h = await mountLeader();
  h.leader().remember("", "e1");
  expect(h.leader().followOf("")).toBe("e1");
  await act(async () => h.leader().forget());
  expect(h.leader().peek()).toBe("");
  expect(h.leader().followOf("")).toBe("");
  // …and the next chat may take a leader of its own, normally.
  h.leader().remember("", "e5");
  expect(h.leader().followOf("")).toBe("e5");
});

test("the handle keeps its identity, so the send callback is not rebuilt", async () => {
  // `dispatchSend` depends on it, and the composer holds that callback through a
  // prop: a new one mid-window is a new closure over a half-spent send.
  const h = await mountLeader();
  await h.setSession("s9");
  await h.setSession("s9");
  expect(new Set(h.handles).size).toBe(1);
});

// ── and what actually goes on the wire ───────────────────────────────────────

test("the second admit carries follow_of, the first carries none", async () => {
  // The body, read off the real `postJson` road — `X-Fused` guard included —
  // rather than off the type, because the type is what the server is MEANT to
  // be sent.
  const bodies: Array<Record<string, unknown>> = [];
  const realFetch = globalThis.fetch;
  (globalThis as { fetch: unknown }).fetch = async (_url: string, init: RequestInit) => {
    bodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
    return {
      ok: true,
      json: async () => ({ run: false, entry: { id: `e${bodies.length}` } }),
    } as unknown as Response;
  };
  try {
    const h = await mountLeader();
    /** One trip through the admission road exactly as `dispatchSend` drives it:
     *  read the session once, ask the leader with THAT id, remember what came
     *  back. */
    const send = async (sid: string, text: string) => {
      const follow = h.leader().followOf(sid);
      const verdict = await admitQueueSend({
        project: "/w/app",
        session_id: sid,
        message: text,
        ...(follow ? { follow_of: follow } : {}),
      });
      if (verdict.run === false) h.leader().remember(sid, String(verdict.entry?.id ?? ""));
    };

    await send("", "first");
    await send("", "second");
    await send("", "third");

    // A brand-new chat's FIRST message has nothing to name: sending a
    // `follow_of` there would point at an entry that does not exist.
    expect(bodies[0]).not.toHaveProperty("follow_of");
    expect(bodies[0].session_id).toBe("");
    // …and every later one joins the LEADER, not the message before it.
    expect(bodies[1].follow_of).toBe("e1");
    expect(bodies[2].follow_of).toBe("e1");

    // The leader fired and this chat has a session now: the next message
    // addresses the conversation, and names no entry at all.
    await h.setSession("s9");
    await send("s9", "fourth");
    expect(bodies[3].session_id).toBe("s9");
    expect(bodies[3]).not.toHaveProperty("follow_of");
  } finally {
    (globalThis as { fetch: unknown }).fetch = realFetch;
  }
});


// ── and the OTHER end of the leader: the session its run opened ──────────────
//
// A leader is taken because the chat has no session and dropped when one
// arrives. Nothing was handing one over when the SCHEDULER ran the leader: every
// road this chat has to a session id starts with this page starting something,
// and a queued leader starts nothing here. So the chat stayed session-less — new
// followers behind an entry that had long since run — and this is the fact that
// ends it, read off the entry the poll already fetches.

/** The `/api/schedule` shape, as the scheduler leaves it once a queued leader
 *  has been claimed and run. */
const ENTRIES = [
  { id: "e1", state: "sent", target: "/w/app", run_id: "r1", claude_session_id: "s9" },
  { id: "e2", state: "pending", target: "/w/app", session_id: "", follow_of: "e1" },
  { id: "z9", state: "sent", target: "/other", run_id: "r2", claude_session_id: "someone-else" },
];

describe("the session the leader's run opened", () => {
  it("is read off the entry list, by entry id", () => {
    const map = schedRanSessions(ENTRIES);
    expect(map.get("e1")).toBe("s9");
    expect(map.get("z9")).toBe("someone-else");
    // An entry that has not run reports nothing — and is LEFT OUT rather than
    // mapped to "", because "" is not an id to open a conversation with.
    expect(map.has("e2")).toBe(false);
  });

  it("is what a session-less chat adopts, and only for ITS leader", () => {
    const map = schedRanSessions(ENTRIES);
    expect(leaderSession(map, "e1", "")).toBe("s9");
    // Somebody else's finished entry is not this chat's conversation.
    expect(leaderSession(map, "e2", "")).toBe("");
  });

  it("is never adopted over a chat that already HAS one", () => {
    // Opening another conversation on top of the reader's is the one mistake
    // worth being careful about here.
    const map = schedRanSessions(ENTRIES);
    expect(leaderSession(map, "e1", "s-mine")).toBe("");
  });

  it("answers nothing before any poll, and with no leader", () => {
    expect(leaderSession(null, "e1", "")).toBe("");
    expect(leaderSession(schedRanSessions(ENTRIES), "", "")).toBe("");
  });

  it("reaches the chat through the poll it already pays for", async () => {
    // The watcher publishes the map on the same tick as `pendingIds`, so the
    // pair cannot disagree about an entry that fired between two reads — and on
    // SUCCESSFUL ticks only, because "I could not ask" must never be spelled the
    // same way as "it has not run".
    const seen: Array<Map<string, string>> = [];
    let fail = false;
    const watcher = createScheduleWatcher({
      file: "/w/app",
      fetchSchedule: async () => {
        if (fail) throw new Error("offline");
        return { entries: ENTRIES };
      },
      sessionId: () => "",
      inChat: () => false,
      busy: () => false,
      onBlockers: () => {},
      onSessions: (m) => seen.push(m),
      addNote: () => {},
      setRunParam: () => {},
      resumeRun: async () => {},
      shownRun: () => false,
    });
    await watcher.tick();
    expect(seen.length).toBe(1);
    expect(seen[0].get("e1")).toBe("s9");
    fail = true;
    await watcher.tick();
    expect(seen.length).toBe(1);
  });

  it("is adopted by the same openSession every other 'open that chat' gesture spends", () => {
    // Wiring, which no unit test of the rule can see: the map is published, the
    // rule is asked with the live session id, and the answer opens the
    // conversation — bringing the real transcript with it.
    expect(USE_SCHEDULE).toContain("onSessions: absorbSessions,");
    expect(USE_SCHEDULE).toContain("ranSessions: ReadonlyMap<string, string> | null;");
    expect(CHAT).toContain(
      'const adoptSession = leaderSession(sched.ranSessions, leader.peek(), state.sessionId ?? "");',
    );
    const effect = CHAT.slice(CHAT.indexOf("const adoptSession = leaderSession("));
    expect(effect).toContain("void controller.openSession(adoptSession);");
    // NO GUARD REF: `openSession` emits the id, the rule answers "" from then
    // on, and the effect's own dependency is what closes it.
    expect(effect).toContain("}, [adoptSession, controller, cardPolicy, params, file]);");
    // …AND THE UNSENT WORDS COME WITH THE SESSION. The composer keys its draft on
    // `new:<file>` while there is none, so the flip would leave that record
    // standing and the next keystroke would autosave the same sentence under the
    // session — one message, two drafts, and a draft ROW beside the conversation
    // (review, PR #1124). Moved HERE and not on any key change: a key that flips
    // because the reader opened some other chat is not an adoption.
    expect(effect).toContain(
      'void moveChatDraft(chatDraftKey(null, file || ""), adoptSession);',
    );
    expect(CHAT).not.toContain("moveChatDraft(draftKey");
    // …AND THE DOOR THE PANE CAME IN BY IS SHUT. A chat opened on `?queued=<id>`
    // takes that entry as its leader on every render (see `queuedParam`), so
    // leaving the param standing beside the session just adopted would keep
    // re-remembering a leader the conversation has outgrown.
    expect(effect).toContain('params.set({ [QUEUED_PARAM]: null }, { history: "replace" });');
    // …and Back takes the leader AND the waiting-row memories with it, so the
    // adoption can never reach across a conversation the reader has closed.
    // (The ROWS themselves are the server's and come back on the next poll for
    // whatever conversation is on screen — what is cleared here is this page's
    // memory of what IT admitted.)
    const back = CHAT.slice(CHAT.indexOf("const onBack = useCallback("), CHAT.indexOf("const onOpenSession ="));
    expect(back).toContain("setWaitingSeeds([]);");
    expect(back).toContain("setAdmitAhead(null);");
    expect(back).toContain("leader.forget();");
    // …and OPENING ANOTHER SESSION from a queued chat forgets it too: `leaderId`
    // reads `leader.peek()` first, so a leader left behind would keep drawing
    // the previous chat's waiting rows under the new transcript (Bugbot).
    const open = CHAT.slice(CHAT.indexOf("const onOpenSession = useCallback("));
    const openBody = open.slice(0, open.indexOf("void controller.openSession(sessionId);"));
    expect(openBody).toContain("leader.forget();");
  });
});

describe("opening a waiting NEW chat from somewhere else", () => {
  const TASKS_LIB = readFileSync(join(HERE, "../../../shell/tasks-lib.ts"), "utf8");
  const STORE = readFileSync(join(HERE, "../params/store.ts"), "utf8");

  it("is a URL param, because a chat that has never run has no session to name", () => {
    // Nothing of it has run, so there is no transcript and no `session_id`. What
    // it HAS is the entry its first message is, which is also what the server
    // groups the whole conversation under (`pending:<leader id>`).
    expect(STORE).toContain('"queued",');
    const keys = STORE.slice(STORE.indexOf("export const CHAT_PARAM_KEYS = ["));
    expect(keys.indexOf('"queued",')).toBeLessThan(keys.indexOf("] as const;"));
  });

  it("becomes this pane's queue LEADER, which is what draws everything else", () => {
    // From there it is the road a chat that queued its own first message already
    // walks: `waitingFor` finds its rows, the `pending:<id>` task row gives the
    // header its number, and `adoptSession` swaps in the real transcript the
    // moment the leader runs.
    // …and ONLY under the flag: a `?queued=` link made while the queue was on
    // must not name a leader on a chat running with the queue off (flag-off
    // audit, 2026-09-12).
    expect(CHAT).toContain('const queuedParam = queueOn ? params.get(QUEUED_PARAM) || "" : "";');
    expect(CHAT).toContain('leader.remember("", queuedParam);');
    // REMEMBERED IN AN EFFECT, READ DIRECTLY FOR THE RENDER: `leader` is a ref,
    // so a render that wrote it would answer differently depending on how many
    // times React ran it — and the first paint needs the id before any effect.
    expect(CHAT).toContain(
      'const leaderId = leader.peek() || (state.sessionId ? "" : queuedParam);',
    );
    // …and it is a CONVERSATION, not the landing.
    expect(CHAT).toContain("params.get(QUEUED_PARAM) ||");
    // A SESSION OUTRANKS IT, always — the effect declines and the render agrees.
    expect(CHAT).toContain("if (!queuedParam || state.sessionId) return;");
  });

  it("is where a queued task row POINTS — and only a queued one", () => {
    // `taskHref` used to answer null for a task with no session, so a chat a
    // reader had typed into minutes before could not be opened from anywhere.
    expect(TASKS_LIB).toContain('if (task.status !== "queued") return null;');
    expect(TASKS_LIB).toContain('const queued = pendingEntryId(task.key || "");');
    expect(TASKS_LIB).toContain('if (queued && where) return chatUrl(where, "", queued);');
    // NOT every `pending:` key: an UPCOMING one-off is keyed that way too, and
    // its row press opens the EDIT FORM — `activate`'s thread arm runs first, so
    // widening this would have taken the form away from every scheduled message.
    const fn = TASKS_LIB.slice(TASKS_LIB.indexOf("export function taskHref("));
    expect(fn.indexOf('task.status !== "queued"')).toBeLessThan(fn.indexOf("pendingEntryId("));
  });
});
