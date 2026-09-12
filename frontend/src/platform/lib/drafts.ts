// DRAFTS — the half-written thing, kept on the server (design.md, "Where drafts
// live: server, one file").
//
// Two kinds share one store and one contract:
//
//   * a CHAT draft, keyed on the session the composer is in — or on `new:<file>`
//     while the chat has no session yet, because a brand-new conversation only
//     gets its id at the first send (design.md, "Chat draft key"). First send
//     creates the session AND deletes the draft, so nothing is ever re-keyed;
//   * a TASK draft, keyed on a uuid the CLIENT mints at the first keystroke in
//     the New task form. A task draft never has a session: the session arrives
//     at the first run like any scheduled entry (Akshil, 2026-09-11).
//
// WHY THE SERVER AND NOT localStorage: the List/Board badge needs `/api/tasks`
// (server-rendered) to join a draft onto its task row, and a draft should
// survive the browser profile, another window, and the packaged app vs. the dev
// server. The sessionStorage hop in `apps/claude/ui/sched-draft.ts` stays as it
// is — it carries the draft ACROSS a navigation, which is a different errand and
// a different lifetime (design.md, "Not in scope": retiring the hop).
//
// THE ONE RULE EVERY WRITE HERE OBEYS: it must never break what the user is
// doing. Typing is not blocked, a refusal is not shown, a network error is
// swallowed — the same contract `stashDraft` keeps with a storage refusal. A
// draft that failed to save costs the user a draft; a draft that threw inside a
// keystroke handler costs them the keystroke.
import { useCallback, useEffect, useRef } from "react";
import type { RecurrenceRule } from "./api";

/** ONE ATTACHMENT, as the schedule form stores it — `path`, `name`, `kind`, the
 *  identical three fields `sched-draft.ts`'s `SchedAttachment` carries and a
 *  stored entry's `attachments` row holds. Spelled again here rather than
 *  imported because `platform/` may not import `apps/` (scripts/check-boundaries)
 *  — and the wire shape is the server's anyway, not the chat's. */
export interface DraftAttachment {
  path: string;
  name: string;
  kind: "image" | "file";
}

/** One chat draft as `GET /api/drafts` returns it. */
export interface ChatDraft {
  text: string;
  attachments: DraftAttachment[];
  updated_at: number;
}

/**
 * The New task form, as a draft: every field the card can lose, and nothing
 * else. `when` / `repeat` / `new_task_each_run` are nullable because "the user
 * never said" is a real answer for all three and is not the same as the form's
 * default — the same distinction `timePicked` draws on the card itself.
 */
export interface TaskDraftForm {
  title: string;
  description: string;
  target: string;
  when: string | null;
  repeat: string | null;
  model: string;
  effort: string;
  permission: string;
  attachments: DraftAttachment[];
  new_task_each_run: boolean | null;
  /**
   * THE RULE BEHIND A "CUSTOM" REPEAT, because the preset key alone is not an
   * answer (Bugbot, PR #1118).
   *
   * Every other repeat choice IS its own data — "every day" needs nothing but
   * the word — but `repeat: "custom"` is a pointer at a rule the recurrence
   * dialog built, and a draft that stored the pointer and dropped the rule
   * reopened on a card that said Custom, held no rule, and refused Save with
   * nothing on screen explaining why (`saveEnabled`: a custom repeat needs its
   * rule). Stored as the object, pass-through on the server, so what comes back
   * is what the dialog produced.
   *
   * Null whenever the choice is not Custom — including a repeat that is switched
   * off entirely, exactly as `repeat` itself is.
   */
  custom_rule: RecurrenceRule | null;
}

export interface TaskDraft extends TaskDraftForm {
  created_at: number;
  updated_at: number;
}

/** `GET /api/drafts` — everything, both kinds, keyed. */
export interface DraftsSnapshot {
  chat: Record<string, ChatDraft>;
  task: Record<string, TaskDraft>;
}

const EMPTY: DraftsSnapshot = { chat: {}, task: {} };

