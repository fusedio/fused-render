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
// server.
//
// ONE RECORD PER DRAFT, VERSIONED (design "Drafts: one record, one key,
// versioned, pushed", 2026-09-16). There is no second copy of a draft anywhere
// — no sessionStorage hop, no `?message=` param, no `draft:<id>` minted from a
// chat — so duplicates and resurrections are impossible by construction rather
// than by bookkeeping. What replaced the bookkeeping is a VERSION: every record
// carries one, every write states the version it is editing (`If-Match`), and a
// write against a stale version is refused with the server's record in the body
// (409). The old `spent` set, the `inflight` map and the stop/settle/resume
// choreography all existed to order writes against reads inside one document;
// a version orders them against every document at once, including the other tab.
//
// THE ONE RULE EVERY WRITE HERE OBEYS: it must never break what the user is
// doing. Typing is not blocked, a refusal is not shown, a network error is
// swallowed. A draft that failed to save costs the user a draft; a draft that
// threw inside a keystroke handler costs them the keystroke. The ONE thing that
// does reach the screen is a 409 taken while the reader is mid-sentence — their
// words are kept and a soft toast says so, because silently keeping them would
// leave the record and the box disagreeing with nothing said about it.
import { useCallback, useEffect, useRef } from "react";
import type { RecurrenceRule } from "./api";

/** ONE ATTACHMENT, as the schedule form stores it — `path`, `name`, `kind`, the
 *  identical three fields the composer's tray and a stored entry's
 *  `attachments` row hold. Spelled again here rather than
 *  imported because `platform/` may not import `apps/` (scripts/check-boundaries)
 *  — and the wire shape is the server's anyway, not the chat's. */
export interface DraftAttachment {
  path: string;
  name: string;
  kind: "image" | "file";
}

/**
 * THE SETTINGS A DRAFT CARRIES BESIDE ITS WORDS — the New task card's non-prose
 * fields, stored on the chat record itself so a Schedule hop edits ONE record
 * (design "one record", §1; contract §1).
 *
 * The WORDS are not here. They live in `ChatDraft.text`, which is the composer's
 * box and the card's two prose fields joined (`NewJobModal.splitDraft` /
 * `joinDraft`) — one string both editors can open on, so a hop out and a walk
 * back are lossless. A `description` field here would be a second copy of the
 * same sentence, which is the whole thing this design removes.
 *
 * Every field is optional and a PUT's `form` is a PATCH: keys present are
 * written, keys absent keep what is stored (contract §2). That is what lets the
 * composer's own plain autosave — which sends no `form` at all — save words
 * without wiping the time and repeat a hop put on the same record.
 */
export interface ChatDraftForm {
  title?: string;
  when?: string | null;
  repeat?: string | null;
  custom_rule?: RecurrenceRule | null;
  model?: string;
  effort?: string;
  permission?: string;
  target?: string;
  new_task_each_run?: boolean | null;
}

/** One chat draft as `GET /api/drafts` returns it. */
export interface ChatDraft {
  text: string;
  attachments: DraftAttachment[];
  updated_at: number;
  /** MONOTONIC PER KEY, bumped by the server on every write — the whole of the
   *  concurrency story (see the header). Stated back on the next write as
   *  `If-Match`, so a write that would clobber somebody else's is refused
   *  rather than silently winning. */
  version: number;
  /** The settings beside the words, `{}` when the draft carries none. */
  form: ChatDraftForm;
  /**
   * THE FORM THESE WORDS ARE ACTUALLY IN, or `""` for the ordinary chat draft
   * that is a record of its own (`fused_render/drafts.py`, "one record, two
   * doors"; Akshil, 2026-09-12).
   *
   * A New task card bound to a session IS that conversation's unsent message,
   * so the server serves it under the session key too — the same words, the
   * card's title and description joined back into one box — and the composer
   * seeds from it, edits it and deletes it through exactly the calls it already
   * makes. The id is here so a client that wants to tell the two apart can;
   * nothing in the composer has to. Absent on an older server, which is the
   * same fact as `""`.
   */
  bound_draft?: string;
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
   * THE CONVERSATION THIS TASK IS A MESSAGE TO, or "" when it is a message to
   * nobody yet (Akshil, 2026-09-12).
   *
   * The composer's Schedule button can hop out of a chat that has ALREADY RUN,
   * and then the task being written is the next turn of that thread — the server
   * schedules it into the session and the number it keeps is the session's. The
   * page knew that while it stayed open and the draft on disk did not, so
   * exiting the card and reopening the draft scheduled it into a NEW session
   * under a NEW number, and the task the reader had been watching was gone.
   *
   * Restated on every save: it costs a short string and means a reopened card
   * cannot lose the binding to a merge that went the wrong way.
   *
   * "" for every other opening — the "+ New task" button, a calendar slot, and
   * a hop out of a chat that has never been sent, which has no session to bind
   * to at all (its draft is keyed `new:<file>`).
   */
  session_id: string;
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
  /** `ChatDraft.version`'s twin, and the same contract. */
  version: number;
}

