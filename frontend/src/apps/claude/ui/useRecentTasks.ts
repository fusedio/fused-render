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
 * `null` while there is no session, no target, or no row yet — a brand-new chat
 * has a session id seconds before `/api/tasks` has a row for it, and the header
 * falls back to what the chat has always printed until it lands.
 */
export function useSessionTask(
  sessionId: string | null,
  file: string | null,
  subscribe: SubscribeTasks = subscribeTasks,
): Task | null {
  const [rows, setRows] = useState<Task[] | null>(null);
  /** THE TRANSPORT THROUGH A REF (the list above spends a lint exemption for
   *  the same fact): the identity of the subscription function is not a fact
   *  about the session, so a caller that passes a fresh closure — every test
   *  does — must not re-subscribe this hook on every render. Read at subscribe
   *  time, which is the only moment it is used. */
  const subscribeRef = useRef(subscribe);
  subscribeRef.current = subscribe;
  useEffect(() => {
    if (!sessionId) return;
    return subscribeRef.current(file, (next) => setRows(next));
  }, [sessionId, file]);
  return useMemo(() => {
    if (!sessionId || rows === null) return null;
    // `key` IS the session id for a task that has a conversation (the listing's
    // own spelling — `ui/Kebab.tsx`'s `useTaskId` asks the same question of the
    // same field); `session_id` is asked too, so a server that ever keys a row
    // by something else still answers.
    return rows.find((t) => t.key === sessionId || t.session_id === sessionId) ?? null;
  }, [rows, sessionId]);
}