/**
 * WHICH KEY THIS COMPOSER'S DRAFT LIVES UNDER (design.md, "Chat draft key").
 * The session id when the chat has one; otherwise `new:<file>` — the same file
 * the sessionStorage hop keys on, so the two halves describe one chat.
 *
 * WHAT `<file>` IS, written down once because four things now have to agree on
 * it (Akshil, 2026-09-11). It is the chat's OWN `file` prop — the path the
 * Claude pane is mounted on, which is the folder for a folder-scoped chat and
 * the document for a file-scoped one — verbatim and unnormalised, exactly as
 * `ClaudeChat` receives it. The other three quote that string rather than
 * building one of their own:
 *
 *   * `sched-draft.stashDraft(file, …)`, the sessionStorage hop, keys on it;
 *   * the Schedule hop's `?target=` param IS it (`sched-draft.schedulerUrl`
 *     writes `link.file` there), which is how the task form knows which chat
 *     draft its own first save supersedes (`from_chat_key`, below);
 *   * `schedule-lib.explorerUrl(target, "")` builds its path out of it, which
 *     is where a `new:<file>` draft ROW sends a reader — so the chat that opens
 *     is mounted on the same `file` and its composer seeds from the same key.
 *
 * Nothing here trims a trailing slash or expands a tilde, deliberately: a key
 * normalised in one of the four places and not the others is a draft that can
 * be written and never read back.
 */
export function chatDraftKey(sessionId: string | null, file: string | null): string {
  return sessionId || `${NEW_CHAT_PREFIX}${file ?? ""}`;
}

/** The marker a chat draft with no session yet wears — `fused_render/drafts.py`
 *  spells it `NEW_CHAT_PREFIX` for the same reason, and the two must agree. */
export const NEW_CHAT_PREFIX = "new:";

/** The file (or folder) a `new:<file>` key was opened on, or `""` for a key
 *  that is a session id. The twin of `drafts.new_chat_file` server-side, and it
 *  exists for the same one reason: a reader who has to get BACK to that chat
 *  needs the path, and every caller re-deriving the prefix arithmetic is a
 *  caller that can get it subtly wrong (Akshil, 2026-09-11). */
export function newChatFile(key: string): string {
  return key.startsWith(NEW_CHAT_PREFIX) ? key.slice(NEW_CHAT_PREFIX.length) : "";
}

/** The id a task draft is minted under. `crypto.randomUUID` is present in every
 *  engine this shell runs in, but it is absent over plain http on some older
 *  builds — and a form that throws on its first keystroke would be a far worse
 *  bug than a draft with a home-made id. */