/** `GET /api/drafts` — everything, both kinds, keyed. */
export interface DraftsSnapshot {
  chat: Record<string, ChatDraft>;
  task: Record<string, TaskDraft>;
}

/**
 * WHICH KEY THIS COMPOSER'S DRAFT LIVES UNDER (design.md, "Chat draft key").
 * The session id when the chat has one; otherwise `new:<file>` — the same file
 * the sessionStorage hop keys on, so the two halves describe one chat.
 *
 * WHAT `<file>` IS, written down once because three things have to agree on it
 * (Akshil, 2026-09-11). It is the chat's OWN `file` prop — the path the Claude
 * pane is mounted on, which is the folder for a folder-scoped chat and the
 * document for a file-scoped one — verbatim and unnormalised, exactly as
 * `ClaudeChat` receives it. The other two quote that string rather than
 * building one of their own:
 *
 *   * the Schedule hop's `?draft=` param IS this key (`SchedButton.go`), which
 *     is how the task form knows which record it is editing;
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

/**
 * IS THIS LISTING KEY ONE A CHAT DRAFT IS FILED UNDER?
 *
 * The Tasks listing files rows under four shapes and a chat draft answers to
 * two of them: a bare SESSION id once the conversation exists, and
 * `new:<file>` before it does — which is exactly `chatDraftKey`'s two answers,
 * read backwards. The other two carry a prefix and a colon (`draft:<id>` for a
 * task draft's row, `pending:<entry>` for a scheduled message with no session
 * yet), and a session id can hold no colon at all (`drafts._SESSION_KEY`
 * server-side), so the absence of one is the whole test.
 *
 * Asked by the shell when the listing says a key is GONE, to decide whether
 * there are unsent words behind it to clean up (App.tsx).
 */
export function isChatDraftKey(key: string): boolean {
  if (!key) return false;
  if (key.startsWith(NEW_CHAT_PREFIX)) return true;
  return !key.includes(":");
}

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
  /**
   * THE VERSION THIS WRITE IS ABOUT, stated by the caller instead of read out
   * of the map at fire time (Bugbot, PR #1180).
   *
   * The map answers "the newest version this client has heard of", and for an
   * ordinary keystroke save that is the right question. For a write that is
   * ABOUT A PARTICULAR RECORD it is the wrong one: the send's DELETE is about
   * the record holding the sentence that was just sent, and between deciding to
   * delete it and the request leaving, a follow-up the reader typed can have
   * saved over that key. Fired against the map, the DELETE then states the
   * FOLLOW-UP's version and deletes the follow-up.
   *
   * So the send names the version its own write produced. A newer record is
   * then refused (409) instead of removed, which is exactly what the reader
   * wants: their unsent words are not what they asked to spend.
   */
  ifMatch?: number;
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
 * THE VERSION THIS CLIENT LAST SAW UNDER EACH KEY (contract §2).
 *
 * Keyed the way the LISTING keys drafts — a chat key verbatim (a session id, or
 * `new:<file>`) and `draft:<id>` for a task draft — because that is the same
 * key `/api/tasks/changes` pushes versions under (`tasksPulse.onDraftChange`),
 * and two spellings of one record is exactly the class of bug this design
 * exists to end.
 *
 * Module scope, like everything else here that is a fact about a KEY rather
 * than about one component: the composer and the New task card can be open on
 * the same record in one document, and they must state the same version.
 *
 * ABSENT means "this client has never read or written this key", which is a
 * real third answer and not zero: a write with no version is UNCONDITIONAL
 * (contract §2), which is the right thing for a first write, and `gone` from
 * the change feed must be IGNORED for such a key (contract §3 — the announced
 * key set is noisy, and discarding unsaved words on it would be the worst bug
 * in the feature).
 */
