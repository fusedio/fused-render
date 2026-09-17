// Run a stored draft NOW — the Board's "drag it into In Progress" gesture
// (design.md §4). Owned by the NewJobModal side (it knows the stored `form`
// shape); called from ScheduleTaskViews' drop handler.
//
// TWO DRAGS LAND HERE, and they are the same sentence said about two rows:
//
//   · a DRAFT ROW — an unfinished New task form — dropped on In Progress, which
//     submits that form as a real task (`runDraftNow`, design.md §4);
//   · a DONE ROW WEARING THE `✎ Draft` CHIP, dropped on the same lane, which
//     sends the unsent thing into the conversation it belongs to
//     (`runRowDraftNow`; Akshil, 2026-09-14: "if I have a done task that has
//     draft and I move it to In Progress it should run, why not?"). What is
//     unsent there is either words in that chat's composer or a New task form
//     bound to the session, and the row says which (tasks-lib.sendDraftAction).
//
// NOTHING HERE BUILDS A BODY OF ITS OWN. The modal's `buildSchedulePayload` is
// the one place that knows what POST /api/schedule is owed — which key a rule
// rides with, when a session is continued, what is left off the wire entirely —
// and a second copy of it here would answer those questions differently the
// first time the card grows a control. So this module does one translation: a
// STORED draft (drafts.TaskDraftForm / drafts.ChatDraft, read back through the
// modal's own `seededDraftForm`) into that builder's argument, `when` = now.
//
// AND IT DELETES NOTHING. Every draft that goes away here goes away inside the
// create request — `draft_id` for a form, `session_id` for a chat draft, both
// honoured by `routers/schedule.py` — because a delete the client made
// afterwards is the half that can fail on its own, leaving the words on the row
// as unsent beside the message they had already become.
import { scheduleMessage } from "@platform/lib/api";
import type { Task, TaskAttachment } from "@platform/lib/api";
import {
  fetchChatDraft,
  fetchDrafts,
} from "@platform/lib/drafts";
import type { DraftAttachment } from "@platform/lib/drafts";
import { copyToTaskShots } from "@apps/claude/ui/SchedButton";
import {
  buildSchedulePayload,
  seededDraftForm,
  toLocalInput,
} from "./NewJobModal";
import type { SchedulePayload } from "./NewJobModal";
import { repeatChoicesFor } from "./schedule-lib";
import type { DropAction } from "./tasks-lib";

/** True when this draft row can be run by a drag: a task-kind draft whose
 *  stored form would pass the card's own Save gate. Chat drafts (which carry no
 *  form at all) and half-written ones stay put.
 *
 *  THE SAME RULES `saveEnabled` APPLIES, in the same order, because the drop IS
 *  a Save — a draft the card would refuse must not become a task by being
 *  dragged past the form that refuses it. A title (the instruction: it is what
 *  Claude is sent, `composeTaskMessage`), a target, and a custom repeat that
 *  actually carries its rule. The description is optional exactly as it is on
 *  the card, so a draft named "Update the changelog" and nothing else runs.
 *
 *  What it cannot check is the path's EXISTENCE — that is an async listDir the
 *  card runs while it is open, and a drop has no window to run one in. The
 *  server checks it regardless and says so in its 400, which is where that
 *  refusal belongs. */
export function canRunDraft(task: Task): boolean {
  if (task.kind !== "draft") return false;
  if ((task.draft_kind ?? "task") !== "task") return false;
  return formRunnable(task.form);
}

/** The gate itself, over a stored form and nothing else — so the SAME rules
 *  answer for a draft row's own form (`canRunDraft`) and for the form bound to
 *  a session, which arrives from `GET /api/drafts` with no row around it
 *  (`boundDraftPayload`). Two copies of Save's gate is two chances for one of
 *  them to let a half-written card through. */