export function newTaskDraftId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `d-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Extra request options a FLUSH needs and an ordinary autosave does not. */
export interface DraftWriteOptions {
  /** Let the request outlive the document — the only way a write started from
   *  `pagehide` or an unmount actually leaves the browser. */
  keepalive?: boolean;
}

/**
 * THE KEY, AS A PATH. Both routes are declared `{key:path}` server-side, so the
 * `/` inside a `new:<file>` key is part of the path rather than something to
 * hide from it: each SEGMENT is encoded and the separators are left standing.
 * `encodeURIComponent` on the whole key would send `%2F`, which every layer
 * between here and the route gets to normalise differently — and this key is a
 * file path, so it is the common case rather than the exotic one.
 */
const encodePath = (key: string) => key.split("/").map(encodeURIComponent).join("/");

const chatUrl = (key: string) => `/api/drafts/chat/${encodePath(key)}`;
const taskUrl = (id: string) => `/api/drafts/task/${encodePath(id)}`;

/**
 * Every write in this module goes through here, and nothing it can do reaches
 * the caller. `X-Fused` is the same CSRF-ish marker every mutation in
 * `platform/lib/api.ts` carries (it forces a CORS preflight, so a foreign page
 * cannot write blind); the body is omitted for a DELETE, which has none.
 *
 * The promise RESOLVES on failure rather than rejecting, so a caller may await
 * it without a try/catch and an un-awaited call can never become an unhandled
 * rejection in the middle of somebody typing.
 */
async function write(
  method: "PUT" | "POST" | "DELETE",
  url: string,
  body: unknown,
  opts?: DraftWriteOptions,
): Promise<boolean> {
  try {
    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json", "X-Fused": "1" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      ...(opts?.keepalive ? { keepalive: true } : {}),
    });
    return res.ok;
  } catch {
    // Offline, server restarting, the document unloading mid-flight. The draft
    // just isn't saved; nothing on screen changes and nothing is said.
    return false;
  }
}

/**
 * CHAT KEYS WHOSE DRAFT IS SPENT — sent, or cleared on purpose — and which must
 * therefore read back as EMPTY even while the server still says otherwise
 * (Bugbot, PR #1118).
 *
 * THE RACE IT CLOSES. The first send from a session-less chat does two things in
 * the same tick: it fires `DELETE /api/drafts/chat/new:<file>`, and it gives the
 * landing a session — which remounts the whole chat, composer included
 * (`ClaudeChat`'s remount on the first send). The new mount seeds itself from
 * the server (`fetchChatDraft`), and that GET can overtake a DELETE that is
 * still in flight. What it answers is the sentence that was just sent, into a
 * box the send had emptied; the rekey that follows then walks those words onto
 * the session, and the message the reader sent is sitting on their own task row
 * as an unsent draft.
 *
 * NOTHING INSIDE ONE MOUNT CAN FIX IT. `reset`, `stop` and `settle` all belong
 * to a component that is being thrown away at that instant, and the seed that
 * resurrects the words runs in a DIFFERENT one. So the fact lives at module
 * scope, where both mounts can see it — which is also where it belongs, since
 * spent-ness is a fact about the KEY and not about any one composer.
 *
 * ENTERED BEFORE THE REQUEST, because covering the window the request is in is
 * the entire point; and KEPT afterwards, because the window does not close when
 * the DELETE answers — a remount a second later would seed from a cached or
 * re-read snapshot just the same. A key stays spent until somebody types into it
 * again, which is exactly what `saveChatDraft` below hears.
 */
const spent = new Set<string>();

/**
 * Upsert this chat's draft. EMPTY TEXT WITH NO ATTACHMENTS IS A DELETE, decided
 * server-side (design.md: "writing empty == delete") — so the caller does not
 * have to tell "cleared the box" apart from "never typed", and a composer
 * emptied by hand leaves no ghost row on the list.
 *
 * A write WITH CONTENT un-spends the key (see `spent`): there are words under it
 * again, they were put there deliberately, and the next composer to open on it
 * must be allowed to see them. An empty write does not, because an empty write
 * IS a delete and un-spending on it would re-open the very window `spent`
 * closes.
 */
export function saveChatDraft(
  key: string,
  text: string,
  attachments: readonly DraftAttachment[] = [],
  opts?: DraftWriteOptions,
): Promise<boolean> {
  if (text.trim() || attachments.length) spent.delete(key);
  return write("PUT", chatUrl(key), { text, attachments }, opts);
}

/**
 * DELETES STILL IN THE AIR, one per key at most — what `rekeyChatDraft` waits
 * on so a move cannot overtake the removal of the thing it is moving (Bugbot,
 * PR #1118, 2026-09-11).
 *
 * `spent` already stops a REMOUNT from reading the sent words back; this stops
 * the SERVER from being asked to copy them. The two requests the first send
 * fires — `DELETE new:<file>` and `POST /api/drafts/chat/rekey` — are otherwise
 * unordered, and the order that loses is the one where the rekey arrives first:
 * the record is still there, so the route copies it onto the session id, and a
 * sent message becomes an unsent draft on the session's own row.
 *
 * A key drops out the moment its own request answers, and only if it is still
 * the one being tracked — a second delete for the same key while the first is
 * running is the later one's to own.
 */
const pendingDeletes = new Map<string, Promise<boolean>>();

/** On send, and on an explicit clear. The key is marked spent BEFORE the
 *  request goes out — see `spent` for the remount that would otherwise read the
 *  draft back out from under the delete — and the request is remembered while
 *  it runs, for the rekey that must not pass it (see `pendingDeletes`). */
export function deleteChatDraft(key: string, opts?: DraftWriteOptions): Promise<boolean> {
  spent.add(key);
  const done: Promise<boolean> = write("DELETE", chatUrl(key), undefined, opts).then((ok) => {
    if (pendingDeletes.get(key) === done) pendingDeletes.delete(key);
    return ok;
  });
  pendingDeletes.set(key, done);
  return done;
}

/**
 * THE DRAFT MOVES WITH THE SESSION IT TURNED OUT TO BE (design.md, Round 2:
 * "Every draft has a TASK number").
 *
 * A chat with no session yet keys its draft `new:<file>` and is given a TASK
 * number under that key. The first send creates the session, and from the next
 * render the composer keys on the session id instead — so without this the
 * number, and the row wearing it, would be stranded on a key nothing reads
 * again. The server moves both (`tasks_store.rekey`, the same call that walks a
 * `pending:` number onto a session).
 *
 * FIRE AND FORGET, once, when the id of the session a session-less send created
 * is learned. WHICH SEND THAT WAS is not this module's business and never was:
 * `ClaudeChat` binds the move to the run it dispatched, because a chat merely
 * opened ON a session also spends its first renders with an empty id and a
 * module-level "a send is owed a rekey" note could be spent by the wrong one
 * (Bugbot, PR #1118, 2026-09-12). A refusal costs the
 * number's continuity and nothing the reader is doing — which is this module's
 * standing contract, and doubly right here: the thing being renamed is a draft
 * that the send is about to delete anyway (Akshil, 2026-09-11).
 *
 * IT WAITS FOR THE SEND'S DELETE, AND IT CARRIES SPENT-NESS ACROSS (Bugbot,
 * PR #1118, 2026-09-11). Going out first was never enough: `DELETE new:<file>`
 * and this POST are two requests with no order between them, and if the rekey
 * is served first the record is still sitting there — the route copies it onto
 * the session id, and the composer that just remounted seeds from the SESSION
 * key, which nothing had marked spent. The sentence the reader sent is back in
 * their box, one key to the right of where the fix was looking.
 *
 * So: await whatever DELETE for `from` is still running (`pendingDeletes` —
 * nothing to wait for in the ordinary case, where the chat simply learned its
 * id without a send), and mark `to` spent whenever `from` is, BEFORE either
 * request, because the window being covered is the one they are in. Spent on
 * the new key means the same as on the old one: the words were just sent, and
 * nothing is restored under it until somebody types again — a PUT with content
 * un-spends it exactly as before (`saveChatDraft`).
 *
 * The reader who typed ON after sending is still served: that PUT carried
 * content, so it had already un-spent `from`, and this copies no spent-ness
 * onto `to`. Those words belong to the conversation the send created, which is
 * the one case the route's copy exists for.
 */
export async function rekeyChatDraft(from: string, to: string): Promise<boolean> {
  if (!from || !to || from === to) return false;
  if (spent.has(from)) spent.add(to);
  await pendingDeletes.get(from);
  return write("POST", "/api/drafts/chat/rekey", { from, to });
}

/**
 * Upsert a task draft under the id the form minted.
 *
 * `fromChatKey` is the MOVE (design.md, Round 2: "A draft moves, never
 * duplicates"). The Schedule hop carries a composer's words into the task form,
 * and for the moment in between there are two stores holding the same sentence:
 * the chat draft the composer autosaved, and the task draft this call is
 * creating. Naming the chat key on the first write makes the server delete that
 * one in the same request — one draft, one row, one TASK number, with no window
 * in which the List shows the same words twice.
 *
 * Sent on the FIRST write only (the caller latches it): the second PUT is an
 * ordinary keystroke save, and repeating a delete for a key that is already
 * gone is a request that can only ever be a no-op or a surprise.
 */
export function saveTaskDraft(
  id: string,
  form: TaskDraftForm,
  opts?: DraftWriteOptions,
  fromChatKey?: string,
): Promise<boolean> {
  const body = fromChatKey ? { ...form, from_chat_key: fromChatKey } : form;
  return write("PUT", taskUrl(id), body, opts);
}

/** Discard. `POST /api/schedule` deletes the draft itself when it is handed a
 *  `draft_id`, so this is the DISCARD button's call and not the Schedule path's. */
export function deleteTaskDraft(id: string, opts?: DraftWriteOptions): Promise<boolean> {
  return write("DELETE", taskUrl(id), undefined, opts);
}

/**
 * Every draft there is. An unreadable answer is an EMPTY SNAPSHOT rather than a
 * throw, for the reason `parseAttachmentsParam` gives: this is read inside the
 * effect that seeds a composer, and a parse error there would cost the mount.
 */
export async function fetchDrafts(): Promise<DraftsSnapshot> {
  try {
    const res = await fetch("/api/drafts");
    if (!res.ok) return EMPTY;
    const data = (await res.json()) as Partial<DraftsSnapshot> | null;
    if (!data || typeof data !== "object") return EMPTY;
    return { chat: data.chat ?? {}, task: data.task ?? {} };
  } catch {
    return EMPTY;
  }
}

/** One chat draft, or null. A convenience over `fetchDrafts` — there is no
 *  per-key GET in the contract, and the store is small enough that the whole of
 *  it is cheaper than a second endpoint would be.
 *
 *  A SPENT KEY IS ALWAYS NULL, whatever the server still holds: this is the
 *  call a remounting composer seeds from, and it is the one that would put a
 *  sent message back in the box (see `spent`).
 *
 *  CHECKED TWICE — before the request, so the ordinary resurrection costs not
 *  even a round trip, and AGAIN once the answer is in hand (Bugbot, PR #1118,
 *  2026-09-11). A GET dispatched a moment before the send is a GET that passed
 *  the first check while the key was still live, and it answers out of a
 *  snapshot taken before the DELETE landed. Whether the key went spent at the
 *  start of the wait or in the middle of it makes no difference to the reader:
 *  the words came back after they were sent. */
export async function fetchChatDraft(key: string): Promise<ChatDraft | null> {
  if (spent.has(key)) return null;
  const all = await fetchDrafts();
  if (spent.has(key)) return null;
  return all.chat[key] ?? null;
}

/** What `useAutosave` hands back: the things a caller ever needs to do to a
 *  running autosave by hand. */
export interface Autosave<T> {
  /** Write NOW if anything has changed since the last write — what send, submit
   *  and every unload path spend. */
  flush(): void;
  /**
   * FORGET WHAT IS PENDING and take `next` as already-written.
   *
   * The composer's send: the box is about to be cleared and the draft deleted,
   * and a debounced write armed a keystroke earlier would otherwise land AFTER
   * the DELETE and resurrect the message that was just sent. Told what the
   * value is about to become, rather than reading the current one, because the
   * state write that empties the box has not been applied yet at the moment
   * this is called.
   *
   * `next` becomes BOTH what counts as written and what a later write would
   * send. Saying only the first left the sent sentence behind in the ref every
   * write reads from, and the unmount flush — which is the one the first send
   * from the landing always runs, since gaining a session remounts the chat —
   * found it different from the empty value just recorded and PUT the words
   * straight back, un-spending the key on the way (Bugbot, PR #1118,
   * 2026-09-11). Assigning both makes a flush after a reset a flush with
   * nothing to say.
   *
   * `reset` ONLY cancels the PENDING timer — a write already dispatched (a
   * `fetch` awaiting its response) cannot be cancelled by anything short of
   * the network, and keeps running underneath. See `settle` for that half
   * (Akshil, 2026-09-11).
   */
  reset(next: T): void;
  /**
   * DISARM, permanently for this mount. The Schedule button's other half: the
   * server deletes the draft as part of `POST /api/schedule`, so a debounced
   * write still in the pipe would resurrect a draft for a task that now exists
   * (Akshil, 2026-09-11 — "stop autosave so a late flush can't resurrect it").
   *
   * PERMANENTLY includes the unmount flush: every write goes through one
   * function and that function reads this flag first, so a stopped autosave
   * writes nothing again for the life of the mount — no timer, no blur, no
   * pagehide, no teardown (Bugbot, PR #1118, 2026-09-11).
   *
   * Same caveat as `reset`: this stops the NEXT write from being armed, not
   * one already in flight. See `settle`.
   */
  stop(): void;
  /**
   * WAIT OUT WHATEVER WRITE IS RUNNING, if any.
   *
   * Neither `reset` nor `stop` can cancel a `fetch` already sent — the PUT for
   * a keystroke a moment ago may still be in the air when Send, Discard, or
   * Schedule fires. Each of those follows a `reset`/`stop` with a server-side
   * DELETE (the draft is spent, or the server drops it as part of creating the
   * task), and an in-flight PUT that lands after that DELETE resurrects
   * exactly the draft the delete was for. `settle` resolves once that one
   * write (whichever is running at the moment it is called) is done, so a
   * caller can order its own delete after it rather than after a cancellation
   * that was never possible. Resolves immediately when nothing is in flight.
   * Never rejects, for the same reason every write in this module never does
   * (Akshil, 2026-09-11).
   */
  settle(): Promise<void>;
}

export interface AutosaveOptions {
  /** Quiet time after the last change before a write goes out. */
  delay?: number;
  /**
   * THE OPENING VALUE COUNTS AS UNWRITTEN — so the very first debounce writes
   * it, with nobody having typed anything (Akshil, 2026-09-11).
   *
   * Off by default, and the default is the load-bearing one: the hook seeds
   * "what was last written" with the value it mounts on, which is exactly what
   * makes "nothing minted for an untouched modal" true — a form whose fields
   * are merely non-empty (an Edit, a re-opened draft) never writes until a
   * person changes something.
   *
   * ON is the case where the content ARRIVED already typed, somewhere else:
   * the Schedule hop hands the task form the sentence the composer was holding
   * (design.md, Round 2, "A draft moves, never duplicates"). Those words are a
   * draft the moment they land — the chat's copy is about to be deleted in
   * favour of this one — so waiting for a keystroke would be waiting to lose
   * them.
   *
   * Read ONCE, at mount, like every other opening fact: flipping it later says
   * nothing, because by then the question of what the form opened with has
   * already been answered.
   */
  writeInitial?: boolean;
}

/** The seed for `written` when `writeInitial` says the opening value has not
 *  been written. Not a value `JSON.stringify` can return for anything — a
 *  stringified string always carries its quotes — so the first comparison can
 *  only ever be "different". */
const UNWRITTEN = "\u0000unwritten";

/** design.md: 600 ms after the last keystroke. Long enough that a sentence is
 *  one write, short enough that a reader who types and immediately closes the
 *  tab is covered by the flush rather than by the timer. */
export const AUTOSAVE_DELAY_MS = 600;

/**
 * AUTOSAVE, THE WHOLE OF IT — debounce, flush, and the promise never to be in
 * the way.
 *
 * `value` is whatever the caller wants persisted; `save` is called with it. The
 * hook compares SERIALISED values (`JSON.stringify`), so a caller may hand it a
 * fresh object every render — which every form does — without that alone
 * counting as a change. A value that serialises identically is never written
 * twice, which is what keeps a re-render from becoming a request.
 *
 * WHEN IT WRITES:
 *   * `delay` ms after the last change — the ordinary case;
 *   * immediately on window blur, on `pagehide`, and on the document going
 *     hidden — the three moments a half-typed thing is most likely to be
 *     abandoned;
 *   * immediately on unmount, which is what covers leaving a chat for another
 *     one, or closing the task form.
 *
 * The last three pass `keepalive`, because a request started while the document
 * is going away is cancelled with it otherwise. `save` NEVER throws into the
 * caller (the module's writes swallow everything) and its result is ignored:
 * there is no retry and no error surface by design — see the header.
 *
 * `save` is read through a ref, so an inline closure (what every call site
 * passes) does not tear down and rebuild the pending write on every render.
 *
 * `save` MAY return the promise its own `fetch` wrapper gives back (every call
 * site does — `saveChatDraft`/`saveTaskDraft` resolve `false` rather than
 * rejecting; see the header). Returning it is what lets `settle` (below) know
 * when a write is actually done rather than merely dispatched — a caller that
 * ignores the return value loses nothing, since `settle` on an autosave whose
 * `save` never resolves anything just resolves at once.
 */
export function useAutosave<T>(
  value: T,
  save: (value: T, opts: DraftWriteOptions) => unknown,
  { delay = AUTOSAVE_DELAY_MS, writeInitial = false }: AutosaveOptions = {},
): Autosave<T> {
  const saveRef = useRef(save);
  saveRef.current = save;
  const valueRef = useRef(value);
  valueRef.current = value;
  // THE VALUE AS ONE STRING, and it is what the debounce below actually depends
  // on. Every caller hands a fresh object literal each render, and a host that
  // re-renders on a poll (the chat does, every 400ms) would otherwise clear and
  // re-arm the timer forever and never write anything.
  const serial = JSON.stringify(value) ?? "";
  // What was last WRITTEN, serialised. Seeded with the opening value so a mount
  // alone never writes: the form's own initial state is not something the user
  // typed, and a task draft minted by merely opening the card is exactly what
  // "nothing minted for an untouched modal" forbids.
  //
  // …unless the caller says the opening value ARRIVED already typed — see
  // `writeInitial`, whose one caller is the task form opened from the chat's
  // Schedule hop. Then the seed is a string no value can serialise to, so the
  // first debounce finds a difference and the draft is minted with nobody
  // having touched the card (Akshil, 2026-09-11).
  const written = useRef<string>(writeInitial ? UNWRITTEN : (JSON.stringify(value) ?? ""));
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const stopped = useRef(false);
  // THE WRITE CURRENTLY RUNNING, if any — what `settle` waits on. Seeded
  // resolved so a `settle` called before this autosave has ever written
  // anything (a Discard on an untouched card, say) returns at once rather
  // than hanging on a promise nothing ever produced. Wrapped in
  // `Promise.resolve(...).catch(() => false)` on every write below: `save`'s
  // own promise already never rejects (see the header), but `settle` must
  // hold that guarantee regardless of what a future caller's `save` does —
  // one bad write must not leave every subsequent `settle` rejecting forever
  // (Akshil, 2026-09-11).
  const inflight = useRef<Promise<unknown>>(Promise.resolve());

  const clear = () => {
    if (timer.current !== undefined) clearTimeout(timer.current);
    timer.current = undefined;
  };

  const writeNow = useCallback((opts: DraftWriteOptions) => {
    clear();
    if (stopped.current) return;
    const next = JSON.stringify(valueRef.current) ?? "";
    if (next === written.current) return;
    written.current = next;
    inflight.current = Promise.resolve(saveRef.current(valueRef.current, opts)).catch(
      () => false,
    );
  }, []);

  const flush = useCallback(() => writeNow({ keepalive: true }), [writeNow]);
  const stop = useCallback(() => {
    stopped.current = true;
    clear();
  }, []);
  const reset = useCallback((next: T) => {
    clear();
    // The VALUE as well as the bookkeeping — see `Autosave.reset`. Until the
    // render that empties the box arrives, `valueRef` still holds the sentence
    // that was just sent, and the unmount flush would write it back.
    valueRef.current = next;
    written.current = JSON.stringify(next) ?? "";
  }, []);
  const settle = useCallback((): Promise<void> => inflight.current.then(() => undefined), []);

  // The debounce. Runs on every render whose serialised value differs from what
  // was last written — the comparison is inside `writeNow`, so a timer that
  // fires on an unchanged value costs one string compare and no request.
  useEffect(() => {
    if (stopped.current) return;
    if (serial === written.current) return;
    clear();
    timer.current = setTimeout(() => writeNow({}), delay);
    return clear;
  }, [serial, delay, writeNow]);

  // The three ways a document leaves, plus the unmount. `pagehide` rather than
  // `unload` alone: it is the one that fires for a bfcache navigation, which is
  // most of them. `visibilitychange` covers the phone/tab-switch that never
  // becomes a pagehide at all.
  useEffect(() => {
    const onHide = () => flush();
    const onVisibility = () => {
      if (document.visibilityState === "hidden") flush();
    };
    window.addEventListener("blur", onHide);
    window.addEventListener("pagehide", onHide);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("blur", onHide);
      window.removeEventListener("pagehide", onHide);
      document.removeEventListener("visibilitychange", onVisibility);
      // THE UNMOUNT FLUSH, and it is the important one: leaving a chat for
      // another unmounts the composer without any window event at all. It goes
      // through `writeNow` like every other write, so a `stop()` disarms it and
      // a `reset()` leaves it nothing to write.
      flush();
    };
  }, [flush]);

  return { flush, stop, reset, settle };
}