const versions = new Map<string, number>();

/** The listing's key for a task draft — the one `/api/tasks/changes` and the
 *  rows both use. */
export const taskDraftKey = (id: string) => `draft:${id}`;

/** What this client believes the server holds under `key`, or `undefined` for a
 *  key it has never seen. */
export function draftVersion(key: string): number | undefined {
  return versions.get(key);
}

/** Take a version the server just stated. Never goes BACKWARDS: a stale GET can
 *  answer after a newer write's response, and adopting its number would make
 *  the next write state a version the server has already moved past. */
export function rememberDraftVersion(key: string, version: unknown): void {
  if (typeof version !== "number" || !Number.isFinite(version)) return;
  const seen = versions.get(key);
  if (seen !== undefined && seen >= version) return;
  versions.set(key, version);
}

/** …and forget one, for a record this client has just deleted. The key then
 *  reads as "never seen", which is what stops a later `gone` for it from being
 *  acted on twice. */
export function forgetDraftVersion(key: string): void {
  versions.delete(key);
}

/** THE ANSWER EVERY WRITE IN THIS MODULE GIVES BACK.
 *
 *  `ok` is the old boolean, unchanged for every caller that only wants to know
 *  whether the words are safe. `conflict` is the new third answer: the write was
 *  REFUSED because somebody else had edited the record, and this is what they
 *  left there (`null` — they deleted it). The caller decides what to do with it;
 *  see `useAutosave`'s adoption rule, which is where the decision actually
 *  lives. */
export interface DraftWrite<R> {
  ok: boolean;
  conflict?: R | null;
  /**
   * THE VERSION THIS WRITE MADE — the number the server stamped on the record
   * this request wrote, and absent for a write that failed, was refused, or
   * removed the record.
   *
   * It is not the same fact as `draftVersion(key)`: that one moves with every
   * answer this client takes, including somebody else's write landing a
   * millisecond later. This is the version OF THIS WRITE, which is what a
   * caller that has to act on what it just saved — the send's DELETE, the
   * Schedule hop's PUT — must state so a straggler is refused rather than
   * obeyed.
   */
  version?: number;
}

/** A task write also answers WHICH ID IT LANDED ON — see `saveTaskDraft`. */
export interface TaskWrite extends DraftWrite<TaskDraft> {
  id: string;
}

/** The 409 body, exactly as the contract spells it (§2). */
interface VersionConflict {
  error: "version";
  record: unknown;
  version?: number;
}

function conflictOf(data: unknown): VersionConflict | null {
  if (!data || typeof data !== "object") return null;
  const row = data as { error?: unknown; record?: unknown; version?: unknown };
  if (row.error !== "version") return null;
  return {
    error: "version",
    record: row.record ?? null,
    ...(typeof row.version === "number" ? { version: row.version } : {}),
  };
}

/**
 * Every write in this module goes through here, and nothing it can do reaches
 * the caller as a throw.
 *
 * `X-Fused` is the same CSRF-ish marker every mutation in `platform/lib/api.ts`
 * carries (it forces a CORS preflight, so a foreign page cannot write blind).
 *
 * `If-Match` IS SENT WHENEVER THIS CLIENT KNOWS A VERSION, and omitted when it
 * does not — which the server reads as unconditional (contract §2). Omitting it
 * on a first write is not a weakening: there is nothing to clobber yet, and the
 * server's own create is what allocates version 1.
 *
 * The promise RESOLVES on failure rather than rejecting, so a caller may await
 * it without a try/catch and an un-awaited call can never become an unhandled
 * rejection in the middle of somebody typing.
 */