function formRunnable(form: Record<string, unknown> | null | undefined): boolean {
  if (!form) return false;
  // The id plays no part in the gate — `seededDraftForm` reads only `form` —
  // so it is not asked for here.
  const saved = seededDraftForm({ id: "", form });
  if (!saved.title || !saved.target) return false;
  if (saved.repeat === "custom" && !saved.customRule) return false;
  return true;
}

/**
 * The draft's stored form as the body POST /api/schedule wants, or null when
 * the draft cannot run (`canRunDraft` says which).
 *
 * Pure, and `now` is a parameter, so what actually goes on the wire for a drop
 * is assertable without a DOM — the same reason `buildSchedulePayload` itself
 * is pure (new-task-form.test.ts).
 *
 * TWO THINGS THE DRAG DECIDES, and only two:
 *   · `when` is NOW — the whole gesture is "stop planning this, run it" — and
 *     `timePicked: false` says nobody chose that minute, which is what keeps
 *     the task off the calendar (design: a plan, not a log).
 *   · `draft_id` rides along, so the server deletes the draft in the same
 *     request that creates the task. Two calls could half-fail and leave a
 *     draft row sitting beside the task it had already become.
 *
 * A REPEAT SURVIVES THE DROP. A draft carrying one is a schedule somebody
 * built, and "run it now" is not a reason to throw that away: the rule goes on
 * the wire with `when` as its anchor, which is one run immediately and the
 * pattern from there (the server's own past-anchor behaviour, SCH-13b). Only
 * the `immediate` flag is dropped in that case, and not by this module — the
 * payload refuses to pair it with a repeat, as the server does.
 */
export function draftRunPayload(
  task: Task,
  now: Date = new Date(),
): SchedulePayload | null {
  if (!canRunDraft(task)) return null;
  return boundDraftPayload(task.draft_id ?? "", task.form, now);
}

/**
 * THE SAME TRANSLATION, given the form on its own rather than a row carrying it
 * — which is the shape the Done row's drop has (Akshil, 2026-09-14).
 *
 * A form bound to a session is NOT a row: the listing gives it none, because it
 * is the next message of a task that already has a row and a number
 * (routers/tasks.py `_bound_chips`). So the drop reads it out of `GET
 * /api/drafts` by the id the row names (`Task.bound_draft`) and hands the two
 * halves in here. Everything after that is identical, deliberately: the bound
 * form holds its own `session_id`, so the message lands in the conversation the
 * card was written for, and `draft_id` still ends the draft in the same request
 * that creates the task.
 */
export function boundDraftPayload(
  draftId: string,
  form: Record<string, unknown> | null | undefined,
  now: Date = new Date(),
): SchedulePayload | null {
  if (!formRunnable(form)) return null;
  const saved = seededDraftForm({ id: draftId, form });
  // The stored repeat is a KEY into the preset list, and that list is built
  // around an anchor — "Weekly on Tuesday" means the anchor's weekday — so it
  // is resolved against the moment this task is being sent for, which is the
  // anchor going on the wire. `custom` is the one choice carrying its own rule.
  const repeat = saved.repeat ?? "none";
  const rule = repeat === "custom"
    ? saved.customRule
    : repeatChoicesFor(now).find((c) => c.key === repeat)?.rule ?? null;
  const attachments = saved.attachments ?? [];
  return buildSchedulePayload({
    target: saved.target ?? "",
    title: saved.title ?? "",
    // The card's second field. Empty is legal — a task whose whole instruction
    // fits in its title should not have to say it twice.
    message: saved.description ?? "",
    when: toLocalInput(now),
    rule,
    repeat,
    // A legacy cron line is an EDIT's field (`editing.repeats`): the form has
    // not written cron since it became a checkbox, so no draft can carry one.
    legacyCron: "",
    permission: saved.permission ?? "",
    model: saved.model ?? "",
    effort: saved.effort ?? "",
    // WHERE THIS TASK IS GOING, when the draft was written out of a chat: the
    // draft restates it on every save precisely so a card nobody has open still
    // knows (drafts.TaskDraftForm.session_id). A drop is exactly that case.
    sessionId: saved.sessionId ?? "",
    newTaskEachRun: saved.newTaskEachRun ?? false,
    // Nothing is being replaced: a draft is not an entry, so there is no
    // scheduled message to withdraw and no number to move off one. The draft's
    // own number moves across on `draft_id` instead, server-side.
    replacesEntryId: "",
    draftId,
    timePicked: false,
    images: attachments.map((a) => a.path),
    attachments,
  });
}

