// THE LANDING LISTS THE CHATS THAT HAVE NOT RUN YET (`sched/waiting-chats`).
//
// "Recent chats" is the transcripts in this folder; a chat whose first message
// was queued has written none, so the landing showed no sign of a conversation
// the reader had typed into minutes before. Its row is on `/api/tasks` instead,
// keyed `pending:<leader id>`, and it is folded into the same list rather than
// given a section of its own.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  CHAT_ORIGIN,
  mergeWaitingChats,
  pacedReader,
  waitingChatRows,
  WAITING_REFRESH_MS,
  WAITING_TASK_STATUSES,
} from "./waiting-chats";
import type { WaitingTask } from "./waiting-chats";
import type { SessionRow } from "../protocol/types";

const HERE = new URL(".", import.meta.url).pathname;
const CHAT = readFileSync(join(HERE, "../ClaudeChat.tsx"), "utf8");
const ROW = readFileSync(join(HERE, "../ui/RecentRow.tsx"), "utf8");
const HOME_CSS = readFileSync(join(HERE, "../styles/home.css"), "utf8");
const SRC = readFileSync(join(HERE, "waiting-chats.ts"), "utf8");

const task = (over: Partial<WaitingTask> = {}): WaitingTask => ({
  key: "pending:e1",
  entry_id: "e1",
  entry_origin: "chat",
  status: "queued",
  project: "/w/app",
  target: "/w/app",
  title: "Pull today's news",
  task_id: "TASK-057",
  last_active: 200,
  ...over,
});

const session = (over: Partial<SessionRow> = {}): SessionRow => ({
  id: "sess-1",
  preview: "ran already",
  created_at: 100,
  last_used: 100,
  cwd: "/w/app",
  pane: "",
  running: false,
  ...over,
});

describe("which tasks become rows", () => {
  it("takes the `pending:` keys of this folder, and shapes them as list rows", () => {
    const rows = waitingChatRows([task()], "/w/app");
    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe("pending:e1");
    expect(rows[0].queuedEntry).toBe("e1");
    expect(rows[0].taskId).toBe("TASK-057");
    expect(rows[0].preview).toBe("Pull today's news");
    // NEVER running: a waiting chat is the opposite of a running one.
    expect(rows[0].running).toBe(false);
    // …and the clock the whole list sorts on.
    expect(rows[0].last_used).toBe(200);
  });

  it("opens by the ENTRY, which is the only name it has", () => {
    expect(waitingChatRows([task()], "/w/app")[0].href).toBe(
      "/explorer/view/w/app?_side=claude&session_id=&queued=e1",
    );
  });

  it("is `queued` or `upcoming` and nothing else", () => {
    expect([...WAITING_TASK_STATUSES].sort()).toEqual(["queued", "upcoming"]);
    expect(waitingChatRows([task({ status: "upcoming" })], "/w/app")).toHaveLength(1);
    for (const status of ["in_progress", "done", "failed", "archived", "blocked"]) {
      expect(waitingChatRows([task({ status } as Partial<WaitingTask>)], "/w/app")).toHaveLength(0);
    }
  });

  it("ignores a task that HAS run — the ordinary list already has it", () => {
    // The store rekeys a task onto its Claude session the moment its run opens
    // one, so the `pending:` prefix IS the test for "no transcript yet".
    expect(waitingChatRows([task({ key: "sess-9", entry_id: "" })], "/w/app")).toHaveLength(0);
    // …and not even a row still naming its entry: the key is what says whether
    // there is a transcript.
    expect(waitingChatRows([task({ key: "sess-9" })], "/w/app")).toHaveLength(0);
  });

  it("gates `upcoming` on `entry_origin` — a thing due Thursday is not a chat", () => {
    // A calendar message, a New-task entry and a repeat's next occurrence are all
    // waiting `pending:` rows too. Not one of them is a conversation somebody
    // typed, and listing on the key alone would have put every future scheduled
    // job into a list whose whole promise is "what has happened here".
    expect(CHAT_ORIGIN).toBe("chat");
    const later = (origin?: string) =>
      waitingChatRows([task({ status: "upcoming", entry_origin: origin })], "/w/app");
    expect(later("")).toHaveLength(0);
    expect(later(undefined)).toHaveLength(0);
    expect(later("schedule")).toHaveLength(0);
    expect(later("chat")).toHaveLength(1);
  });

  it("takes EVERY `queued` row, whatever composed it", () => {
    // `queued` is not "due later": it says the message would be running this
    // second if the folder were free. A New-task modal's message that lands in a
    // busy folder is a conversation the reader has just started and will come
    // back to in a minute, and leaving it off the list because a form rather than
    // a composer typed it hid the very rows the queue exists to explain
    // (Akshil, 2026-09-12).
    for (const origin of ["", "schedule", "chat", undefined]) {
      expect(waitingChatRows([task({ status: "queued", entry_origin: origin })], "/w/app"))
        .toHaveLength(1);
    }
  });

  it("takes the entry the SERVER named, and falls back to the key", () => {
    expect(waitingChatRows([task({ key: "pending:e1", entry_id: "e9" })], "/w/app")[0].queuedEntry)
      .toBe("e9");
    // An older server sends no `entry_id`; the key still carries it.
    expect(waitingChatRows([task({ entry_id: undefined })], "/w/app")[0].queuedEntry).toBe("e1");
  });

  it("ignores another folder's, and keeps this folder's when the target is a FILE", () => {
    expect(waitingChatRows([task({ project: "/w/other", target: "/w/other" })], "/w/app")).toHaveLength(0);
    // A task on `~/w/app` belongs on the landing of `~/w/app/notes.md`.
    expect(waitingChatRows([task()], "/w/app/notes.md")).toHaveLength(1);
    // …and its `pane` is the file it is about, blank when it is about the folder.
    expect(waitingChatRows([task()], "/w/app")[0].pane).toBe("");
    expect(
      waitingChatRows([task({ target: "/w/app/notes.md" })], "/w/app")[0].pane,
    ).toBe("/w/app/notes.md");
  });

  it("has nothing to say with no target", () => {
    expect(waitingChatRows([task()], null)).toEqual([]);
    expect(waitingChatRows(null, "/w/app")).toEqual([]);
  });
});