async function write<R>(
  method: "PUT" | "DELETE",
  key: string,
  url: string,
  body: unknown,
  opts: DraftWriteOptions | undefined,
  /** Pull the stored record (and its version) out of a 200 answer, so a write
   *  leaves this client holding the version its own write produced rather than
   *  waiting for the next GET to tell it. */
  landed: (answer: unknown) => { record: R | null; version: unknown } | null,
): Promise<DraftWrite<R>> {
  // THE CALLER'S VERSION OUTRANKS THE MAP'S (see `DraftWriteOptions.ifMatch`).
  const seen = opts?.ifMatch ?? versions.get(key);
  try {
    const res = await fetch(url, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-Fused": "1",
        ...(seen === undefined ? {} : { "If-Match": String(seen) }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      ...(opts?.keepalive ? { keepalive: true } : {}),
    });
    if (res.status === 409) {
      const clash = conflictOf(await res.json().catch(() => null));
      if (!clash) return { ok: false };
      // ADOPT THE SERVER'S NUMBER BEFORE ANYTHING ELSE. Whatever the caller
      // decides to do with the record, the next write from this client has to
      // state the version that actually exists or it is refused for ever.
      if (clash.record === null) versions.delete(key);
      else rememberDraftVersion(key, clash.version ?? (clash.record as { version?: unknown })?.version);
      return { ok: false, conflict: (clash.record ?? null) as R | null };
    }
    if (!res.ok) return { ok: false };
    const answer = await res.json().catch(() => null);
    const got = landed(answer);
    let made: number | undefined;
    if (got) {
      if (got.record === null) versions.delete(key);
      else {
        rememberDraftVersion(key, got.version);
        if (typeof got.version === "number" && Number.isFinite(got.version)) {
          made = got.version;
        }
      }
    }
    return made === undefined ? { ok: true } : { ok: true, version: made };
  } catch {
    // Offline, server restarting, the document unloading mid-flight. The draft
    // just isn't saved; nothing on screen changes and nothing is said.
    return { ok: false };
  }
}

/** The `draft` a chat route answers with, and its version. */
const chatLanded = (answer: unknown) => {
  const row = (answer ?? {}) as { draft?: unknown; removed?: unknown };
  if (row.draft === undefined) return { record: null, version: undefined };
  const draft = row.draft as ChatDraft | null;
  return { record: draft, version: draft?.version };
};

const taskLanded = (answer: unknown) => {
  const row = (answer ?? {}) as { draft?: unknown };
  if (row.draft === undefined) return { record: null, version: undefined };
  const draft = row.draft as TaskDraft | null;
  return { record: draft, version: draft?.version };
};

/**
 * Upsert this chat's draft. EMPTY TEXT WITH NO ATTACHMENTS IS A DELETE, decided
 * server-side (design.md: "writing empty == delete") — so the caller does not
 * have to tell "cleared the box" apart from "never typed", and a composer
 * emptied by hand leaves no ghost row on the list. A record carrying a FORM
 * survives that emptying with its settings intact and simply reads as no draft
 * (contract §2), which is what keeps a hop's time and repeat alive while the
 * reader clears the words to retype them.
 *
 * `form` IS A PATCH AND OMITTING IT CHANGES NOTHING (contract §2). The composer
 * never sends one — it has no opinion about a time or a repeat — so its
 * keystroke saves cannot wipe what the New task card put on the same record.
 * The card sends the fields it owns.
 */
export function saveChatDraft(
  key: string,
  text: string,
  attachments: readonly DraftAttachment[] = [],
  opts?: DraftWriteOptions,
  form?: ChatDraftForm,
): Promise<DraftWrite<ChatDraft>> {
  return write<ChatDraft>(
    "PUT",
    key,
    chatUrl(key),
    form === undefined ? { text, attachments } : { text, attachments, form },
    opts,
    chatLanded,
  );
}