/** Submit the draft's stored form as a real task, due immediately, and let the
 *  server delete the draft (`draft_id` rides on POST /api/schedule).
 *
 *  RETURNS THE NEW ENTRY'S ID, because creating the task is only half of what
 *  the drop promised: the card was dragged into In Progress, and a task sitting
 *  a second away from its own first run is a card in the wrong lane. The caller
 *  fires it (`performRun({ kind: "run-now" })`), the same road the `resay` drop
 *  takes — the id is the only thing it needs from here and the only thing this
 *  module can tell it. */
export async function runDraftNow(task: Task): Promise<string> {
  const body = draftRunPayload(task);
  // The drag is gated on `canRunDraft`, so this is only reachable from a caller
  // that never asked — and a silent return there would read as a drop that
  // worked. The sentence is the card's own (`saveBlockedReason`), because the
  // fix is the same one: open the draft and finish it.
  if (!body) {
    throw new Error(UNFINISHED);
  }
  const made = await scheduleMessage(body);
  return made.entry.id;
}

// ---- the DONE row's draft ----------------------------------------------------
// "if I have a done task that has draft and I move it to In Progress it should
// run, why not?" (Akshil, 2026-09-14.)
//
// The row here is a real task with a transcript behind it, and the thing being
// dragged is not the task — it is the sentence sitting unsent on it. So the drop
// does what pressing Send in that conversation would have done, and the lane it
// lands on stops being a claim about finished work: nothing of the task is
// re-run, one new message goes out.
//
// WHICH DRAFT is the row's own answer (`Task.draft.kind`, tasks-lib
// `sendDraftAction`), and the two are genuinely different things to send:
//
//   · `"form"` — a New task card bound to this session. It holds a time, a
//     repeat rule, a model and files as well as words, so it is submitted the
//     way a draft row's drop submits one (`boundDraftPayload`);
//   · `"chat"` — words in that conversation's composer, which hold nothing but
//     themselves. They travel as an immediate message into the session, the same
//     road tasks-lib's `resay` takes for a typed message going out again.
//
// NEITHER BODY IS HERE ON THE ROW. The listing carries a PREVIEW — one clipped
// line, enough for a chip — so both paths read the real thing back from
// `GET /api/drafts` first. A drop that sent the preview would silently truncate
// the user's own sentence.

/** What Save says to a card with no instruction in it, and what a drop that
 *  reaches an unfinished form says for the same reason: the fix is to open the
 *  draft and finish it. Spelled once, now that three callers say it. */
const UNFINISHED =
  "Say what Claude should do — a task with no instructions has nothing to run.";

/**
 * UNSENT COMPOSER WORDS AS A MESSAGE INTO THEIR OWN CONVERSATION, due now.
 * Null when there is nothing to send or nowhere to send it.
 *
 * Pure and `now` is a parameter, exactly like `draftRunPayload`, so what goes on
 * the wire for this drop is assertable without a DOM.
 *
 * NO TITLE AND NO DESCRIPTION, which is the whole difference from the form path
 * and is not an omission:
 *
 *   · a `title` names a TASK, and this task is named already — it has a row the
 *     reader recognises. Left off the wire, the server keeps the name it has;
 *   · a `description` would be worse than redundant. The row's description is
 *     read off the LAST entry that has one (routers/tasks.py `_description`), so
 *     sending these words as one would rewrite what the task says it is about
 *     into whatever was left in the composer. `buildSchedulePayload` derives it
 *     from `message`, so it is dropped here, deliberately and in one place.
 *
 * The words themselves are untouched — `composeTaskMessage` with no title is the
 * text and nothing else — because the user typed a message, not a form.
 *
 * `session_id` IS ALSO THE DELETE. `POST /api/schedule` drops the chat draft
 * filed under the session it is scheduling into (routers/schedule.py), so the
 * `✎ Draft` chip clears in the same request that sends the words — no second
 * call to half-fail, and no window where the row shows them as still unsent.
 */