describe("folding them into Recent chats", () => {
  it("is ONE list, sorted by last_active, newest first", () => {
    const merged = mergeWaitingChats(
      [session({ id: "old", last_used: 50 }), session({ id: "new", last_used: 300 })],
      waitingChatRows([task()], "/w/app"),
    );
    expect(merged!.map((r) => r.id)).toEqual(["new", "pending:e1", "old"]);
  });

  it("keeps `null` — that is the sessions read's own skeleton state", () => {
    // "the sessions read has not answered" stays true whatever the tasks read
    // said, and the waiting rows are an addition to that list rather than a
    // reason to stop waiting for it.
    expect(mergeWaitingChats(null, waitingChatRows([task()], "/w/app"))).toBe(null);
  });

  it("returns the list untouched when nothing is waiting", () => {
    const rows = [session()];
    expect(mergeWaitingChats(rows, [])).toBe(rows);
  });

  it("never draws one conversation twice", () => {
    // The window where a leader's run has opened a session, the transcript
    // exists, and the tasks read is one lap stale.
    const merged = mergeWaitingChats(
      [session({ id: "pending:e1", last_used: 300 })],
      waitingChatRows([task()], "/w/app"),
    );
    expect(merged).toHaveLength(1);
  });
});

describe("one conversation, one row, on either of its two names", () => {
  it("drops a waiting row whose LEADER'S SESSION is already in the list", () => {
    // The window this closes: the run opens a session, the transcript lands,
    // `sessions` lists it by that session id — and the tasks read still holds the
    // row under `pending:<entry>`. Two ids, one conversation, two rows until the
    // next tasks read (🔴 review 2026-09-12). The transcript row wins.
    const waiting = waitingChatRows([task({ session_id: "sess-1" })], "/w/app");
    expect(waiting[0].leaderSession).toBe("sess-1");
    expect(mergeWaitingChats([session({ id: "sess-1" })], waiting)).toHaveLength(1);
    // …and with no such row in the list, it is still drawn.
    expect(mergeWaitingChats([session({ id: "sess-other" })], waiting)).toHaveLength(2);
    // A leader with no session yet is never mistaken for one: "" matches nothing.
    const fresh = waitingChatRows([task()], "/w/app");
    expect(fresh[0].leaderSession).toBe("");
    expect(mergeWaitingChats([session({ id: "" })], fresh)).toHaveLength(2);
  });

  it("re-reads the tasks on the RECENT LIST'S own tick", () => {
    // `tasks-changed` is rung by an admission and by a run's start; a leader's
    // transcript APPEARING is news only the sessions watch hears. Without this
    // the pair of lists disagreed until something unrelated poked them.
    expect(SRC).toContain("tick?: unknown,");
    expect(SRC).toContain("pokeRef.current();");
    expect(SRC).toContain("}, [tick]);");
    // …and the first tick is skipped, or a mount reads twice for nothing.
    expect(SRC).toContain("const firstTick = useRef(true);");
    expect(CHAT).toContain("    recentSessions,");
  });
});