/** On send, and on an explicit clear. */
export function deleteChatDraft(
  key: string,
  opts?: DraftWriteOptions,
): Promise<DraftWrite<ChatDraft>> {
  return write<ChatDraft>("DELETE", key, chatUrl(key), undefined, opts, () => ({
    record: null,
    version: undefined,
  }));
}

/**
 * Upsert a task draft under the id the form minted.
 *
 * `draft:<id>` AND NOT `<id>` is the version key, because that is the key the
 * listing and the change feed name this record by (`taskDraftKey`).
 *
 * ANSWERS THE ID THE WRITE ACTUALLY LANDED ON — normally `id`, and somebody
 * else's when the server folded this form into a draft that already held its
 * session (fused_render/drafts.py `put_task`, Bugbot PR #1126). The caller has
 * to adopt it: every later call names the draft by id, so a card that went on
 * using the id it minted would autosave, Discard and Schedule against a record
 * that is not there. `""` for a write that failed — the same silence every
 * other write in this module keeps, and the caller simply keeps the id it had.
 */
export async function saveTaskDraft(
  id: string,
  form: TaskDraftForm,
  opts?: DraftWriteOptions,
): Promise<TaskWrite> {
  const key = taskDraftKey(id);
  let landedId = "";
  const out = await write<TaskDraft>(
    "PUT",
    key,
    taskUrl(id),
    form,
    opts,
    (answer) => {
      const row = (answer ?? {}) as { draft_id?: unknown };
      if (typeof row.draft_id === "string") landedId = row.draft_id;
      return taskLanded(answer);
    },
  );
  // A fold onto another id leaves THIS key's version meaningless — the record
  // it named is not the record that was written.
  if (landedId && landedId !== id) versions.delete(key);
  return { ...out, id: out.ok ? (landedId || id) : "" };
}

/** Discard. `POST /api/schedule` deletes the draft itself when it is handed a
 *  `draft_id` or a `draft_key`, so this is the DISCARD button's call and not
 *  the Schedule path's. */
export function deleteTaskDraft(
  id: string,
  opts?: DraftWriteOptions,
): Promise<DraftWrite<TaskDraft>> {
  return write<TaskDraft>("DELETE", taskDraftKey(id), taskUrl(id), undefined, opts, () => ({
    record: null,
    version: undefined,
  }));
}

/**
 * Every draft there is — or NULL, which means "could not find out" and never
 * "there are none" (Bugbot, PR #1126, 2026-09-12).
 *
 * It still does not throw, for the reason the header gives: this is read inside
 * the effect that seeds a composer, and a rejection there would cost the mount.
 * But answering an empty snapshot made a failed lookup indistinguishable from an
 * empty store, and a caller that reads a network blip as "no draft" is a caller
 * that mints a second one.
 *
 * EVERY VERSION IT SEES IS REMEMBERED, which is what makes this the ordinary way
 * a key becomes known to this client: a composer or a card that seeds from here
 * can state `If-Match` on its very first write, and the change feed's `gone` is
 * actionable for that key from then on (contract §3).
 */
export async function fetchDrafts(): Promise<DraftsSnapshot | null> {
  try {
    const res = await fetch("/api/drafts");
    if (!res.ok) return null;
    const data = (await res.json()) as Partial<DraftsSnapshot> | null;
    if (!data || typeof data !== "object") return null;
    const chat = data.chat ?? {};
    const task = data.task ?? {};
    for (const [key, row] of Object.entries(chat)) rememberDraftVersion(key, row?.version);
    for (const [id, row] of Object.entries(task)) {
      rememberDraftVersion(taskDraftKey(id), row?.version);
    }
    return { chat, task };
  } catch {
    return null;
  }
}