export function chatDraftPayload(
  draft: {
    /** The conversation these words are in, which is both where the message
     *  goes and the key the draft is filed under. */
    sessionId: string;
    /** Where the run happens — the task's own folder, as the `resay` drop uses
     *  it (`task.target || task.project`). */
    target: string;
    text: string;
    /** Already copied into the task-shots dir — see `carriedAttachments`. */
    attachments?: TaskAttachment[];
  },
  now: Date = new Date(),
): SchedulePayload | null {
  const text = draft.text.trim();
  // A message with no words is not a message, whatever is attached to it: the
  // server has nothing to hand Claude, and a composer holding only files is a
  // half-made thought rather than a thing to send on a drag.
  if (!draft.sessionId || !text) return null;
  const attachments = draft.attachments ?? [];
  const body = buildSchedulePayload({
    target: draft.target,
    // See above: the task is named already, and the words are the message.
    title: "",
    message: text,
    when: toLocalInput(now),
    rule: null,
    repeat: "none",
    legacyCron: "",
    permission: "",
    model: "",
    effort: "",
    sessionId: draft.sessionId,
    newTaskEachRun: false,
    replacesEntryId: "",
    // The chat draft is deleted by `session_id` alone (above), so there is no
    // draft id to name: these words were typed in the conversation they are
    // going to.
    draftId: "",
    timePicked: false,
    images: attachments.map((a) => a.path),
    attachments,
  });
  // The one field the builder would derive that this message must not carry.
  delete body.description;
  return body;
}

/**
 * The chat draft's files, copied into the task-shots dir and named the way a
 * scheduled entry names them.
 *
 * THE PATHS CANNOT TRAVEL AS THEY ARE. A composer's attachment lives in the
 * claude template's tempdir-rooted shots dir on a 12 h TTL, and
 * `POST /api/schedule` refuses any attachment path outside
 * `schedule.shots_dir()` — so a body built from the draft's own paths would be
 * a 400 and the drop would do nothing at all. The bytes move instead, through
 * the very function the composer's own Schedule hop uses for this
 * (`SchedButton.copyToTaskShots`), which is what makes an attachment that came
 * from a chat indistinguishable from one attached to a card.
 *
 * A stored draft attachment IS a tray attachment with the fields a JSON file
 * cannot hold left out (`drafts.DraftAttachment` is written from the tray's
 * `view`/`name`/`kind`), so it is handed over as one. Nothing here is pending:
 * the composer only writes a chip into its draft once its bytes have landed.
 *
 * ONE FAILURE COSTS ONE ATTACHMENT, which is that function's own contract: a
 * pruned file must not be the reason the sentence never goes.
 */
async function carriedAttachments(
  rows: readonly DraftAttachment[],
): Promise<TaskAttachment[]> {
  if (!rows.length) return [];
  return copyToTaskShots(
    rows.map((a) => ({ id: a.path, kind: a.kind, view: a.path, name: a.name })),
  );
}

