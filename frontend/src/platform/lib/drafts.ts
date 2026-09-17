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

/**
 * THE CONVERSATION A CHAT KEY NAMES, or `""` when it names none — `newChatFile`
 * read from the other end, and the whole of "the key IS the session" said once.
 *
 * A chat record is filed under the session id once the conversation exists and
 * under `new:<file>` before it does (`chatDraftKey`), so a key without the
 * prefix is a session and a key with it is a folder somebody has not talked to
 * yet. Two readers ask this — the New task card the Schedule hop opens, which
 * has to say WHICH thread its message is going into, and the Tasks page's
 * source-task chip — and the answer has to be the same one: a card that reads
 * the session as "" schedules a standalone task beside the conversation it was
 * supposed to continue, which is one task listed twice as far as the reader is
 * concerned (Akshil, 2026-09-17).
 */
export function chatKeySession(key: string): string {
  if (!key || key.startsWith(NEW_CHAT_PREFIX)) return "";
  return key;
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

// -- ONE STRING, TWO FIELDS: the prose convention both editors share ---------
//
// MOVED DOWN HERE FROM `shell/NewJobModal` (2026-09-16) and re-exported from it,
// because the chat composer needs the same rule and an app may not import the
// shell (`scripts/check-boundaries.mjs`). A composer's box is one block of
// prose; the New task card has a Title and a description; a task draft born in
// the composer has to be filed as the card's two fields. Two copies of that cut
// would be two answers to "what is this task called", so there is one.
// A draft arriving from the chat composer's Schedule button
// (`?new=1&message=…`) is one block of prose written for Claude, and the form
// now has two places to put it. It is SPLIT rather than dropped whole into the
// description (Akshil, 2026-08-18): the first line is what the draft is about,
// which is exactly what a title is, and the rest is the body.
//
// This is NOT the bug of 2026-08-17 coming back. That one prefilled Title with
// `firstLine(ask)` while the SAME text also filled the description — the message
// arrived duplicated into both fields, and the task ended up named after its own
// body. Here the two fields PARTITION the draft: what goes in the first field is
// removed from the second, and composeTaskMessage puts it back together on Save,
// so nothing is said twice and nothing is lost.
//
// THE LINE BREAK IS THE ONLY CUT (Akshil, 2026-08-18). A long first line is kept
// whole rather than clamped to a name: the field asks "What should Claude do?",
// and a clamp answers that question with two thirds of a sentence. The clamp
// that was here also had to keep the draft ENTIRE in the description to avoid
// losing the tail, so a long draft arrived with its opening said twice — worse
// than the long value it was avoiding. TITLE_MAX still governs a name DERIVED
// from a session's first message (shortTitle), which is a different job: that is
// the app naming a thread nobody named, where a clamp is all there is. Here the
// user wrote the line, and the field is theirs to shorten.
export function splitDraft(draft?: string | null): {
  title: string;
  description: string;
} {
  const text = (draft ?? "").trim();
  if (!text) return { title: "", description: "" };
  const brk = text.indexOf("\n");
  return {
    title: (brk < 0 ? text : text.slice(0, brk)).trim(),
    description: brk < 0 ? "" : text.slice(brk + 1).trim(),
  };
}

/**
 * `splitDraft` RUN BACKWARDS — the card's two fields put back into the one
 * block of prose the composer was holding (design.md, Round 2: "'Back to chat'
 * reverses it").
 *
 * The hop split a sentence across Title and the description; going back has to
 * hand the composer one string again. Title line, blank line, body — the same
 * shape the composer's own text had when it left, so `splitDraft` on the way
 * out again lands on the same two fields.
 *
 * EITHER HALF ALONE IS JUST THAT HALF, with no separator to show for the one
 * that is missing: a card whose title was cleared must not come back as a
 * message opening on two blank lines, and one with nothing but a title must not
 * come back with a trailing gap (Akshil, 2026-09-11).
 */
export function joinDraft(title?: string | null, description?: string | null): string {
  const head = (title ?? "").trim();
  const body = (description ?? "").trim();
  if (!head) return body;
  if (!body) return head;
  return `${head}\n\n${body}`;
}

/**
 * A SESSION-LESS COMPOSER'S BOX, AS AN UPCOMING TASK DRAFT (Akshil, 2026-09-16).
 *
 * A chat that has never been sent is not a conversation with an unsent message
 * in it — it is a thing the reader has not started yet. So "Save as draft" and
 * the Schedule hop out of such a box mint a TASK draft, `draft:<id>`, exactly
 * the record the "+ New task" card mints: a new one every time, listed in
 * Upcoming under its own TASK number, opened in the card from every surface.
 *
 * The shape that used to be written instead was `new:<file>` — ONE record per
 * folder — so the second draft out of the same folder silently replaced the
 * first (Akshil, 2026-09-16: not intended). `new:<file>` is still READ
 * everywhere it was; nothing writes one any more.
 *
 * WHAT IS AND IS NOT STATED. The words (cut into the card's two fields by
 * `splitDraft`), the files (already copied into the task-shots dir by the
 * caller — a chat attachment's own path is a tempdir on a 12 h TTL) and the
 * folder. Every setting is left UNANSWERED — `when`/`repeat`/`new_task_each_run`
 * null, the three pills "" — because a composer has no opinion about a time, a
 * model or a permission mode, and null is the card's own word for "nobody has
 * said" (`TaskDraftForm`). `session_id` is "" for the same reason it is on a
 * card-born draft: there is no thread to send this into.
 */
export function composerTaskDraft(
  text: string,
  target: string,
  attachments: DraftAttachment[] = [],
): TaskDraftForm {
  const split = splitDraft(text);
  return {
    title: split.title,
    description: split.description,
    target,
    when: null,
    repeat: null,
    model: "",
    effort: "",
    permission: "",
    attachments,
    new_task_each_run: null,
    session_id: "",
    custom_rule: null,
  };
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
  /**
   * STATE NO VERSION AT ALL — an unconditional write, which the contract reads
   * as "whatever is there, this is what it should hold" (contract §2).
   *
   * The ONE caller is the keepalive flush that bypasses the one-in-flight rule
   * (see the syncer's header). Its `If-Match` would be a number read BEFORE the
   * request it just overtook landed, so the ordinary PUT landing first turns the
   * document's last word into a 409 — and a 409's retry is an ordinary request
   * that dies with the document. This page already orders itself with `seq`, so
   * the keepalive has a way to say "the newest one wins" that does not depend on
   * a number it cannot have. Every other write still states its version, so
   * `If-Match` goes on arbitrating between DOCUMENTS exactly as before.
   */
  unconditional?: boolean;
  /**
   * WHERE THIS REQUEST SITS IN ITS PAGE'S OWN QUEUE FOR THIS KEY — the other
   * half of the ordering story, and the half a version cannot tell (contract
   * `drafts-seq-contract.md`).
   *
   * A version orders THIS page against ANOTHER one: a write stating a number
   * somebody has already moved past is refused. It says nothing about two of
   * this page's own requests, because both of them state the number this page
   * read, and which lands second is the network's decision. So every request
   * the syncer dispatches carries a counter that only goes up, and the server
   * drops one that is not newer than the last it applied from this same page
   * (200 `{dropped: true}`).
   *
   * Sent with the page's own `client` id, which `write` adds: the pair is what
   * makes the counter meaningful, and one without the other is meaningless.
   */
  seq?: number;
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

/**
 * THIS DOCUMENT'S OWN ID, minted once when this module loads.
 *
 * It names the PAGE, not the user and not the tab's contents: what it is for is
 * the server telling "a request from the page that has already sent me
 * something newer" apart from "a request from the other window". The first is
 * dropped, the second is arbitrated by versions, and a shared id would collapse
 * the two into one wrong answer.
 *
 * `crypto.randomUUID` with the same fallback `newTaskDraftId` uses, and for the
 * same reason: it is absent over plain http on some older builds, and a throw
 * here would take the whole module down at import time.
 */
const CLIENT_ID = newTaskDraftId();

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
   * THE SERVER DROPPED THIS WRITE AS A STRAGGLER — not an error, and nothing
   * for the caller to do (contract: 200 `{ok: true, dropped: true}`).
   *
   * It means this page had already sent a NEWER request for the same key, so
   * what the record holds is what this page wanted; this one simply arrived
   * late. `ok` stays true — the desired state is on the server — but the body
   * says nothing about the record this write did not make, so no version is
   * read out of it.
   */
  dropped?: boolean;
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
  // THE CALLER'S VERSION OUTRANKS THE MAP'S (see `DraftWriteOptions.ifMatch`),
  // and a caller saying it holds no useful version outranks both.
  const seen = opts?.unconditional ? undefined : opts?.ifMatch ?? versions.get(key);
  // THE SEQUENCE RIDES IN THE BODY, on a DELETE as much as on a PUT — a delete
  // that a straggling PUT can outlive is precisely the resurrection this pair
  // exists to stop, so a bodiless DELETE grows one here.
  const payload = opts?.seq === undefined
    ? body
    : { ...(body === undefined ? {} : (body as object)), client: CLIENT_ID, seq: opts.seq };
  try {
    const res = await fetch(url, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-Fused": "1",
        ...(seen === undefined ? {} : { "If-Match": String(seen) }),
      },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
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
    // A DROPPED WRITE IS READ BEFORE ANYTHING ELSE AND ITS BODY IS NOT READ AT
    // ALL. The record in it is the one a NEWER request from this page left (or
    // is about to leave), so taking a version off it would be this client
    // learning a number from a write it did not make.
    if ((answer as { dropped?: unknown } | null)?.dropped === true) {
      return { ok: true, dropped: true };
    }
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

// ---------------------------------------------------------------- the syncer
//
// ONE WRITER PER DRAFT KEY, AND IT OWNS THE ORDER.
//
// What this replaces is a page on which seven things wrote one draft — a 600 ms
// debounce, a window blur, a tab going hidden, an unmount, the send's DELETE, a
// Discard, the Schedule hop's PUT — each firing its own request, with the
// CALLERS trying to put them in order between them (`settle`, a flush chained
// behind whatever was on the wire, an `era` counter, an `inflight` promise, "the
// hop waits for the autosave"). Nine bugs came out of that layer and every fix
// added another rung to it, because the shape was wrong: ordering is not
// something six callers can each get right.
//
// So there are no writers any more, only STATEMENTS OF INTENT. `setText` says
// what the record should hold; `markDeleted` says it should not exist. The
// syncer holds the latest such statement (`desired`) and has at most ONE request
// in flight; when that request settles and the desired state has moved on, it
// sends again. Every request carries the WHOLE state — never a delta — so the
// last one to land is right by construction, and the reader's last keystroke
// always wins over anything still in the air.
//
// THREE THINGS KEEP IT HONEST ACROSS A NETWORK THAT REORDERS:
//
//   * `If-Match`: a write against a version somebody else has moved past is
//     refused (409), which is how TWO DOCUMENTS are arbitrated;
//   * `seq`: a monotonic counter per key per document, dropped server-side when
//     it is not newer than the last one applied, which is how ONE DOCUMENT is
//     ordered against itself — the one thing a version cannot do, since both of
//     this page's requests state the same version;
//   * the one-in-flight rule, which means those two only ever have to catch the
//     one case that bypasses it: the keepalive flush.
//
// THE KEEPALIVE FLUSH IS THE DELIBERATE EXCEPTION (Bugbot 4026181414, PR #1180).
// `pagehide` is the last moment anything can leave this document, and a flush
// that QUEUED itself behind an in-flight PUT never left at all: the browser
// cancels the non-keepalive request it was waiting for, and the queued one is
// never dispatched, so the last thing the reader typed dies with the tab. So a
// keepalive flush bypasses the one-in-flight rule and goes immediately, with the
// newest state and a higher `seq`. Two requests are then on the wire at once and
// the older one is harmless whichever order they arrive in — the server drops
// it, because its sequence is not the newest this page has sent.

/**
 * THE EDITOR'S ANSWER TO "SOMEBODY ELSE WROTE THIS RECORD FIRST" (design §2).
 *
 * A write refused as stale comes back with the server's own record, and there
 * are exactly two honest things to do with it. If the reader is NOT in this
 * editor, or is but has not typed since the last save, the other writer's words
 * are simply newer and better: `adopt` puts them on screen. If the reader IS
 * mid-sentence, their words win — losing what somebody is actively typing is
 * not a trade any conflict rule may make — so the state is sent again ONCE
 * against the version just learned, and `onKept` says so out loud.
 *
 * ONE RETRY, not a loop: two tabs both typing would otherwise write past each
 * other for as long as they both go on.
 */
export interface DraftConflictRule {
  /** Is the reader's caret in this editor right now? */
  focused(): boolean;
  /** What the editor is SHOWING, for the "unchanged since the last save" half
   *  of the rule. Compared against the same reading taken at dispatch. */
  localText(): string;
  /** Put the server's record on screen — `null` when it was deleted. */
  adopt(record: unknown): void;
  /** Said when the local text was kept over a newer, non-empty server record. */
  onKept?(): void;
  /**
   * THE ID A TASK WRITE ACTUALLY LANDED ON, when it was not the one it named
   * (`saveTaskDraft`, Bugbot PR #1126: the store folds a write into the draft
   * that already holds this session). The card has to adopt it — every later
   * call names the draft by id — and the editor is the only thing that can,
   * since it is the one holding the id.
   */
  onTaskId?(id: string): void;
}

/** What `handoff` answers: whether the server now holds what was asked for, and
 *  the version it holds it at. */
export interface DraftHandoff {
  ok: boolean;
  version?: number;
  /**
   * THE DELETE'S OWN OUTCOME, which is not the same question as `ok`.
   *
   * `ok` is about the DESIRED state — "does the server hold what this page last
   * asked for" — and a keystroke arriving behind the List while the trash's
   * DELETE is on the wire moves that state on to a PUT. The row's answer is
   * about the RECORD the reader pressed the trash on: it was removed, or it was
   * not. Asking `ok` put the row back although the delete had landed.
   */
  removed: boolean;
}

interface ChatDesired {
  kind: "chat";
  text: string;
  attachments: DraftAttachment[];
  form?: ChatDraftForm;
}

interface TaskDesired {
  kind: "task";
  form: TaskDraftForm;
}

interface GoneDesired {
  kind: "gone";
}

type Desired = ChatDesired | TaskDesired | GoneDesired;

/**
 * HOW A STATEMENT IS TIMED. `defer: true` records what is wanted but starts no
 * autosave timer: the write goes out on the next `flushNow` — a window blur, a
 * `pagehide`, an unmount, a swap — and never 600 ms after a keystroke. The
 * Explorer's landing composer saves this way (Akshil, 2026-09-17: "we save
 * drafts when the page goes out of focus").
 */
export interface DraftStateOptions {
  defer?: boolean;
}

export interface DraftSyncer {
  /** The key this syncer is the writer for. */
  readonly key: string;
  /** THE RECORD SHOULD HOLD THIS. The whole state every time — words, files and
   *  (for the card that owns them) the settings — because a request carries the
   *  whole state and a partial statement would be a delta by another name. */
  setText(
    text: string,
    attachments?: readonly DraftAttachment[],
    form?: ChatDraftForm,
    opts?: DraftStateOptions,
  ): void;
  /** …and the task-draft shape of the same sentence, for a card with no chat
   *  behind it (`draft:<id>`). */
  setTask(form: TaskDraftForm, opts?: DraftStateOptions): void;
  /** THE SERVER ALREADY HOLDS THIS — seeding an editor from a record, or taking
   *  one the change feed pushed. Sets what is wanted AND what is believed to be
   *  stored, so a box that merely filled writes nothing. */
  seedText(text: string, attachments?: readonly DraftAttachment[]): void;
  /** `seedText` for a TASK record: what the Explorer composer holds when it
   *  opens on an Upcoming draft it read off the listing row (`form`). */
  seedTask(form: TaskDraftForm): void;
  /** NOTHING IS WANTED AFTER ALL — for a key this page has never written: the
   *  held composer emptied before its first save, so there is no record to
   *  delete and nothing to state. A no-op while a request is on the wire. */
  unwant(): void;
  /** THE RECORD SHOULD NOT EXIST. The send, and every Discard. Goes out at once
   *  rather than on the debounce — but still behind whatever is in flight. */
  markDeleted(): void;
  /** Send what is pending NOW. `keepalive` also lets the request outlive the
   *  document, and is the one thing that may pass an in-flight request. */
  flushNow(opts?: { keepalive?: boolean }): void;
  /** RESOLVE ONCE THE SERVER MATCHES THE DESIRED STATE — the Schedule hop, which
   *  cannot navigate to a card that seeds from a record this page has not
   *  finished writing. Never rejects. */
  handoff(): Promise<DraftHandoff>;
  /** STOP WANTING ANYTHING — the record is out of this page's hands (the
   *  Schedule that turns a draft into a task: the server deletes it as part of
   *  creating the entry, so a pending write would put it straight back). Unlike
   *  `markDeleted` this asks for nothing; it forgets. */
  forget(): void;
  /**
   * WHAT THIS PAGE IS ASKING THE RECORD TO HOLD, or `undefined` when it is
   * asking for nothing — or for something that is not a chat's words (a task
   * form, a delete).
   *
   * Read by the one caller that cannot get its answer from the server: the
   * Schedule hop, which has to know whether its own statement about the
   * attachments is still the latest one this page has made.
   */
  wants(): { text: string; attachments: DraftAttachment[] } | undefined;
  /**
   * DOES THIS PAGE BELIEVE THERE IS NO RECORD UNDER THIS KEY — its own delete
   * has landed, or one is on its way out (a Send, a Discard, the trash).
   *
   * Read by an editor holding an answer that PREDATES that delete: a draft GET
   * dispatched before the Send and answering after it names a record this page
   * has since spent, and painting those words back is a sentence arriving out of
   * nowhere (Bugbot 4027549698). The syncer is the one thing that can say so,
   * because it is the one thing that said it.
   */
  isGone(): boolean;
  /** Register the editor's conflict rule; the answer detaches it. Several
   *  editors may be open on one key (the composer and the New task card), so
   *  these stack: the NEWEST one decides, and detaching one restores the one
   *  under it. */
  watch(rule: DraftConflictRule): () => void;
}

/**
 * ONE SYNCER PER KEY PER DOCUMENT, and it OUTLIVES THE EDITORS.
 *
 * Module scope for the same reason the version map is: two editors can be open
 * on one record in one document (the composer and the New task card, on the
 * same chat key), and two writers for one key is the thing this design removes.
 * It also means an unmount needs no flush of its own — the debounce belongs to
 * the key, not to the component, so leaving a chat does not have to race the
 * 600 ms it was in the middle of.
 */
const syncers = new Map<string, InnerSyncer>();

/**
 * …AND THE SEQUENCE OUTLIVES THE SYNCER, because it is a fact about this
 * DOCUMENT and this KEY, not about the object that happens to be writing them
 * (contract `drafts-seq-contract.md`: "one `seq` counter per KEY per document").
 *
 * A syncer with nothing left to say drops out of the registry (`sweep`), and
 * the next ask mints a fresh one. If the counter went with it, that fresh one
 * would start at 1 again under the SAME client id — and the server drops a
 * write whose `seq` is not newer than the last it applied from that client, so
 * every write after the first sweep would be silently discarded as a straggler.
 */
const seqs = new Map<string, number>();

/** The syncer for this key, made on first ask.
 *
 *  AND THE PLACE THE UNLOAD LISTENERS ARE ARMED. They used to be armed by
 *  `useAutosave`'s mount, which is one editor's effect: a document whose only
 *  writer is something else — the List's trash reaching for `peekDraftSyncer`,
 *  a card that never mounted a composer — had no `pagehide` flush at all. A
 *  syncer existing is exactly the condition under which the three moments
 *  matter, so that is when they are listened for. */
export function draftSyncer(key: string): DraftSyncer {
  let found = syncers.get(key);
  if (!found) {
    found = makeSyncer(key);
    syncers.set(key, found);
  }
  listen();
  return found;
}

/** …and the one that already exists, or nothing. Asked by a caller that has
 *  something to say about a key only IF this document is writing it — the
 *  List's trash, which otherwise simply DELETEs (`discardDraft`). */
export function peekDraftSyncer(key: string): DraftSyncer | undefined {
  return syncers.get(key);
}

/** Forget every syncer. For tests, which build a document per case; nothing in
 *  the app has any business dropping a pending write. */
export function resetDraftSyncers(): void {
  for (const sync of syncers.values()) sync.cancel();
  syncers.clear();
  seqs.clear();
}

/** design.md: 600 ms after the last keystroke. Long enough that a sentence is
 *  one write, short enough that a reader who types and immediately closes the
 *  tab is covered by the flush rather than by the timer. */
export const AUTOSAVE_DELAY_MS = 600;

interface InnerSyncer extends DraftSyncer {
  /** Drop the timer and stop caring about answers — tests only. */
  cancel(): void;
}

/** The key a TASK draft is sequenced and versioned under, run backwards: the id
 *  the routes take. `""` for a chat key, which is the test. */
function taskIdOf(key: string): string {
  return key.startsWith("draft:") ? key.slice("draft:".length) : "";
}

/** Two states are the same state when they serialise the same. Built field by
 *  field rather than handed to `JSON.stringify` whole, so a form the caller
 *  spelled in another order is not a change. */
function serialOf(state: Desired): string {
  // `gone:` and not a raw NUL: the sentinel only has to be a string no chat
  // and no form can spell, and a literal control byte in the source made every
  // `grep` over this file answer "Binary file drafts.ts matches" instead of the
  // line it was asked for.
  if (state.kind === "gone") return "gone:";
  if (state.kind === "task") return "task:" + stable(state.form as unknown);
  return "chat:" + stable({
    text: state.text,
    attachments: state.attachments,
    form: state.form ?? null,
  });
}

function stable(value: unknown): string {
  return JSON.stringify(value, (_k, v) => {
    if (!v || typeof v !== "object" || Array.isArray(v)) return v;
    const row = v as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(row).sort()) out[k] = row[k];
    return out;
  }) ?? "";
}

function makeSyncer(key: string): InnerSyncer {
  // WHAT THE RECORD SHOULD HOLD, and what this page believes it does hold. The
  // gap between the two is the only thing that ever makes a request.
  let desired: Desired | undefined;
  /** The last statement asked to wait for a flush (`DraftStateOptions.defer`). */
  let deferred = false;
  let known: string | undefined;
  // HOW MANY REQUESTS ARE OUT. Normally one at most; two only across a keepalive
  // flush, which is allowed to pass (see the header).
  let out = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  /** The highest `seq` whose answer has been taken. A slower earlier request
   *  answering after a faster later one must not teach this page anything. */
  let applied = 0;
  /** The version the last landed write of this page's made — what `handoff`
   *  answers with, and what the hop states. */
  let made: number | undefined;
  /** Was the last completed request a success? */
  let landedOk = true;
  /** Send again the moment the wire clears, rather than on the debounce — set by
   *  every gesture that is not a keystroke, and cleared once the server has
   *  caught up. */
  let urgent = false;
  /** ONE conflict retry per desired state (see `DraftConflictRule`). */
  let retried = false;
  /** Twice refused: stop sending until somebody says something new. Without it
   *  two tabs in a 409 loop would write past each other for ever. */
  let stalled = false;
  /** What the editor was showing when the request in flight was dispatched. */
  let dispatchedText = "";
  /**
   * EVERY EDITOR OPEN ON THIS KEY, newest last.
   *
   * One slot was wrong because two editors really are open at once: the New
   * task card hops out of a composer that stays mounted behind it, on the same
   * chat key. The card's `watch` replaced the box's rule and its unwatch left
   * the slot EMPTY, so from then on a 409 in that chat retried instead of
   * adopting and the box never heard that its record had changed elsewhere.
   * Newest wins — the card is what the reader is looking at — and detaching one
   * restores the one under it.
   */
  const rules = new Set<DraftConflictRule>();
  /**
   * THE RECORD IS BELIEVED GONE — the answer the trash waits for, kept apart
   * from `landedOk` because a keystroke behind the List moves the desired state
   * on to a PUT while the DELETE is still out (`DraftHandoff.removed`).
   */
  let removed = false;
  /**
   * HOW MANY DELIBERATE HANDOFFS ARE WAITING — Continue, a Discard, the trash.
   *
   * While one is outstanding a 409 MAY NOT ADOPT. `handoff` is a gesture the
   * reader made about THESE words, and Continue blurs the box on its way out,
   * so the ordinary "nobody is focused here, take the newer record" rule would
   * quietly replace the sentence being scheduled with the other tab's draft and
   * then report failure (Bugbot 4026812608). A gesture states itself once more
   * against the version just learned, exactly as a mid-sentence reader does.
   */
  let handing = 0;
  const waiting: Array<(answer: DraftHandoff) => void> = [];

  const dirty = () => desired !== undefined && serialOf(desired) !== known;
  /** The editor that decides, or nothing. */
  const editorOf = (): DraftConflictRule | undefined => {
    let last: DraftConflictRule | undefined;
    for (const one of rules) last = one;
    return last;
  };

  const clearTimer = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  };

  /**
   * DROP THIS SYNCER FROM THE REGISTRY when it has nothing left to say and
   * nobody left to say it to.
   *
   * The map is module scope and used to grow for ever: one entry per chat key
   * this document ever touched, each holding a desired state and a conflict
   * rule, for the life of the page. A syncer with no pending write, no request
   * out, no editor watching and nobody waiting is a syncer that is exactly as
   * useful as the one `draftSyncer` would mint on the next ask — and the
   * versions it needs to state live in their own map, which outlives it.
   */
  const sweep = () => {
    if (dirty() || out > 0 || timer !== undefined) return;
    if (rules.size || waiting.length) return;
    if (syncers.get(key) === api) syncers.delete(key);
  };

  /** Resolve the hop's waiters — only once the server holds what is wanted and
   *  nothing is still on the wire. */
  const settleWaiters = () => {
    if (!waiting.length) return;
    if (out > 0) return;
    if (dirty() && !stalled) return;
    const answer: DraftHandoff = {
      ok: landedOk && !dirty(),
      removed,
      ...(() => {
        const version = made ?? draftVersion(key);
        return version === undefined ? {} : { version };
      })(),
    };
    handing -= waiting.length;
    if (handing < 0) handing = 0;
    for (const resolve of waiting.splice(0)) resolve(answer);
  };

  /** Somebody changed what is wanted: a stall is over and the single retry is
   *  spent on the new state rather than the old one. */
  const wanted = (next: Desired, urgently: boolean, defer = false) => {
    const serial = serialOf(next);
    if (desired !== undefined && serialOf(desired) === serial && !urgently) return;
    desired = next;
    deferred = defer && !urgently;
    stalled = false;
    retried = false;
    // A NEW STATEMENT UNSAYS THE LAST DELETE, whether it is another delete (the
    // answer is about to be made again) or the words a keystroke put back —
    // UNLESS THE RECORD IS ALREADY KNOWN GONE (Bugbot 4027177439). A second
    // trash press, or one on a chat this page already deleted (a Send that
    // just cleared it, a lingering Recent chats row), asks for exactly the
    // state `known` already names. "Already absent" IS the outcome the trash
    // wanted, so the handoff says `removed: true` without another DELETE
    // going out — `dirty()` below is false for the same reason, so nothing
    // is dispatched.
    removed = next.kind === "gone" && known === serial;
    // A KEYSTROKE ENDS AN URGENCY. A blur that could not send (something was in
    // flight) asked for "as soon as the wire clears"; the reader typing again
    // is the reader still writing, and the answer to that is the debounce.
    urgent = urgently;
    if (!dirty()) {
      clearTimer();
      settleWaiters();
      return;
    }
    if (urgently) {
      pump({});
      return;
    }
    clearTimer();
    if (deferred) return;
    timer = setTimeout(() => pump({}), AUTOSAVE_DELAY_MS);
  };

  function dispatch(sending: Desired, mine: number, keepalive: boolean, bypass: boolean) {
    const opts: DraftWriteOptions = {
      seq: mine,
      ...(keepalive ? { keepalive: true } : {}),
      // THE ONE REQUEST THAT STATES NO VERSION (`DraftWriteOptions.unconditional`).
      // A keepalive flush that overtook a request still on the wire holds a
      // number read BEFORE that request landed, so the ordinary PUT arriving
      // first would turn the document's last word into a 409 whose retry is an
      // ordinary request — and an ordinary request dies with the document. Both
      // orders are safe without it: the older one is dropped for its `seq` when
      // this one wins the race, and this one is applied when it loses.
      ...(bypass ? { unconditional: true } : {}),
    };
    const seen = bypass ? undefined : draftVersion(key);
    const ident = taskIdOf(key);
    if (sending.kind === "gone") {
      // NO `If-Match: 0` ON A DELETE. Zero means "I expect no record", and a
      // delete that refuses to run because the record exists would be the trash
      // not working. A version this page HOLDS is still stated — that one is the
      // promise not to spend somebody else's newer words.
      const del = seen === undefined ? opts : { ...opts, ifMatch: seen };
      return ident ? deleteTaskDraft(ident, del) : deleteChatDraft(key, del);
    }
    // …AND `If-Match: 0` ON THE FIRST WRITE OF A KEY THIS DOCUMENT HAS NEVER
    // SEEN A RECORD FOR (contract §2). Unconditional would mean a page that
    // failed to read the store — offline for a second at mount — silently
    // overwriting a draft it never saw. Zero says what it believes: there is
    // nothing here. A 409 then hands it the record, and the rule below decides.
    const put = bypass ? opts : { ...opts, ifMatch: seen ?? 0 };
    if (sending.kind === "task") return saveTaskDraft(ident, sending.form, put);
    return saveChatDraft(key, sending.text, sending.attachments, put, sending.form);
  }

  function pump(opts: { keepalive?: boolean }) {
    clearTimer();
    if (stalled || !dirty()) {
      settleWaiters();
      return;
    }
    // ONE AT A TIME — except a keepalive flush, which is the document's last
    // word and may not wait for anything (see the header).
    if (out > 0 && !opts.keepalive) return;
    // …AND THE FLUSH THAT DID PASS ONE IS THE FLUSH THAT MAY NOT STATE A
    // VERSION (`dispatch`). Only this case: a keepalive flush with the wire
    // clear holds a number nothing can have moved past yet.
    const bypass = !!opts.keepalive && out > 0;
    const sending = desired as Desired;
    const serial = serialOf(sending);
    // …and it comes out of the DOCUMENT's counter for this key, not this
    // object's, so a syncer that was swept and re-made goes on counting up
    // (see `seqs`).
    const mine = (seqs.get(key) ?? 0) + 1;
    seqs.set(key, mine);
    const editor = editorOf();
    dispatchedText = editor ? editor.localText() : "";
    out += 1;
    void Promise.resolve(dispatch(sending, mine, !!opts.keepalive, bypass))
      .then((answer) => {
        const res = answer as DraftWrite<unknown> | undefined;
        out -= 1;
        if (!res) {
          landedOk = false;
        } else if (res.dropped) {
          // This page has already said something newer; there is nothing to
          // learn and nothing to redo.
          landedOk = true;
          // …BUT A DROPPED DELETE STILL DELETED (Bugbot 4027177439). The server
          // drops a request whose `seq` is not the newest this page has sent,
          // and one of the ways that happens is the trash's DELETE arriving
          // behind this page's own newer statement about the same key — the
          // record the reader pressed the trash on IS gone either way. Reported
          // as "not removed", `dropDraft` put the row straight back on the List.
          // Guarded by `mine > applied` like every other thing learned here, so
          // a straggler cannot un-say a later PUT's answer.
          if (sending.kind === "gone" && mine > applied) removed = true;
        } else if (res.ok) {
          landedOk = true;
          if (mine > applied) {
            applied = mine;
            known = serial;
            made = sending.kind === "gone" ? undefined : res.version;
            // THE TRASH'S OWN ANSWER (`DraftHandoff.removed`): this delete is
            // the newest thing this page has heard about, and the record it
            // named is not there any more.
            if (sending.kind === "gone") removed = true;
          }
          // A TASK WRITE MAY HAVE LANDED ON ANOTHER ID (`saveTaskDraft`). The
          // version this page now holds belongs to THAT key, and the card has to
          // be told, or its next save, its Discard and its Schedule all aim at a
          // record that is not there.
          const landedOn = sending.kind === "task"
            ? (res as { id?: string }).id ?? ""
            : "";
          if (landedOn && landedOn !== taskIdOf(key)) {
            rememberDraftVersion(taskDraftKey(landedOn), res.version);
            editorOf()?.onTaskId?.(landedOn);
          }
        } else if ("conflict" in res) {
          // THE SAME `mine > applied` GUARD THE SUCCESS ABOVE HAS, and for the
          // same reason. A 409 can answer after a LATER request of this page's
          // already landed — the keepalive pair puts two on the wire on purpose
          // — and the record in that refusal predates the one this page has
          // since written. Resolving on it rolls the box back to a state
          // nobody is in any more.
          if (mine > applied) {
            applied = mine;
            landedOk = false;
            resolve(res.conflict ?? null, sending);
          }
        } else {
          // Offline, a 500, the document unloading mid-flight. Nothing is said
          // and nothing is lost: the state is still WANTED, so the next change,
          // the next flush and the next pagehide all send it again.
          //
          // AND NOTHING IS SENT AGAIN ON ITS OWN. Re-dispatching the moment a
          // failure comes back would be a hot loop against a server that is
          // down — one request per round trip, for as long as it stays down —
          // and the write that would fix it is the one the reader has not made
          // yet. So this stalls, exactly as a second refusal does, and any
          // statement about this key starts it again.
          landedOk = false;
          stalled = true;
        }
        after();
      })
      .catch(() => {
        out -= 1;
        landedOk = false;
        after();
      });
  }

  /** Where the next request is decided, and the only place it is. */
  function after() {
    if (!dirty() || stalled) {
      urgent = false;
      settleWaiters();
      sweep();
      return;
    }
    if (out > 0) return; // the other request in flight will call this again
    if (urgent) {
      pump({});
      return;
    }
    // Still typing: the newest state goes out on its own debounce rather than
    // one request per round trip.
    clearTimer();
    if (deferred) return;
    timer = setTimeout(() => pump({}), AUTOSAVE_DELAY_MS);
  }

  /**
   * A 409 — and the version is already adopted by `write` before this runs, so
   * whatever is decided here, the next request states a number that exists.
   */
  function resolve(record: unknown, sent: Desired) {
    const rec = record as ChatDraft | null;
    // IS THERE ANYTHING ON THE OTHER SIDE TO LOSE? A record that is gone, or the
    // WORDLESS one a chat keeps while its Schedule form lives on (contract §2),
    // is not somebody's sentence — so keeping the local text over it costs
    // nobody anything, and a toast about it would be a toast about nothing. A
    // task record is judged whole: its fields are not a box anybody is typing
    // one word at a time into.
    const theirs = sent.kind === "task"
      ? !!record
      : !!rec && (!!`${rec.text ?? ""}`.trim() || !!rec.attachments?.length);
    const editor = editorOf();
    const again = (): void => {
      // State it once more against the version just learned — `write` has
      // already taken it — and stop after that, because two tabs both retrying
      // would write past each other for as long as they both go on.
      if (retried) {
        stalled = true;
        return;
      }
      retried = true;
      urgent = true;
    };
    // NOTHING TO LOSE, or nothing in this document holding the record (the
    // List's trash, the hop): the gesture stands, once.
    if (!theirs || !editor) {
      again();
      return;
    }
    // A DELIBERATE GESTURE IS NEVER ADOPTED OVER (Bugbot 4026812608). Continue,
    // a Discard and the trash all wait on `handoff`, and Continue BLURS the box
    // on its way out — so the "nobody is focused here, their record is simply
    // newer" rule below would take the other tab's draft over the very words
    // the reader asked to schedule, and then report the hop as failed. A
    // gesture states itself once more, exactly as a mid-sentence reader does.
    if (handing > 0) {
      again();
      return;
    }
    // NOT FOCUSED, OR NOTHING TYPED SINCE THE DISPATCH — the other writer's
    // record is simply the newer one, and taking it is how two tabs on one
    // folder agree within a second.
    if (!editor.focused() || editor.localText() === dispatchedText) {
      editor.adopt(rec);
      // What is on screen now is what the server holds, so it is also what is
      // wanted: re-writing it straight back would be this page winning a
      // conflict it has just conceded.
      if (rec && sent.kind !== "task") {
        desired = {
          kind: "chat",
          text: rec.text ?? "",
          attachments: rec.attachments ?? [],
        };
        known = serialOf(desired);
      } else {
        desired = undefined;
        known = undefined;
      }
      retried = false;
      stalled = false;
      return;
    }
    // MID-SENTENCE: the local words win, once, and the reader is told.
    again();
    if (!stalled) editor.onKept?.();
  }

  const api: InnerSyncer = {
    key,
    setText(text, attachments = [], form, opts) {
      wanted(
        {
          kind: "chat",
          text,
          attachments: attachments.slice(),
          ...(form === undefined ? {} : { form }),
        },
        false,
        !!opts?.defer,
      );
    },
    setTask(form, opts) {
      wanted({ kind: "task", form }, false, !!opts?.defer);
    },
    seedText(text, attachments = []) {
      // A SEED IS NEWS ABOUT THE SERVER, AND STALE NEWS LOSES TO WHAT THIS PAGE
      // IS STILL WRITING (Bugbot 4026812625).
      //
      // It says "the record holds this" — and it was believed even while a PUT
      // of newer words was on the wire, or a keystroke was sitting in the
      // debounce. Both ways round it cost the last sentence: the in-flight PUT
      // landing wrote `known` back to the serial it had DISPATCHED, which made
      // the seeded state look dirty and sent it over the newer record; and an
      // editor reopening inside the 600 ms seeded from a GET that predated the
      // keystrokes and threw them away. Nothing here is a statement of intent,
      // so there is nothing to lose by declining: the pending write is still
      // wanted, still goes, and its own answer is what teaches this page what
      // the record holds.
      if (dirty() || out > 0) return;
      clearTimer();
      desired = { kind: "chat", text, attachments: attachments.slice() };
      known = serialOf(desired);
      retried = false;
      stalled = false;
      settleWaiters();
    },
    unwant() {
      if (out > 0 || known !== undefined) return;
      clearTimer();
      desired = undefined;
      deferred = false;
    },
    seedTask(form) {
      if (dirty() || out > 0) return;
      clearTimer();
      desired = { kind: "task", form };
      known = serialOf(desired);
      retried = false;
      stalled = false;
      settleWaiters();
    },
    markDeleted() {
      wanted({ kind: "gone" }, true);
    },
    forget() {
      clearTimer();
      desired = undefined;
      known = undefined;
      stalled = false;
      retried = false;
      urgent = false;
      settleWaiters();
      sweep();
    },
    flushNow(opts = {}) {
      urgent = true;
      stalled = false;
      pump(opts);
    },
    handoff() {
      urgent = true;
      stalled = false;
      retried = false;
      handing += 1;
      return new Promise<DraftHandoff>((resolveWith) => {
        waiting.push(resolveWith);
        pump({});
        settleWaiters();
      });
    },
    wants() {
      if (desired?.kind !== "chat") return undefined;
      return { text: desired.text, attachments: desired.attachments.slice() };
    },
    isGone() {
      // Both halves, because they are two moments of one fact: `removed` is a
      // DELETE this page has already had an answer to, and a `gone` desired
      // state is one it has decided on and not yet heard back about. An editor
      // asking has words in its hand either way.
      return removed || desired?.kind === "gone";
    },
    watch(next) {
      rules.add(next);
      return () => {
        rules.delete(next);
        sweep();
      };
    },
    cancel() {
      clearTimer();
      rules.clear();
      waiting.splice(0);
      handing = 0;
    },
  };
  return api;
}

/**
 * THE THREE MOMENTS A HALF-TYPED THING IS MOST LIKELY TO BE ABANDONED, listened
 * for ONCE for every key at once rather than once per editor.
 *
 * `pagehide` rather than `unload` alone: it is the one that fires for a bfcache
 * navigation, which is most of them. `visibilitychange` covers the phone and the
 * tab switch that never becomes a pagehide at all. Both are the document going
 * away, so both send `keepalive` — and both BYPASS the in-flight rule, because a
 * queued flush is a flush that never leaves (Bugbot 4026181414).
 *
 * A window blur is not the document going away, so it is an ordinary flush: what
 * is pending goes now instead of in 600 ms, behind whatever is already out.
 */
let listening = false;

function listen(): void {
  if (listening || typeof window === "undefined") return;
  listening = true;
  const leaving = () => {
    for (const sync of syncers.values()) sync.flushNow({ keepalive: true });
  };
  window.addEventListener("pagehide", leaving);
  window.addEventListener("blur", () => {
    for (const sync of syncers.values()) sync.flushNow();
  });
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") leaving();
    });
  }
}