/**
 * One chat draft — and it answers in THREE states, not two (Bugbot, PR #1180).
 *
 *   * a RECORD — the draft, as the server holds it;
 *   * `null` — THERE IS NO RECORD under that key, which the contract makes an
 *     instruction: a reader adopting this empties its box;
 *   * `undefined` — COULD NOT FIND OUT. The GET failed — offline, the server
 *     restarting, a blip — so nothing is known and nothing may be done.
 *
 * The last two used to collapse into `null`, and it cost words: the change
 * feed's adopt path read a failed GET as "the draft was deleted" and cleared a
 * box that still had a sentence in it. `undefined` for "unknown" is the same
 * third answer `draftVersion` already gives for a key nobody has seen, and the
 * same distinction `fetchDrafts` has made since PR #1126.
 *
 * A convenience over `fetchDrafts` — there is no per-key GET in the contract,
 * and the store is small enough that the whole of it is cheaper than a second
 * endpoint would be.
 */
export async function fetchChatDraft(
  key: string,
): Promise<ChatDraft | null | undefined> {
  const all = await fetchDrafts();
  if (!all) return undefined;
  return all.chat[key] ?? null;
}

/**
 * What `useAutosave` hands back — THREE CALLS, where there used to be five.
 *
 * `stop` / `resume` were an ordering protocol between one mount's pending write
 * and somebody else's DELETE, and a version does that job for every document at
 * once: a write that arrives after the record it edits has been REWRITTEN is
 * refused by the server (409) instead of landing. A discard and a schedule no
 * longer stand this hook down and wait for it; they simply write, and this
 * hook's late PUT bounces.
 *
 * `settle` came back, and only for the one case a version cannot order (Bugbot,
 * PR #1180): TWO REQUESTS ALREADY ON THE WIRE CARRYING THE SAME `If-Match`. A
 * version refuses a LATER write that states a STALE number; it says nothing
 * about a PUT and a DELETE that were both dispatched against version 7. The
 * send's DELETE can land first, and the autosave's PUT then recreates the
 * sentence that was just sent as a live draft. So the send waits for the write
 * that is already out before deleting — see `settle`.
 */
export interface Autosave<T> {
  /** Write NOW if anything has changed since the last write — what send, submit
   *  and every unload path spend. */
  flush(): void;
  /**
   * FORGET WHAT IS PENDING and take `next` as already-written.
   *
   * The composer's send: the box is about to be cleared and the draft deleted,
   * and a debounced write armed a keystroke earlier would otherwise be one more
   * request for a record that is gone. Told what the value is about to become,
   * rather than reading the current one, because the state write that empties
   * the box has not been applied yet at the moment this is called.
   *
   * `next` becomes BOTH what counts as written and what a later write would
   * send, so a flush after a reset is a flush with nothing to say.
   *
   * It also DISOWNS whatever write is still on the wire: its answer — an
   * adoption, a conflict retry — is about a value this editor has just stopped
   * holding, and acting on it would put the sent sentence back in the box.
   */
  reset(next: T): void;
  /**
   * RESOLVE WHEN NOTHING THIS HOOK STARTED IS STILL ON THE WIRE — WITH THE
   * VERSION THAT WRITE MADE.
   *
   * The one thing a version cannot order: a PUT already dispatched and a DELETE
   * about to be, both stating the same `If-Match`. The server takes them in
   * whatever order they arrive, so a send that deletes without waiting can have
   * its own autosave recreate the message it just sent. `await settle()` first
   * and the two are ordered by this client instead.
   *
   * AND THE ANSWER IS THE VERSION, not merely "done" (Bugbot, PR #1180).
   * Reading the version out of the map afterwards asks "what is the newest
   * number anybody has seen", and a follow-up typed during the round trip makes
   * that somebody else's record. The number this call answers with is the one
   * THE AWAITED WRITE earned, so a caller can state it and have a newer record
   * refuse them. `undefined` when nothing was written, when the write failed,
   * and when it was refused — all three mean "this client made no version", and
   * the caller falls back to what it knew before.
   *
   * Resolves immediately when nothing is in flight, and never rejects.
   */
  settle(): Promise<number | undefined>;
}