/**
 * SEND WHAT THIS ROW IS CARRYING, and let the server clear the chip.
 *
 * The action is tasks-lib's (`send-draft`): it has already decided that there
 * IS a draft, which kind it is and which conversation it belongs to. What is
 * left is the part only a request can answer — the draft's real body — so both
 * branches read it back before building anything.
 *
 * EVERY REFUSAL IS A SENTENCE, thrown for the Board's own note line (the same
 * place a 409 from the scheduler lands). The two worth spelling out are a draft
 * that is no longer there — sent from the chat while the card was in the air,
 * or discarded — and a bound form that is not finished enough to send, which is
 * Save's own refusal and has Save's own fix.
 */
export async function runRowDraftNow(
  task: Task,
  action: Extract<DropAction, { kind: "send-draft" }>,
): Promise<string> {
  let made;
  if (action.draftKind === "form") {
    made = await scheduleMessage(await boundFormBody(action.draftId));
  } else {
    made = await scheduleMessage(await chatBody(task, action.sessionId));
  }
  // The new entry's id, for the same reason `runDraftNow` hands one back: the
  // words were dragged into In Progress, so the caller fires this immediately
  // rather than leaving a message due "now" for the scheduler to notice.
  return made.entry.id;
}

/** The bound New task form, read back by id and translated. */
async function boundFormBody(draftId: string): Promise<SchedulePayload> {
  const all = await fetchDrafts();
  // NULL IS "COULD NOT FIND OUT", never "there are none" (platform/lib/drafts
  // `fetchDrafts`) — and the two must not be reported as one thing here, where
  // the second sentence would tell the reader their draft is gone when the
  // server merely did not answer.
  if (!all) throw new Error(UNREADABLE);
  const form = all.task[draftId] as unknown as Record<string, unknown> | undefined;
  if (!form) throw new Error(GONE);
  // STILL BOUND? The row offered this drop because the listing said this form is
  // a message into that conversation (`_bound_chips` collects only forms that
  // name a session), and the drop's whole promise is that the words land THERE.
  // A form that has since lost its binding would be scheduled as a brand-new
  // task under a new number — a different outcome from the one the reader
  // dragged for, and a silent one. Refuse instead; the card is still openable.
  if (!form.session_id) throw new Error(GONE);
  const body = boundDraftPayload(draftId, form);
  if (!body) throw new Error(UNFINISHED);
  return body;
}

/** The composer's unsent words, read back by session and translated. */
async function chatBody(task: Task, sessionId: string): Promise<SchedulePayload> {
  const draft = await fetchChatDraft(sessionId);
  // THE SAME THREE ANSWERS `fetchDrafts` GIVES, because this one is built on it
  // (Bugbot, PR #1180): `undefined` is a GET that failed and `null` is a key
  // with nothing under it. Told apart here for `boundFormBody`'s reason — the
  // GONE sentence says the reader's words are already sent or discarded, which
  // is a hard thing to be told because the server blinked.
  if (draft === undefined) throw new Error(UNREADABLE);
  if (!draft) throw new Error(GONE);
  const body = chatDraftPayload({
    sessionId,
    target: task.target || task.project,
    text: draft.text,
    attachments: await carriedAttachments(draft.attachments ?? []),
  });
  if (!body) throw new Error(GONE);
  // NOTHING IS "SPENT" HERE ANY MORE (design "one record", §2). The create's own
  // `session_id` deletes the chat draft server-side, and the announcement that
  // follows reaches every composer mounted on that key through the change feed
  // (`tasksPulse.onDraftChange`) — so the box empties on the news rather than on
  // a promise this module used to make on the sender's behalf. A composer write
  // still in the air when the delete lands is refused as stale, which is what
  // the version is for; it can no longer put the sentence back.
  return body;
}

/** What a drop finds when the draft it was aiming at is not there any more —
 *  sent from the chat while the card was in the air, or discarded. Worded as
 *  the likelihood rather than as an error, because it usually IS what happened,
 *  and the board re-reads itself straight afterwards either way. */
const GONE = "Nothing unsent left in that conversation — it may already have gone.";

/** …and the other thing a read can answer: nothing at all. */
const UNREADABLE = "Could not read that draft just now — try again.";
