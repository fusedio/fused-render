// WHICH QUEUED MESSAGE A FOLLOW-UP BELONGS TO, while the chat holding both has
// no session at all (the project queue, prefs `queue.enabled`).
//
// The shape of the problem, from the reader's side: they open a new chat, type,
// and the folder is busy — so the words become a pending entry and the bubble
// stays up with a chip under it. Then they type a second line. That line has
// nothing to address. There is no Claude session (nothing has run), and a send
// with an empty `session_id` is what "start a brand-new task" means everywhere
// else in this app — so the second message would open a SECOND task in the same
// folder, and the conversation on screen would silently be two.
//
// THE FIX IS A NAME, and the server already has the machinery: `follow_of` is
// the one-off twin of `template_id`. The second admit names the first entry, the
// server groups them under `pending:<leader id>`, orders them, and resolves the
// follower's session from whatever the leader's run opens (design.md,
// "Follow-ups into a queued NEW chat"). All the client owes it is the id.
//
// SO THIS IS THE MEMORY OF ONE ID, AND ITS TWO EDGES — remembered on the first
// admission a session-less chat gets back `run: false` from, forgotten the
// moment that chat HAS a session. It lives here, pure and beside the poller
// whose facts decide the second edge, because "which task did my second message
// join?" is not a question a render test of the view would ever ask.
import { useCallback, useEffect, useMemo, useRef } from "react";

/**
 * The leader after an admission came back queued.
 *
 * FIRST ONE WINS, and it must: entries two and three are both follow-ups OF THE
 * FIRST, not a chain where each names the one before it. A chain would make the
 * grouping depend on every link surviving — cancel the middle message and the
 * third one is orphaned from a leader that is still perfectly alive — while one
 * name points every follower at the entry the task is actually named after
 * (`pending:<leader id>`).
 *
 * AND A CHAT WITH A SESSION NEVER TAKES ONE. There the queued entry already
 * addresses a conversation by id; a leader would be a second answer to a
 * question that has one, and the next send would name an entry instead of the
 * session it belongs to.
 */
export function leaderAfterAdmit(leader: string, sessionId: string, entryId: string): string {
  if (sessionId) return "";
  if (leader) return leader;
  return entryId || "";
}

/**
 * What goes on the next admit body — the leader, or "" for "nothing to name".
 *
 * THE SESSION ID OUTRANKS IT, asked here rather than trusted from the effect
 * that clears it. A send reads the session at dispatch time (the controller's
 * own state, which is fresher than any render), and a `follow_of` sent beside a
 * real `session_id` would ask the server to group an addressed message under a
 * task it is no longer part of. The clearing effect is the tidy-up; this is the
 * guarantee.
 */
export function leaderFollowOf(leader: string, sessionId: string): string {
  return sessionId ? "" : leader;
}

/**
 * The leader once the chat knows its session — dropped.
 *
 * The leader existed only because there was no session to name. The moment one
 * arrives (the leader's run opened it and the poll reported its
 * `claude_session_id`, or this chat acquired one the ordinary way) every later
 * message addresses THAT, and holding the entry id would keep joining new sends
 * to a task the conversation has already outgrown.
 */
export function leaderAfterSession(leader: string, sessionId: string): string {
  return sessionId ? "" : leader;
}

/**
 * THE SESSION THE LEADER'S RUN OPENED — "" while there is nothing to adopt.
 *
 * THE OTHER END OF THE LEADER, and the one the first round left unbuilt. A
 * leader is taken because there is no session; it is dropped when one arrives
 * (`leaderAfterSession`) — and NOTHING was handing one over when the leader was
 * run BY THE SCHEDULER rather than by this page. The chat's own roads to a
 * session id all begin with this page starting something: `sendMessage` reports
 * one on its first poll, `openSession` is told one, the schedule watcher's
 * attach learns one from `resumeRun`'s probe. A queued leader starts nothing
 * here, and the watcher's attach cannot stand in for it: it needs a LIVE run to
 * probe, so a leader that ran and finished while this tab was in the background
 * leaves nothing to attach to — and the FOLLOWER's run is written off outright
 * (`scheduledRunIsOurs` only adopts entries naming no session on a session-less
 * screen, and a follower's session is resolved at claim time).
 *
 * So the chat stayed a chat with no session: every later message admitted as
 * another follower of an entry that had long since run, the composer never
 * returning to the ordinary send path, and a transcript showing none of what
 * the leader actually said.
 *
 * THE ENTRY IS THE RECORD, and it outlives the run: `claude_session_id` is on
 * it the moment the scheduler's claim opens a session (design.md,
 * "Follow-ups into a queued NEW chat"). This is the whole rule for reading it.
 *
 *   * a chat that HAS a session adopts nothing — it already has the answer, and
 *     opening another conversation over the reader's would be the one mistake
 *     worth being careful about here;
 *   * no leader, nothing polled, or a leader that has not run: "".
 */
export function leaderSession(
  sessions: ReadonlyMap<string, string> | null | undefined,
  leader: string,
  sessionId: string,
): string {
  if (sessionId || !leader || !sessions) return "";
  return sessions.get(leader) || "";
}

export interface QueuedLeader {
  /** `follow_of` for an admit made with this session id ("" on a chat that has
   *  none) — empty when there is nothing to name. */
  followOf(sessionId: string): string;
  /** An admission just came back `run: false` for a send made with this session
   *  id. Remembers the entry as the leader when it is the first one and the
   *  chat has no session; a no-op otherwise. */
  remember(sessionId: string, entryId: string): void;
  /** THE CONVERSATION THIS LEADER BELONGED TO IS GONE — Back, or any other
   *  gesture that replaces the transcript. A leader is a memory of what the
   *  messages ON SCREEN joined; carried into the next conversation it would file
   *  a fresh chat's first line under a task it has nothing to do with, and —
   *  since the chat ADOPTS the session that leader's run opens — drag the reader
   *  into a conversation they had just left. */
  forget(): void;
  /** Test seam, and the one thing a render cannot see. */
  peek(): string;
}

/**
 * The same rule, as the chat holds it: a ref, and a stable handle over it.
 *
 * A REF RATHER THAN STATE, for the reason every other member of the send window
 * is one (`sendBusy`, `sendHolder`): `dispatchSend` reads this in the tick it
 * was called in, and a value React has not re-rendered with yet is a value that
 * would send the second message as a fresh task. Nothing on screen draws it, so
 * there is nothing a re-render would fix.
 *
 * STABLE IDENTITY, so `dispatchSend`'s dependency list does not rebuild the send
 * callback on every session change — the composer reads that callback through a
 * prop, and a new one mid-window is a new closure over a half-spent send.
 */
export function useQueuedLeader(sessionId: string): QueuedLeader {
  const leader = useRef("");
  // THE CLEAR IS AN EFFECT, not a render-time write: a render that edits the
  // memory it just read answers differently depending on how many times React
  // ran it. `followOf` refuses on the live session id anyway, so the effect is
  // never the thing standing between a session and a correct body.
  useEffect(() => {
    leader.current = leaderAfterSession(leader.current, sessionId);
  }, [sessionId]);
  const followOf = useCallback((sid: string) => leaderFollowOf(leader.current, sid), []);
  const remember = useCallback((sid: string, entryId: string) => {
    leader.current = leaderAfterAdmit(leader.current, sid, entryId);
  }, []);
  const forget = useCallback(() => {
    leader.current = "";
  }, []);
  const peek = useCallback(() => leader.current, []);
  return useMemo(
    () => ({ followOf, remember, forget, peek }),
    [followOf, remember, forget, peek],
  );
}
