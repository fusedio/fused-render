// The landing page's Recent list, subscribed. `protocol/sessions.ts` owns the
// reads (`GET /api/tasks` plus the `/api/tasks/changes` long-poll, T:18339
// watchRecent / T:18390 loadRecent); this is the React seam, and the one place
// the whole listing is narrowed to the pane the list is in.
//
// `null` is the SKELETON state and not "empty": `subscribeTasks` fires `null`
// first, then a row array for every read, so a re-read over a drawn list
// repaints in place rather than blinking its rows into placeholder bars
// (T:18411).
import { useEffect, useMemo, useRef, useState } from "react";
import type { Task } from "@platform/lib/api";
import { sortForList } from "@shell/tasks-lib";
import { subscribeTasks } from "../protocol/sessions";
import { taskInPane } from "./list-rows";

/**
 * The subscription, injectable — and injectable rather than module-mocked for
 * the reason `useSchedule`'s three endpoint calls are: `bun test` runs every
 * suite in ONE process, so a `mock.module("../protocol/sessions", …)` replaces
 * that module for every suite loaded AFTER it. (An ESM namespace object is also
 * frozen, so the patch-and-restore road is not open here at all.)
 */
export type SubscribeTasks = typeof subscribeTasks;

export function useRecentTasks(
  /**
   * The template folder holding `agent.py` — read ONLY as "is this landing
   * showing its lists at all". `ClaudeChat` hands `null` on the way into a
   * chat, which is what takes the long-poll down for a view that has no lists
   * (T:18339); the rows themselves come from `/api/tasks`, which knows nothing
   * about templates.
   */
  agentDir: string | null,
  file: string | null,
  subscribe: SubscribeTasks = subscribeTasks,
  /**
   * T's `leftLive` (T:13066-13071, P4-21). The two extra looks after landing
   * exist to cover the CLI's first transcript write for a chat left MID-TURN;
   * a cold landing has no such write to race, so it pays for neither. Read at
   * SUBSCRIBE time — which is the moment the landing is arrived at, and the
   * only moment the answer is about.
   */
  coverWrite = false,
): Task[] | null {
  const [rows, setRows] = useState<Task[] | null>(null);
  /** Has this list ever had real rows in it? The skeleton is only honest before
   *  the first answer of the page's life; after that a `null` means "reading
   *  again", which is not the same news. */
  const painted = useRef(false);
  const coverWriteRef = useRef(coverWrite);
  coverWriteRef.current = coverWrite;
  useEffect(() => {
    // ENTERING A CHAT DOES NOT EMPTY THE LIST. T clears `#recentlist`'s markup
    // on the way back but deliberately does NOT reset the counts: "both reads
    // are about to run again and land within a few hundred ms, and clearing
    // them first would take the tab bar off screen and put it back for the trip
    // — a stale count for a moment is quieter than a section that blinks. Boot
    // is the only place the 'not read yet' state is real." (T:13049-13053)
    //
    // So a torn-down subscription leaves the rows exactly where they were, and
    // the tab bar over them does not flash out and back for the round trip.
    if (!agentDir) return;
    // Read through a ref, so a `coverWrite` that flips while the landing is up
    // (the chat it described has since ended) cannot re-subscribe: the answer
    // is about the ARRIVAL, and the arrival has happened.
    return subscribe(
      file,
      (next) => {
        // THE SKELETON STANDS IN FOR ROWS WE DO NOT HAVE — never for rows that
        // are already up (T:18408-18411). `subscribeTasks` opens every
        // subscription with `null`, so without this a target change (or the
        // re-subscribe on the way back to the landing) blinked drawn rows into
        // placeholder bars, which "would make every retry look like the list lost
        // the chats it is about to reprint".
        if (next === null) {
          if (!painted.current) setRows(null);
          return;
        }
        painted.current = true;
        setRows(next);
      },
      undefined,
      coverWriteRef.current,
    );
    // `subscribe` is deliberately NOT a dependency: a caller that passes a fresh
    // closure every render would re-subscribe on every render, and the identity
    // of the transport is not a fact about the target.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentDir, file]);
  /**
   * …AND THE NARROWING IS HERE, not in the subscription (protocol/sessions.ts's
   * header says why): `/api/tasks` is the whole machine's listing, and this list
   * is about one pane. A folder pane keeps the tasks whose project IS it, a file
   * pane the tasks whose target IS it — one test either way, `taskInPane`.
   *
   * AND THEN THE TASKS PAGE'S OWN ORDER, `sortForList` (Akshil, 2026-09-14:
   * "sorting must equal the Tasks list view"). The rows ARE the Tasks page's
   * rows now, so the answer to "what is at the top of this list" has to be the
   * Tasks page's answer as well — status lanes first, a lane's drafts at its
   * head, time inside that — or the same conversations read as two different
   * lists on two surfaces. `/api/tasks` sends one flat `last_active` order,
   * which is the input that function takes, not a second opinion about it: the
   * server's order still breaks every tie the lanes leave open.
   */
  return useMemo(
    () =>
      rows === null
        ? null
        : // `Date.now()` is read INSIDE the memo, not taken as a prop: the lanes
          // it decides are "is this scheduled for later" (tasks-lib.groupByColumn),
          // and the only moment that question is about is the render doing the
          // asking. A clock in the deps would re-sort the list on every tick.
          sortForList(rows.filter((t) => taskInPane(t, file)), Date.now()),
    [rows, file],
  );
}

/**
 * THE TASK BEHIND THE CONVERSATION ON SCREEN — one row out of the same listing,
 * found by session id, for the chat's own header (`ui/Topbar.tsx`, Akshil
 * 2026-09-14: the in-session header is the task side peek's identity block).
 *
 * The SAME transport as the list above and deliberately not a second one: a
 * chat draws no lists, so `useRecentTasks` is asleep while one is open and this
 * takes the subscription over for exactly as long — the landing's list and the
 * chat's header are never both subscribed, and neither is ever subscribed
 * twice. Live rather than a one-shot read because the header wears the task's
 * STATUS: the ring has to hollow when the run ends, and a read taken on the way
 * in cannot know that.
 *
 * NOT NARROWED TO THE PANE, which is the one thing it does differently: the
 * question here is "which row is this session", and the answer is a single row
 * whose own identity is the test. A chat reached by a `session_id` param that
 * belongs to another file would otherwise be filtered out of its own header.
 *
 * THREE ANSWERS AND NOT TWO (Akshil, 2026-09-14). "We have not read the
 * listing" and "we read it and this session is not in it" used to be one `null`
 * and the header printed the ✻ Claude fallback for both — so every deep link
 * into a chat wore the wrong identity for as long as `/api/tasks` took to
 * answer with 800-odd rows, and then swapped. They are different news:
 *
 *   * a `task` — the row, from the listing or from the SEED the press left
 *     behind (`seedSessionTask`), which is the same answer a whole round trip
 *     earlier;
 *   * `pending` — there is a session, nobody has told us anything about it yet,
 *     and the header draws a skeleton rather than a name that may be wrong;
 *   * neither — the listing HAS answered and has no row for this session, which
 *     is a real state and the only one the ✻ Claude line is honest about: a
 *     chat seconds old, whose transcript the server's watcher has not seen.
 */
export interface SessionIdentity {
  /** The listing's row for this session, or the seeded one, or null. */
  task: Task | null;
  /** A session whose row NOBODY HAS ANSWERED FOR YET. Never true without a
   *  session id, and never true once a listing has been read. */
  pending: boolean;
}

/**
 * THE ROW A PRESS ALREADY HAD IN ITS HAND, kept for the header that is about to
 * need it (Akshil, 2026-09-14).
 *
 * The Recent list opens a chat IN PLACE — no navigation, no reload — and the
 * row it opened is a `Task` the list was already drawing. `useSessionTask` then
 * subscribed from scratch and the header sat on its fallback for the length of
 * one `/api/tasks` read, showing the OLD identity of a conversation the reader
 * had just named. So the press leaves the row here and the hook reads it on its
 * very first render.
 *
 * A MAP AND NOT A SINGLE SLOT, because a reader walks in and out of several
 * chats in one page life and Back is free — the seed for the chat they are
 * returning to must still be there. Capped, oldest-out, because nothing ever
 * invalidates these: the listing supersedes each one within a second of the
 * press, so the cap is about memory and not about staleness.
 *
 * MODULE STATE, like `lists-visibility`'s remembered tab and for the same
 * reason: the landing unmounts on the way into the chat, so there is no
 * component alive on both sides of the gesture to hold it.
 */
const SEED_CAP = 32;
/**
 * ON `globalThis`, which is the one unusual thing here and is worth the line:
 * this map is written by the LIST and read by the HEADER, and the two are only
 * the same store while they are the same module instance. A dev server's HMR
 * re-evaluates a module and hands the new copy an empty map; a bundler that
 * ever splits this file between the landing's chunk and the chat's does the
 * same thing permanently. Either way the seed is silently lost and the header
 * goes back to waiting out a listing read — a bug that cannot be seen in a test,
 * because a test process has exactly one copy. The key is namespaced and the
 * value is a plain Map.
 */
const SEED_STORE = "__fusedRenderChatSessionSeeds";
const seeds: Map<string, Task> =
  ((globalThis as Record<string, unknown>)[SEED_STORE] as Map<string, Task>) ??
  (((globalThis as Record<string, unknown>)[SEED_STORE] = new Map<string, Task>()) as Map<
    string,
    Task
  >);

export function seedSessionTask(task: Task): void {
  const id = task.session_id || task.key;
  if (!id) return;
  // Re-inserted rather than updated in place, so a seed just used is also the
  // youngest — the cap then drops the chats nobody has been near.
  seeds.delete(id);
  seeds.set(id, task);
  while (seeds.size > SEED_CAP) {
    const oldest = seeds.keys().next();
    if (oldest.done) break;
    seeds.delete(oldest.value);
  }
  stashSeed(id, task);
}

/** The seeded row for a session, or null. */
export function sessionSeed(sessionId: string | null): Task | null {
  if (!sessionId) return null;
  return seeds.get(sessionId) ?? stashedSeed(sessionId);
}

/** Tests only — module state outlives every renderer in a `bun test` process,
 *  exactly as `lists-visibility.resetRememberedTab` does. */
export function resetSessionSeeds(): void {
  seeds.clear();
  try {
    sessionStorage.removeItem(SEED_STASH);
  } catch {
    // No storage — there was nothing stashed to clear.
  }
}

/**
 * …AND THE SAME ANSWER ACROSS A NAVIGATION (Akshil QA, 2026-09-14).
 *
 * MOST RECENT-LIST PRESSES LEAVE THE PAGE. A row is opened IN PLACE only when
 * it is about the pane's own file; on a FOLDER pane — which is where the list
 * is usually read — every chat is about some file inside it, so every row
 * carries a real `href` and the press is a navigation (`Lists.pressFor`). The
 * module map above cannot survive that, and a header that has to wait out an
 * 800-row listing is exactly the bug the seed exists to fix.
 *
 * `sessionStorage` is the right lifetime and the one this app already uses for
 * a handoff across a navigation (`ui/sched-draft.ts` carries the composer's
 * draft the same way): per TAB, gone with it, never shared with another window.
 *
 * ONE ROW, not a map: a navigation carries exactly one press, and the trip is
 * over by the time the next one happens. TTL'd, because this is the only copy
 * that can outlive the errand it was written for — a reload of that same chat
 * URL an hour later must not print a title the listing has since changed. The
 * listing overwrites it within a second either way.
 *
 * EVERY ACCESS IS GUARDED: storage can be refused outright (a private window,
 * blocked site data), and a seed that cannot be written costs a moment of
 * skeleton — never the navigation.
 */
const SEED_STASH = "fused:chatseed";
const SEED_TTL_MS = 60_000;

function stashSeed(id: string, task: Task): void {
  try {
    sessionStorage.setItem(SEED_STASH, JSON.stringify({ id, task, at: Date.now() }));
  } catch {
    // Storage denied — the seed just does not survive the trip.
  }
}

function stashedSeed(id: string): Task | null {
  try {
    const raw = sessionStorage.getItem(SEED_STASH);
    if (!raw) return null;
    const row = JSON.parse(raw) as { id?: string; task?: Task; at?: number };
    if (!row || row.id !== id || !row.task) return null;
    if (!(typeof row.at === "number") || Date.now() - row.at > SEED_TTL_MS) return null;
    return row.task;
  } catch {
    // Unreadable or not ours — the same answer as "nothing stashed".
    return null;
  }
}

export function useSessionTask(
  sessionId: string | null,
  file: string | null,
  subscribe: SubscribeTasks = subscribeTasks,
): SessionIdentity {
  const [rows, setRows] = useState<Task[] | null>(null);
  /**
   * THE SEED, READ ON THE FIRST RENDER THIS SESSION ID IS SEEN — not in an
   * effect, and not inside the memo below (Akshil QA, 2026-09-14: a seeded
   * header still wore the skeleton for ~260 ms).
   *
   * The press writes the row and THEN opens the session, so by the time this
   * hook renders with the new id the answer is already sitting in the map: the
   * only way to be late with it is to read it late.
   *
   * A REF CACHE AND NOT STATE, which is the one thing here worth defending.
   * Adjusting STATE during render is React's documented shape for "a prop
   * changed and some state derives from it", but it re-runs the render and
   * THROWS THE FIRST PASS AWAY — so the very paint this fix is about would
   * still be computed without the seed. Nothing outside this component can
   * observe the write (it is a pure function of `sessionId`, recomputed rather
   * than accumulated), so the cache is a ref and the first render with a new id
   * is already the right one.
   *
   * HELD, rather than re-read every render: the map is capped and evicts, and
   * an identity that vanished mid-conversation because somebody opened 32 other
   * chats would be a worse bug than the one this fixes.
   */
  const seedFor = useRef(sessionId);
  const seedRef = useRef<Task | null>(sessionSeed(sessionId));
  if (seedFor.current !== sessionId) {
    seedFor.current = sessionId;
    seedRef.current = sessionSeed(sessionId);
  }
  const seed = seedRef.current;
  /** THE TRANSPORT THROUGH A REF (the list above spends a lint exemption for
   *  the same fact): the identity of the subscription function is not a fact
   *  about the session, so a caller that passes a fresh closure — every test
   *  does — must not re-subscribe this hook on every render. Read at subscribe
   *  time, which is the only moment it is used. */
  const subscribeRef = useRef(subscribe);
  subscribeRef.current = subscribe;
  /** The list hook above keeps the same flag for the same reason: every
   *  `subscribeTasks` opens with a `null` SKELETON (T:18408-18411), and once a
   *  listing has been held that `null` means "reading again", not "no rows".
   *  Re-entering a chat re-subscribes over a listing we already have, and
   *  writing the skeleton through dropped the header to its `✻ Claude` fallback
   *  for the length of the `/api/tasks` round trip — a flash of the wrong
   *  identity on a task whose name we were already printing. */
  const painted = useRef(false);
  useEffect(() => {
    if (!sessionId) return;
    return subscribeRef.current(file, (next) => {
      if (next === null) {
        if (!painted.current) setRows(null);
        return;
      }
      painted.current = true;
      setRows(next);
    });
  }, [sessionId, file]);
  return useMemo(() => {
    if (!sessionId) return NO_IDENTITY;
    const seeded = seed;
    // NOT READ YET. A seed answers it outright — the press handed us the row —
    // and without one the header is owed a skeleton, never the fallback: the
    // fallback is a CLAIM (this chat has no task row) and we do not know that.
    if (rows === null) {
      return seeded ? { task: seeded, pending: false } : PENDING;
    }
    // `key` IS the session id for a task that has a conversation (the listing's
    // own spelling — `ui/Kebab.tsx`'s `useTaskId` asks the same question of the
    // same field); `session_id` is asked too, so a server that ever keys a row
    // by something else still answers.
    const found =
      rows.find((t) => t.key === sessionId || t.session_id === sessionId) ?? null;
    // A LISTING THAT DROPPED THE SEEDED ROW KEEPS THE SEED. The row was real a
    // moment ago; an identity that blinks out mid-conversation is worse than one
    // that is a poll behind, and the next read puts it right either way.
    return { task: found ?? seeded, pending: false };
  }, [rows, sessionId, seed]);
}

/** The two answers that carry no row, as constants: a new object every render
 *  would make this hook's result a fresh dependency on every paint. */
const NO_IDENTITY: SessionIdentity = { task: null, pending: false };
const PENDING: SessionIdentity = { task: null, pending: true };