export interface AutosaveOptions {
  /** Quiet time after the last change before a write goes out. */
  delay?: number;
  /**
   * WHAT TO DO WITH A RECORD SOMEBODY ELSE EDITED FIRST (design §2).
   *
   * A write refused as stale (409) comes back with the server's own record, and
   * there are exactly two honest things to do with it. If the reader is NOT in
   * this editor, or is but has not typed since the last save, the other writer's
   * words are simply newer and better: `adopt` puts them on screen and the
   * record and the box agree again. If the reader IS mid-sentence, their words
   * win — losing what somebody is actively typing is not a trade any conflict
   * rule may make — so the hook retries ONCE against the version it has just
   * learned, and `onKept` says so out loud, because a silent overwrite of
   * somebody else's save is the other way to lose work.
   *
   * ONE RETRY, not a loop: two tabs both typing would otherwise write past each
   * other for as long as they both go on. The second refusal leaves the local
   * text where it is and the next keystroke tries again on its own debounce.
   */
  conflict?: AutosaveConflict;
}

export interface AutosaveConflict {
  /** Is the reader's caret in this editor right now? */
  focused(): boolean;
  /** What the editor is SHOWING, for the "unchanged since the last save" half
   *  of the rule. Compared against the same reading taken at the last write. */
  localText(): string;
  /** Put the server's record on screen — `null` when it was deleted. */
  adopt(record: unknown): void;
  /** Said when the local text was kept over a newer server record. */
  onKept?(): void;
}