describe("what the row says and where it goes", () => {
  it("says `waiting` where a live row says `running`, and no time", () => {
    // The state is the more useful answer to "when", and the two side by side
    // spend the row's last inch saying one thing twice.
    expect(ROW).toContain('<span className="c-row-wait">waiting</span>');
    expect(ROW).toContain("{session.running || waiting ? null : (");
    expect(HOME_CSS).toContain(".c-row-wait {");
    expect(HOME_CSS).toContain("color: var(--status-queued);");
  });

  it("wears the DASHED ring rather than the filled dot", () => {
    const css = HOME_CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    expect(css).toContain(".c-chat-row.is-waiting .c-dot::after {");
    const ring = css.slice(
      css.indexOf(".c-chat-row.is-waiting .c-dot::after {"),
      css.indexOf("}", css.indexOf(".c-chat-row.is-waiting .c-dot::after {")),
    );
    // Dashed, in the running yellow: one colour for waiting and running, and the
    // shape is what separates them (tokens.css, schedule.css).
    expect(ring).toContain("border: 2px dashed var(--status-queued)");
    expect(ring).toContain("border-radius: 999px");
  });

  it("opens by navigating to its own queued url — there is no session to swap in", () => {
    expect(ROW).toContain("const waiting = session.queuedEntry ||");
    expect(ROW).toContain("if (session.href) onNavigate?.(session.href);");
    // Before the two ordinary doors, because neither of them can open it.
    expect(ROW.indexOf("if (session.href) onNavigate")).toBeLessThan(
      ROW.indexOf("onOpen(session.id);"),
    );
  });

  it("is merged by the landing, and only there and only under the flag", () => {
    expect(CHAT).toContain("const waitingChats = useWaitingChats(");
    expect(CHAT).toContain("!inChat && queueOn ? file : null");
    expect(CHAT).toContain("mergeWaitingChats(recentSessions, waitingChats)");
  });
});

describe("one read for one event, and a floor under all of them", () => {
  // 🟡 review, 2026-09-12. Two things ask for a re-read and neither knew about
  // the other — the recent list's own tick and the `tasks-changed` announcement
  // — and one send rings BOTH within a few milliseconds. The landing globbed
  // every transcript on the machine twice, for one event, with no floor at all.
  it("is five seconds, the same floor the chat's own row read has", () => {
    expect(WAITING_REFRESH_MS).toBe(5000);
  });

  it("reads on the mount, and folds two pokes in one window into one read", async () => {
    let reads = 0;
    let clock = 1_000_000;
    const pacer = pacedReader(() => {
      reads += 1;
    }, () => clock, 20);

    // The mount's own read is never floored: nothing is on screen to be stale.
    pacer.poke(true);
    expect(reads).toBe(1);

    // The tick and the announcement, in the same breath. ONE read comes of it,
    // and it is deferred to the end of the floor rather than dropped.
    pacer.poke();
    pacer.poke();
    expect(reads).toBe(1);
    clock += 20;
    await new Promise((r) => setTimeout(r, 40));
    expect(reads).toBe(2);

    // …and a poke past the floor goes straight through.
    clock += 100;
    pacer.poke();
    expect(reads).toBe(3);
    pacer.stop();
  });

  it("drops a deferred read when the subscription goes", async () => {
    let reads = 0;
    let clock = 1_000_000;
    const pacer = pacedReader(() => {
      reads += 1;
    }, () => clock, 20);
    pacer.poke(true);
    pacer.poke();
    pacer.stop();
    await new Promise((r) => setTimeout(r, 40));
    expect(reads).toBe(1);
  });
});