/**
 * WHAT `useAutosave` HANDS BACK — TWO CALLS, where there were five and then
 * three.
 *
 * There is no `settle` and no `stop`/`resume`, because there is nothing left for
 * a caller to order: the syncer has one request in flight and one desired state,
 * so a send, a discard and a hop simply SAY what they want and the order is the
 * order they said it in.
 */
export interface Autosave<T> {
  /** Send what is pending NOW rather than on the debounce. */
  flush(): void;
  /**
   * FORGET WHAT IS PENDING and take `next` as what the editor holds.
   *
   * The composer's send: the box is about to be cleared, and until the render
   * that empties it arrives this hook still holds the sentence that was sent.
   * Told what the value is about to become, rather than reading the current one.
   *
   * IT SAYS NOTHING TO THE SYNCER, deliberately. The send has just told it to
   * delete the record; a `reset` that also spoke would take that back. What the
   * syncer should hold is always said in the caller's own words —
   * `markDeleted`, `seedText`, `setText` — and this is only about what counts as
   * a change from here on.
   */
  reset(next: T): void;
}

/**
 * AUTOSAVE — now a thin seat over the syncer, and all that is left of it is
 * "tell the syncer when this editor's value changes".
 *
 * The hook compares SERIALISED values, so a caller may hand it a fresh object
 * every render — which every form does — without that alone counting as a
 * change. A MOUNT ALONE NEVER WRITES: the opening value is the baseline, so an
 * editor that merely opened mints nothing (design §4, "mint only on intent").
 * That baseline is per MOUNT and the syncer's is per KEY, which is the division
 * that matters: a second composer opening on a key this document has already
 * written must not read its own empty box as an instruction to delete.
 *
 * Everything else that used to be here — the debounce, the unload flush, the
 * unmount flush, the conflict retry, the one-in-flight queue — belongs to the
 * key rather than to the component and now lives with it.
 */
export function useAutosave<T>(
  value: T,
  push: (value: T) => void,
  { key }: { key?: string } = {},
): Autosave<T> {
  const pushRef = useRef(push);
  pushRef.current = push;
  const serial = JSON.stringify(value) ?? "";
  const written = useRef<string>(serial);
  const keyRef = useRef(key);
  keyRef.current = key;
  const valueRef = useRef(value);
  valueRef.current = value;

  // The unload listeners are armed by `draftSyncer` and not from here: they are
  // a fact about a KEY having a writer, and this hook is only one of the things
  // that can be that writer's mouth.
  useEffect(() => {
    if (serial === written.current) return;
    written.current = serial;
    pushRef.current(valueRef.current);
  }, [serial]);

  const flush = useCallback(() => {
    const on = keyRef.current;
    if (on) draftSyncer(on).flushNow();
  }, []);
  const reset = useCallback((next: T) => {
    written.current = JSON.stringify(next) ?? "";
    valueRef.current = next;
  }, []);

  return { flush, reset };
}