/** A serialisation no value can produce — a stringified string always carries
 *  its quotes — so a comparison against it can only ever be "different". Used to
 *  re-arm a write that a conflict retry has to send again. */
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
 * A MOUNT ALONE NEVER WRITES: `written` is seeded with the opening value, so an
 * editor that merely opened — the New task card, a composer seeded from its
 * record — mints nothing. That is `§4`'s "mint only on intent" in one line, and
 * it is why there is no `writeInitial` any more: the Schedule hop no longer
 * arrives holding words that exist nowhere else, because the record it opens on
 * IS where they are.
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
 * caller (the module's writes swallow everything).
 *
 * `save` is read through a ref, so an inline closure (what every call site
 * passes) does not tear down and rebuild the pending write on every render.
 *
 * WHAT `save` ANSWERS IS READ, and that is the one new thing here: a
 * `DraftWrite` carrying a `conflict` is a write the server refused because
 * somebody else got there first, and the `conflict` option decides between the
 * two records — see `AutosaveOptions.conflict`.
 */
export function useAutosave<T>(
  value: T,
  save: (value: T, opts: DraftWriteOptions) => unknown,
  { delay = AUTOSAVE_DELAY_MS, conflict }: AutosaveOptions = {},
): Autosave<T> {
  const saveRef = useRef(save);
  saveRef.current = save;
  const valueRef = useRef(value);
  valueRef.current = value;
  const conflictRef = useRef(conflict);
  conflictRef.current = conflict;
  // THE VALUE AS ONE STRING, and it is what the debounce below actually depends
  // on. Every caller hands a fresh object literal each render, and a host that
  // re-renders on a poll (the chat does, every 400ms) would otherwise clear and
  // re-arm the timer forever and never write anything.
  const serial = JSON.stringify(value) ?? "";
  const written = useRef<string>(JSON.stringify(value) ?? "");
  // WHAT THE EDITOR WAS SHOWING AT THE LAST WRITE — the other half of the
  // adoption rule (`AutosaveOptions.conflict`). "The reader has not typed since
  // we last saved" is a question about the BOX, not about the serialised form
  // around it: a card whose model dropdown moved is not a card whose sentence
  // is at risk.
  const savedText = useRef<string>(conflict ? conflict.localText() : "");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // WHAT IS ON THE WIRE, and which editing life it belongs to. `settle` awaits
  // the first; `reset` bumps the second, so a write started before a send can
  // no longer adopt, retry, or otherwise speak for a box that has moved on.
  //
  // The promise carries the VERSION that write made, because that is the one
  // thing about a disowned write that still matters: the record it left behind
  // is the record the send is about to delete.
  const inflight = useRef<Promise<number | undefined>>(Promise.resolve(undefined));
  const era = useRef(0);

  const clear = () => {
    if (timer.current !== undefined) clearTimeout(timer.current);
    timer.current = undefined;
  };

  /**
   * ANSWERS THE WHOLE WRITE, retry included — which is what `settle` awaits. The
   * conflict retry starts a second request from inside the first one's `then`,
   * so it is RETURNED into that chain rather than left beside it: one promise
   * covers both attempts, and a caller that took the handle before the send can
   * never be left waiting on a write that started after it.
   */
  const writeNow = useCallback((
    opts: DraftWriteOptions,
    retry = false,
  ): Promise<number | undefined> => {
    clear();
    const next = JSON.stringify(valueRef.current) ?? "";
    if (next === written.current) return Promise.resolve(undefined);
    written.current = next;
    const rule = conflictRef.current;
    if (rule) savedText.current = rule.localText();
    const mine = era.current;
    const out = saveRef.current(valueRef.current, opts);
    if (!out || typeof (out as Promise<unknown>).then !== "function") {
      return Promise.resolve(undefined);
    }
    const chain = (out as Promise<unknown>)
      .then((res): number | undefined | Promise<number | undefined> => {
        const clash = res as DraftWrite<unknown> | undefined;
        // THE VERSION IS A FACT ABOUT THE SERVER, not about this editor's life,
        // so it survives the era check below: the send that disowned this write
        // is precisely the caller that has to know which record it left.
        const made = clash && clash.ok ? clash.version : undefined;
        // A `reset` since this went out means the box is no longer holding what
        // this write was about — the send already cleared it — so this answer
        // has nobody to speak for.
        if (era.current !== mine) return made;
        if (!clash || clash.ok || !("conflict" in clash)) return made;
        const rule2 = conflictRef.current;
        if (!rule2) return undefined;
        // NOT FOCUSED, OR NOTHING TYPED SINCE THE LAST SAVE — the other
        // writer's record is simply the newer one, and taking it is how two
        // tabs on one folder agree within a second.
        if (!rule2.focused() || rule2.localText() === savedText.current) {
          rule2.adopt(clash.conflict ?? null);
          // The adopted value is what is on screen now, so it is also what
          // counts as written: re-writing it straight back would be this
          // client winning a conflict it just conceded.
          written.current = JSON.stringify(valueRef.current) ?? "";
          savedText.current = rule2.localText();
          return undefined;
        }
        // MID-SENTENCE: the local words win, once. `write` has already taken
        // the server's version, so this second attempt states a version that
        // exists — and re-arming `written` is what lets it go out at all.
        if (retry) return undefined;
        rule2.onKept?.();
        written.current = UNWRITTEN;
        return writeNow(opts, true);
      })
      .catch((): number | undefined => {
        // `save`'s own wrapper never rejects; this only guarantees that a
        // future caller's cannot become an unhandled rejection mid-keystroke.
        return undefined;
      });
    inflight.current = chain;
    return chain;
  }, []);

  const flush = useCallback(() => {
    void writeNow({ keepalive: true });
  }, [writeNow]);
  /** Whatever is on the wire AS OF THIS CALL — a write started after it is a
   *  write about words the caller has not seen, and waiting on those is how a
   *  send would come to delete a draft typed after it. */
  const settle = useCallback(() => inflight.current, []);
  const reset = useCallback((next: T) => {
    clear();
    era.current += 1;
    // The VALUE as well as the bookkeeping — see `Autosave.reset`. Until the
    // render that empties the box arrives, `valueRef` still holds the sentence
    // that was just sent, and the unmount flush would write it back.
    valueRef.current = next;
    written.current = JSON.stringify(next) ?? "";
    const rule = conflictRef.current;
    if (rule) savedText.current = rule.localText();
  }, []);

  // The debounce. Runs on every render whose serialised value differs from what
  // was last written — the comparison is inside `writeNow`, so a timer that
  // fires on an unchanged value costs one string compare and no request.
  useEffect(() => {
    if (serial === written.current) return;
    clear();
    timer.current = setTimeout(() => {
      void writeNow({});
    }, delay);
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
      // another unmounts the composer without any window event at all.
      flush();
    };
  }, [flush]);

  return { flush, reset, settle };
}
