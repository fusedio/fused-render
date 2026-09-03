// The New task card's pure decisions: what the fields open on, what Save
// refuses, what goes over the wire. Nothing here touches the DOM, which is what
// lets new-task-form.test.ts assert each rule without rendering the card.
import type {
  Config, RecurrenceRule, ScheduledMessage, TaskAttachment, scheduleMessage,
} from "@platform/lib/api";
import { repeatChoicesFor } from "../schedule-lib";
import { normPath } from "./paths";

// Where a new task points before the user says otherwise: the Fused
// workspace (Akshil, 2026-08-14 — an empty path field was the confusing part
// of the form). Taken from the server's RESOLVED workspace rather than
// composed from home, so a FUSED_RENDER_DIR user gets their own folder and
// not one that may not exist (whose only symptom is the server's 400 naming
// the path). The picker makes changing it a click.
export const defaultTargetOf = (c: Pick<Config, "home" | "fused_dir">) =>
  normPath(c.fused_dir || c.home);

// How a permission mode is SAID. The keys are the server's contract and stay
// exactly as they are on the wire; only the reading changes. A mode this map
// has never heard of shows its key, which is still better than hiding it.
const PERMISSION_LABELS: Record<string, string> = {
  auto: "Auto",
  acceptEdits: "Accept edits",
  plan: "Plan only",
  prompt: "Ask every time",
};

export const permissionLabel = (key: string) => PERMISSION_LABELS[key] ?? key;

// A Date as the value a <input type="datetime-local"> wants: local wall-clock,
// minute precision, no zone suffix. `toISOString` is exactly wrong here (UTC).
export function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// Which derived choice a stored RULE is, so editing reopens on the words the
// user picked; anything the preset list can't say is "custom". Legacy cron
// templates get a "cron" key of their own — the form no longer writes cron,
// but editing an old entry must not silently rewrite its rule.
export function keyOfRule(rule: RecurrenceRule, anchor: Date): string {
  const choices = repeatChoicesFor(anchor);
  const canon = (r: RecurrenceRule) =>
    JSON.stringify({
      freq: r.freq,
      interval: r.interval ?? 1,
      byday: r.freq === "week" ? (r.byday?.length ? [...r.byday].sort((a, b) => a - b) : [anchor.getDay()]) : undefined,
      monthly: r.freq === "month" ? (r.monthly ?? "day") : undefined,
      until: r.until,
      count: r.count,
    });
  const hit = choices.find((c) => c.rule && canon(c.rule) === canon(rule));
  return hit?.key ?? "custom";
}

// The repeat choice a form OPENS on. "none" is the one value that means the
// Repeat checkbox is unticked, so this is also what decides whether editing a
// repeating task opens checked (design §6).
export function initialRepeatKey(entry?: ScheduledMessage | null): string {
  if (entry?.rule) return keyOfRule(entry.rule, new Date(entry.due));
  return entry?.repeats ? "cron" : "none";
}

// What the DESCRIPTION field opens with. An Edit opens on what the entry has; a
// new task opens on the body of the chat composer's draft, whose first line has
// gone to the title instead (see splitDraft).
//
// `description` FIRST now, and that order is the whole subtlety: `message` is
// the composed title-plus-body Claude was sent (composeTaskMessage), so opening
// the description on it would put the title back into the field under itself,
// and the next Save would compose the heading a second time. `message` stays as
// the fallback for every task stored before the two were composed — with the
// heading peeled off if it is there, which is what `withoutTitleHeading` is for.
// `||`, not `??`: "" is a missing answer here, not an answer.
export function initialAskOf(
  entry?: ScheduledMessage | null,
  chatDraft?: string | null,
): string {
  if (entry) {
    return (
      entry.description
      || withoutTitleHeading(entry.message ?? "", entry.title ?? "")
    );
  }
  return splitDraft(chatDraft).description;
}

// The inverse of composeTaskMessage, for the one reader that needs it: an Edit
// falling back to a stored `message`. It has to invert BOTH shapes the composer
// can produce, because the composer has two:
//
//   * `title\n\nbody` — the two-field task. The heading and its blank line come
//     off and the body is what is left;
//   * `title` alone — the TITLE-ONLY task, which is the ordinary case now that
//     the second field is optional. Nothing was appended, so there is no prefix
//     to spot, and treating it as unrecognised prose is the bug it was: the
//     whole message came back as the additional instructions, the next Save
//     composed `title\n\ntitle`, and every further edit stacked another copy
//     (Bugbot, PR #595). It inverts to "" — there were no additional
//     instructions, which is exactly what the field should open on.
//
// Still deliberately exact about what it will peel: an opening that merely
// begins with the same words is prose and is left alone. The equality check is
// on the trimmed message, so trailing whitespace the wire may have picked up
// does not make a title-only task look like something else.
export function withoutTitleHeading(message: string, title: string): string {
  const name = title.trim();
  if (!name) return message;
  if (message.trim() === name) return "";
  const head = `${name}\n\n`;
  return message.startsWith(head) ? message.slice(head.length) : message;
}

// The thread a task ALREADY OWNS, if any. A repeating template LEARNS one: its
// first run reports the session it ran in and the server writes that id back
// onto the template, so run 2 resumes it (a task IS a session — design §6).
// That id has to survive an edit, because an edit is cancel + re-create and
// dropping it orphans everything the task built.
//
// An UNMARKED id is not that. It is a chat handoff kept from when the task was
// scheduled, and it keeps a handoff's rules: continued while the task stays a
// one-off, refused the moment it starts repeating — otherwise ticking Repeat on
// a chat-scheduled task quietly signs the user's open conversation up to be
// appended to forever, the exact thing the repeat rule exists to refuse.
//
// The two are told apart by `session_learned`, which the server writes at the
// moment it learns the id and which travels through the cancel-and-re-create an
// edit is. This used to be INFERRED — an id counted as learned if the entry
// repeated — and that reading cannot survive a round trip: demote a chaining
// task to a one-off (its learned id deliberately rides along) and promote it
// back, and the learned thread reads as a chat handoff and is dropped
// (Bugbot, PR #555). An absent marker means NOT learned, which is the reading
// that keeps a chat's id refused by a repeat.
export function learnedSessionOf(entry?: ScheduledMessage | null): string {
  if (!entry?.session_id || entry.session_learned !== true) return "";
  return entry.session_id;
}

// ---- Deleting a task -----------------------------------------------------
// The one way to STOP a repeating task. Everything else on the page cancels an
// OCCURRENCE — the list's per-message cancel and the calendar popover's row
// cancel both mean "skip this run", deliberately, and a rule whose runs you
// skip one at a time keeps minting more forever (Akshil, 2026-08-17). The
// server has always been able to do it: `schedule.cancel` on a TEMPLATE id
// cancels the template AND its pending occurrence, which is exactly "no further
// runs". Nothing in the UI had ever called it with a template id.
//
// The modal is where it belongs because the modal is already the one place a
// template is addressable: an occurrence's Edit resolves `template_id ||
// entry_id` (Scheduled.editEntry), so opening "tomorrow's run" of a repeating
// task opens the RULE. The button just had to exist.
//
// What is cancellable is decided here rather than at the press, so a control
// that would 404 is never drawn: `sending` is deliberately not cancellable (the
// helper is away and the turn may have started — schedule.cancel's docstring),
// and a terminal entry (`sent`/`missed`/`error`/`cancelled`) has nothing left
// to stop. Only `pending` and `recurring` can be withdrawn.
export interface DeleteAction {
  // The id to cancel — a template's id when this is a rule, which is what
  // makes it stop the series rather than skip one run.
  id: string;
  // Whether cancelling ends a SERIES. Drives every sentence below, and the
  // reading of a 404.
  series: boolean;
  label: string;
  // The second press. It names the consequence rather than asking "are you
  // sure?", because the consequence is the whole difference between the two
  // cases and it is not undoable from this page.
  confirm: string;
  title: string;
}

export function deleteActionFor(entry?: ScheduledMessage | null): DeleteAction | null {
  if (!entry) return null;
  const series = entry.state === "recurring";
  if (!series && entry.state !== "pending") return null;
  return {
    id: entry.id,
    series,
    // One label for both cases — the user is deleting the task either way, and
    // a rule that called itself "Delete schedule" would read as a third noun
    // the page never uses. The difference is spelled out on the second press.
    label: "Delete task",
    confirm: series
      ? "Delete and stop all future runs?"
      : "Delete and cancel this run?",
    title: series
      ? "Deletes this task and stops all future runs. Runs it has already made are kept."
      : "Deletes this task. It will not run.",
  };
}

// What a press of that button decides, as a value rather than as a branch
// buried in a handler — so "the first press cannot reach the server" is a thing
// that can be asserted. `arm` carries no id at all; only the second press
// produces one.
export type DeletePress = { do: "arm" } | { do: "delete"; id: string };

export function deletePress(
  action: DeleteAction | null,
  armed: boolean,
): DeletePress | null {
  if (!action) return null;
  if (!armed) return { do: "arm" };
  return { do: "delete", id: action.id };
}

// What the error area says when the cancel does not land. A 404 is the honest
// race, not a failure: the run fired, or someone cancelled it in another tab —
// so it is translated instead of showing the server's id-bearing sentence,
// which reads as a bug. Every other status keeps the server's own words: those
// are written for a human (see the router's 400s).
export function deleteFailureText(err: unknown, series: boolean): string {
  const status = (err as { status?: number } | null)?.status;
  if (status === 404) {
    return series
      ? "This task is already stopped — nothing is scheduled to run from it any more."
      : "This task is already gone — it has run, or it was cancelled somewhere else.";
  }
  return (err as Error | null)?.message || "The task could not be deleted.";
}

// ---- Naming the task -----------------------------------------------------
// Title is REQUIRED (Akshil, 2026-08-17), and it opens prefilled wherever the
// app honestly knows a name — which is any path with a SESSION behind it (see
// the precedence below). Where it does not, the field opens blank and the
// requirement is what asks for a name. That is the trade, stated plainly: a
// blank required field costs the user one line of typing, while a field
// prefilled with a guess costs them a task named after its own description.
//
// The placeholder ASKS FOR THE TASK (Akshil, 2026-08-18), because that is what
// this field now collects. It said "Title", which is what the value is USED for
// — the row's name in the list — and not what the user is being asked to write;
// people answered it with a label ("News") and then wrote the actual instruction
// underneath, which is the split this whole pass exists to close. The question is
// the same one the composer asks, and the answer to it is both the task's name
// and the first line of what Claude is sent.
//
// (Two earlier wordings are gone for the same class of reason: "optional, filled
// in automatically" outlived the requirement, and a PREVIEW of the chat's own
// name is worse than a question when Save then refuses the preview.)
export const TITLE_PLACEHOLDER = "What should Claude do?";

// And the second field is the OVERFLOW of that question — the constraints, the
// context, the "start with the parquet path" — never the task again. Both jobs a
// placeholder can honestly do are in it: which field this is, and that it can be
// left alone. It read "What should Claude do?" while it was the whole message,
// and leaving that question here while the field above asks it too would put the
// user in front of the same question twice.
export const ASK_PLACEHOLDER = "Additional instructions (optional)";

// One line of a block of prose, trimmed. Used to reduce a multi-line value to
// something an <input> can hold — it would strip the newlines anyway. It also
// used to answer "is this string the message I am about to send?" for
// sessionTitleOf; that question is the server's now, and answered by provenance
// rather than by comparing strings.
export function firstLine(text: string): string {
  return text.trim().split("\n")[0]?.trim() ?? "";
}

// -- The title names the SESSION, never the message ---------------------------
// The bug this replaced (Akshil, 2026-08-17): scheduling from a Claude chat
// prefilled Title with `firstLine(ask)` — the very message being scheduled — so
// a long message came out duplicated, once as the title and once as the
// description. "The description is what we type in the chat box"; the title is
// what the CONVERSATION is called.
//
// So the precedence is now, in order:
//   1. the task's own stored title, if a user ever set one;
//   2. the SESSION's resolved title — Claude Code's `ai-title` record, which is
//      the "cloud summarised it" case, served on /api/tasks as `title` with
//      `title_source: "ai"`;
//   3. the session's FIRST user message, shortened — "the first message that we
//      had". Also /api/tasks, as `title_source: "message"`: the first line of the
//      transcript's first user prompt (tasks.py `_title` reading
//      `tasks_store.head`). Its sibling `title_source: "entry"` — a row named
//      from a message merely SCHEDULED at the session, because the transcript
//      could not be read — is not a step here at all; see sessionTitleOf;
//   3b. the slash command the session ran (`title_source: "command"`), for a
//      session that contains no prose at all — `/making-a-release` is the only
//      true thing there is to call one of those, and the server says so rather
//      than leaving the row nameless;
//   4. nothing. The field opens blank and the user types a name.
// Never the composed message, at any step. Steps 2 and 3 need a fetch, so they
// live in the /api/tasks effect; 1 and 4 are what `initialTitleOf` decides
// synchronously.

// How long a title derived from a first message is allowed to be. A name, not a
// summary: this is the whole point of step 3 — a 200-char first line (the
// server's own cap) is the duplication bug again in a longer field.
export const TITLE_MAX = 60;

// A first message reduced to a name: one line, clamped, cut on a word boundary.
// No ellipsis — the field is a NAME the user can edit, and "…" is punctuation
// they would have to delete. A single word longer than the clamp has no boundary
// to cut on, so it is cut hard; that is the only mid-word cut here.
export function shortTitle(text: string, max = TITLE_MAX): string {
  const line = firstLine(text);
  if (line.length <= max) return line;
  // max + 1 so a value whose max'th character is the space gets the whole word
  // before it rather than losing it.
  const boundary = line.slice(0, max + 1).lastIndexOf(" ");
  return (boundary > 0 ? line.slice(0, boundary) : line.slice(0, max)).trimEnd();
}

// -- The chat handoff fills BOTH fields ---------------------------------------
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

// A prefill this field must refuse, whichever source produced it: a transcript
// record's machine-written wire, leaked into a title.
//
// THIS IS A GUARD, NOT THE FIX. The fix is server-side — four readers of a
// transcript's first user message each had their own idea of what counted as
// machinery, and /api/tasks served rows titled `<live-app-state>` and
// `<command-message>making-a-release</command-message>` (44 of them in one real
// store). tasks_store owns that policy now and the server no longer emits such a
// string. This refuses one anyway, because of what happens to a bad prefill in
// THIS field specifically: the precedence below is permanent in one direction —
// a `user`-set title outranks every other source forever — so a single leaked
// string the user does not notice before pressing Save becomes that task's name
// for good. One already is, in one real store. A second check on the cheap side
// of an asymmetric cost.
//
// Deliberately NARROW, and the narrowness is the point: it refuses a value that
// OPENS with a tag or with the annotation preamble's sentence. It does not go
// hunting for angle brackets, because "fix why <div> renders twice" is a
// perfectly good name for a thread about that bug, and refusing it would be the
// very mistake this whole change undoes — a reader deciding that markup means
// nobody typed it.
const LEAKED_TITLE = /^(?:<[a-z][a-z0-9-]*>|The user annotated )/;

// What Title OPENS on, synchronously. Only step 1 and step 4: a stored title
// wins outright (an edit that quietly replaced it would be data loss), and
// otherwise the field is BLANK until the /api/tasks lookup answers.
//
// It used to derive `firstLine(initialAskOf(...))` here, which is what put the
// scheduled message in the title. Blank is the honest synchronous answer instead
// — the form has nothing to say about the session yet — and blank is safe even
// though Title is required: the requirement bites at Save, by which time either
// the lookup has filled the field or the user has.
export function initialTitleOf(entry?: ScheduledMessage | null): string {
  const title = (entry?.title ?? "").trim();
  // Guarded here as well as in `sessionTitleOf`, because a stored title is
  // exactly how the one bad row in the real store got there: it was saved, so it
  // is a `user` title now, and re-prefilling it on an Edit would keep the
  // mistake alive every time the form opened.
  return LEAKED_TITLE.test(title) ? "" : title;
}

// The pairing that keeps the two halves of step 1 from drifting apart. "Is there
// a usable stored title?" is ONE question with two readers — the value the field
// OPENS on, and whether the /api/tasks lookup may run at all — so it is asked
// once, by `initialTitleOf` above, and both readers take that answer.
//
// It was asked twice (review, 2026-08-18), and the two answers disagreed on
// exactly the rows LEAKED_TITLE exists to rescue: the field took
// `initialTitleOf`, which blanks a leaked machinery title, while the lookup gated
// on the RAW `entry.title` — and a leaked string is non-empty, so the guard sent
// the lookup home. The field arrived blank AND stayed blank on a REQUIRED field,
// so Save was refused on the one task the user cannot easily rename. Repeating
// the LEAKED_TITLE test at the second site would only have set the same trap for
// whatever the third reason to reject a stored title turns out to be.
//
// `lookupSession` is "" for "do not fetch", and it carries BOTH refusals: a
// stored title has won step 1 outright (an async overwrite would be data loss),
// or there is no session to ask about in the first place.
//
// A CHAT DRAFT's first line (splitDraft) sits between the two, and it takes the
// same refusal: it is a name the user has just written, this second, and the
// /api/tasks lookup would land a beat later and replace it with the name of the
// conversation they wrote it in. A stored title still outranks it — an Edit
// never loses the name it has — and the draft's line only exists on a NEW task,
// where there is nothing to lose.
export function initialTitleStateOf(
  entry?: ScheduledMessage | null,
  sessionId?: string | null,
  draftTitle?: string | null,
): { title: string; lookupSession: string } {
  const title = initialTitleOf(entry) || (entry ? "" : (draftTitle ?? "").trim());
  return { title, lookupSession: title ? "" : (sessionId ?? "") };
}

// Steps 2 and 3, which only /api/tasks can answer: the name the session this
// form was opened from already carries. A session IS a task there, so the row
// keyed on it has both the resolved `title` and the `title_source` saying which
// branch produced it — and that provenance is the whole reason this reads the
// API instead of a string in the deep link.
//
// The server's step 3 has TWO sources and only one of them is a name, so the
// row says which it read (tasks.py `_title`):
//
//   * `message` — the session's own first prompt, out of the transcript. Step 3
//     itself, "the first message that we had", and taken as a name (shortened).
//   * `entry` — no readable transcript, so the row is named from the earliest
//     message SCHEDULED at that session. On a task made in this form that is the
//     ask itself, which is the duplication bug arriving by way of the server, so
//     it is refused and Title stays blank for the user to fill.
//
// This used to be one value, and the composed ask was passed in so the client
// could GUESS which of the two it had: a `message` title was dropped whenever
// the ask's first line began with it. A guess cannot tell an echo from a
// continuation — "pull today's news and file it" begins with the session's real
// first prompt "pull today's news" — so a session lost the name the app already
// knew and Save sat disabled until the user retyped it. The server knows the
// answer for certain, so it says it, and no draft is an input here at all.
//
// Steps 1 and 2 are taken verbatim — a name a human typed and a name Claude
// wrote are both already names, and shortening either would edit someone's
// words. Only step 3 is a message being reduced to one.
export function sessionTitleOf(
  tasks: readonly { session_id: string; title: string; title_source: string }[],
  sessionId: string,
): string {
  if (!sessionId) return "";
  const task = tasks.find((t) => t.session_id === sessionId);
  const title = (task?.title ?? "").trim();
  if (!title) return "";
  // Before the source is consulted at all: a leaked wire string is not a name
  // from ANY source, and the verbatim branches below would take it as one. See
  // LEAKED_TITLE — a guard behind a server fix, not the fix.
  if (LEAKED_TITLE.test(title)) return "";
  if (task?.title_source === "entry") return "";
  if (task?.title_source === "message") return shortTitle(title);
  // Everything else is already a name and is taken as written: `user` and `ai`,
  // and `command` — a session whose only user records are a slash command is
  // named `/making-a-release`, which is short, true, and not a message.
  return title;
}

// What the Repeat checkbox does to the repeat state. Unticking CLEARS: the key
// goes back to "none" AND the custom rule is dropped, so nothing stays armed
// behind a dropdown that is no longer on screen — a hidden rule would still be
// submitted by `rule` below. Ticking an unset form lands on the commonest
// answer rather than on a blank menu; ticking a form that already carries a
// rule (an Edit) leaves it exactly where it was.
export const DEFAULT_REPEAT_KEY = "daily";

export function applyRepeatToggle(
  on: boolean,
  current: { repeat: string; customRule: RecurrenceRule | null },
): { repeat: string; customRule: RecurrenceRule | null } {
  if (!on) return { repeat: "none", customRule: null };
  if (current.repeat === "none") return { repeat: DEFAULT_REPEAT_KEY, customRule: current.customRule };
  return current;
}

// -- What a time already gone actually MEANS ---------------------------------
// A past time is not refused, by this form or by the server (design §9). What
// the form owes instead is a sentence naming which of the TWO things will
// happen, because a one-off and a rule answer differently:
//
//   * one-off — the queue sorts it to the head and sends it (SCH-3b). Once.
//   * rule    — SCH-13b. A rule template with nothing materialized yet walks
//     anchor → now and creates ONE occurrence, on the latest slot at or before
//     now, marked `catch_up`; it is overdue the instant it exists, so it goes
//     on the next tick. Every slot it stepped past is never materialized and
//     never runs — the same collapse `_coalesce` applies to a backlog
//     (SCH-13) — so an anchor a year back is still exactly one run, not a
//     year of them. The series then continues from now in the ordinary way.
//
// Two sentences rather than one, because "runs as soon as it can" is a promise
// a repeat does not keep: it runs once now AND then keeps its pattern.
export const PAST_NOTE_ONE_OFF =
  "This time has passed — the task will run as soon as it can.";
export const PAST_NOTE_CATCH_UP =
  "This time has passed — one catch-up run goes now, then the task keeps to "
  + "its schedule. Just the one, however many have gone by.";

// The series' FIRST slot, given the picked date as its anchor — null once the
// rule's `until` has already cut the series off before it began.
//
// Mirrors recur._walk's opening step, and for four of the five frequencies
// there is nothing to mirror: hourly, daily, monthly and annually all include
// the anchor itself (a monthly nth-weekday reads "the second Wednesday" OFF
// the anchor, so the anchor's own month always has it; a Feb 29 anchor is in a
// leap year by construction). Only a WEEKLY rule can start later than its
// anchor, and only because the chosen days are free of it: the anchor's week
// is a partial one (`when >= anchor` in _walk_week), so a Tuesday anchor with
// only Thursday ticked starts on that Thursday, and a Tuesday anchor with only
// Monday ticked starts `interval` weeks on.
export function firstRuleSlot(rule: RecurrenceRule, anchor: Date): Date | null {
  let first = anchor;
  if (rule.freq === "week" && rule.byday?.length) {
    const days = [...rule.byday].sort((a, b) => a - b);
    // Sunday-anchored blocks, counted from the anchor's OWN week — the unit
    // that repeats is the week, not "7·interval days from each run".
    const sunday = anchor.getDate() - anchor.getDay();
    const slot = (day: number, weeks: number) =>
      new Date(anchor.getFullYear(), anchor.getMonth(), sunday + day + weeks * 7,
               anchor.getHours(), anchor.getMinutes());
    const thisWeek = days
      .map((d) => slot(d, 0))
      .find((d) => d.getTime() >= anchor.getTime());
    first = thisWeek ?? slot(days[0], rule.interval ?? 1);
  }
  if (rule.until) {
    // INCLUSIVE, and compared on the DATE, so the time of day cannot decide
    // it — recur._walk's rule exactly.
    const [y, m, d] = rule.until.split("-").map(Number);
    if (first.getTime() > new Date(y, m - 1, d, 23, 59, 59, 999).getTime())
      return null;
  }
  return first;
}

// Midnight-safe start of the minute `d` falls in, as epoch ms. Field arithmetic
// rather than `- (seconds * 1000)`, for the reason schedule-lib's day helpers give:
// a constructed local date cannot be knocked into the wrong hour by a DST edge.
export const startOfMinute = (d: Date) =>
  new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes())
    .getTime();

// The note the when-row prints, or null for silence. Silence is the answer for
// a future time, and also for the two repeats with no anchor to catch up FROM:
// a legacy cron template (`create` computes its first run from now by
// construction, and cron never reads `due` at all — Bugbot, PR #541) and a
// half-finished Custom, which Save refuses anyway. Never a refusal: it does
// not touch `ready`, because "start this pattern, and run the one I missed" is
// a legitimate thing to ask for.
export function pastNoteFor(
  picked: Date | null,
  repeatOn: boolean,
  rule: RecurrenceRule | null,
  now: Date,
): string | null {
  if (!picked || Number.isNaN(picked.getTime())) return null;
  // COMPARED AT THE FIELD'S OWN PRECISION (2026-08-18). The picker is
  // minute-precision, so the current minute is not a time that has "passed" — it
  // is the only way this form can say "now", and it is what the card now opens on.
  // Comparing against `now` to the millisecond made that default print a warning
  // about itself for the 59 seconds after the minute turned. Seconds the reader
  // cannot see cannot be the thing that decides.
  const cutoff = startOfMinute(now);
  if (picked.getTime() >= cutoff) return null;
  if (!repeatOn) return PAST_NOTE_ONE_OFF;
  if (!rule) return null;
  const first = firstRuleSlot(rule, picked);
  return first !== null && first.getTime() < cutoff ? PAST_NOTE_CATCH_UP : null;
}

// ---- The first message ---------------------------------------------------
// TITLE AND DESCRIPTION ARE ONE MESSAGE (Akshil, 2026-08-18). The card collects
// a name and a body, and what Claude is sent is both of them: the title as the
// first line, the description under it. Two reasons:
//
//   * the title is real instruction. "Update the changelog" is the whole task
//     most of the time, and a form that sent only the description threw that
//     sentence away — the user had typed the task and then had to type it again
//     underneath. That is what makes the description OPTIONAL now (saveEnabled);
//   * a message that opens with its own heading reads to Claude the way it reads
//     in the list: one titled instruction, not an anonymous paragraph.
//
// A BLANK LINE between them, which is the plainest heading there is in the
// markdown Claude is read in — a single newline would run the two together as
// one paragraph. Either side alone is sent alone: no leading blank line on a
// description-only message (a task from before this rule, re-saved), and no
// trailing one on a title-only task.
export function composeTaskMessage(title: string, description: string): string {
  const name = title.trim();
  const body = description.trim();
  if (!name) return body;
  if (!body) return name;
  return `${name}\n\n${body}`;
}

// WHAT THE PRIMARY BUTTON SAYS. "Save" was the word for a card that only ever
// wrote a row down; the same card now does two genuinely different things, and
// the button is the last thing read before either of them happens (Akshil,
// 2026-08-23).
//
//   Schedule — the picked time is still ahead, or the task repeats. Something is
//              being written into the future, and nothing runs on this press.
//   Create   — the time is now or already past, which is what a card opened from
//              the List or the Board and left alone means. It briefly said "Run"
//              (2026-08-23), but the press creates the task — the run is a
//              consequence — and "Run" over-promised on a card that may still be
//              being written (Akshil, 2026-08-25).
//
// A repeat is always "Schedule" even when its anchor is behind: a past anchor
// gets ONE catch-up run and then a pattern, and "Create" would describe the
// catch-up while saying nothing about the standing rule, which is the bigger fact.
//
// An EDIT reads by the same rule rather than reverting to "Save": moving a task's
// time forward and moving it into the past are the two things an edit does here,
// and they deserve the same two words a create gets.
//
// Minute precision, matching the field and `pastNoteFor`: a card opened on the
// current minute and saved unchanged says Create, not Schedule.
export function saveActionLabel(
  picked: Date | null,
  repeatOn: boolean,
  now: Date,
): "Schedule" | "Create" {
  if (repeatOn) return "Schedule";
  // An unreadable date cannot be claimed to run now. Save is refused on it
  // anyway (saveBlockedReason), so this is only about which word the disabled-
  // looking button wears.
  if (!picked || Number.isNaN(picked.getTime())) return "Schedule";
  return startOfMinute(picked) > startOfMinute(now) ? "Schedule" : "Create";
}

// The body POSTed to /api/schedule — api.ts's own parameter type, nothing
// added to it. That type models `title`, `description` and `new_task_each_run`
// itself, so this alias only names what the builder returns.
export type SchedulePayload = Parameters<typeof scheduleMessage>[0];

export function buildSchedulePayload(form: {
  target: string;
  // The SECOND field on the card: the task's DESCRIPTION, and — joined under
  // the title by `composeTaskMessage` — the second half of the first message
  // Claude is sent. OPTIONAL as of 2026-08-18: a task whose whole instruction
  // fits in its name ("Update the changelog") should not have to say it twice,
  // so an empty one is legal here and the composed message is the title alone.
  // It still rides the wire as `description` when it has content, because the
  // server stores the two separately and a task page with nothing under the
  // title would be the only alternative.
  message: string;
  // The FIRST field on the card, and the REQUIRED one (2026-08-17): a name for
  // the list, and — as of 2026-08-18 — the first line of what Claude is sent.
  // `saveEnabled` refuses Save on a blank one, so what reaches here is a name a
  // human accepted or typed. The wire contract is unchanged — the server still
  // names an untitled task from the transcript's `ai-title` (design §4) — so the
  // empty branch below stays as the honest fallback for a caller this form's
  // gate never saw.
  title: string;
  when: string;
  // The structured rule the current choice means; null for a one-off and for
  // the legacy cron key, whose line is submitted verbatim instead.
  rule: RecurrenceRule | null;
  repeat: string;
  legacyCron: string;
  permission: string;
  // A CHAT HANDOFF's session: the conversation the composer was in when it
  // deep-linked here (?new=1&session_id=…). A one-off continues it; a repeat
  // refuses it, because a task that runs every day must not hijack the user's
  // open chat and compound its context forever.
  sessionId: string;
  // The task's OWN session, and the opposite case: the thread the entry being
  // edited LEARNED when its first run reported the session it ran in, which the
  // server marks as learned at that moment (`session_learned`). Editing is
  // cancel + re-create, so dropping this is how a chaining task silently
  // abandons everything it had built. It outranks a chat's id and survives a
  // repeat — unless the task forks every run below.
  learnedSessionId?: string;
  // Ticked: mint a fresh task — a fresh Claude session — per occurrence,
  // instead of the default, which is every run landing in this task's own
  // thread (design §6).
  newTaskEachRun: boolean;
  // The id of the entry being EDITED, and "" for a new task. An edit is cancel
  // + re-create, so the entry the user is looking at is about to stop existing
  // and a new one with a new id take its place — and a task that has not run yet
  // is NUMBERED on that entry id. Carrying it lets the server move the number
  // across rather than allocate a second one, which is what renamed TASK-078 to
  // TASK-079 when only its time had changed.
  replacesEntryId?: string;
  // DID ANYONE PICK THIS TIME? False when the card was opened from the List or
  // the Board — where the when-row starts folded away — and the user never
  // touched it, so `when` is only the form's own default of "now". The task
  // still runs, and runs immediately; what changes is that the calendar knows
  // not to draw it (design: a plan, not a log). Absent means "yes, treat it as
  // planned", which is what every caller that is not this form means.
  timePicked?: boolean;
  // Uploaded task-shot paths, in attach order. Omitted from the wire when
  // empty, like every other optional here.
  images?: string[];
  // The SAME uploads with the two facts a path loses — the user's filename and
  // the kind the browser settled — in the same order. Sent beside `images`
  // rather than instead of it (D619): the fired run writes the chat's own
  // `<pane-shot>` block from these, so the task's turn shows receipt rows
  // (📄 name, a thumbnail) instead of a list of temp paths, and every reader
  // that only knows `images` keeps working.
  attachments?: TaskAttachment[];
}): SchedulePayload {
  const repeating = form.rule !== null || form.repeat === "cron";
  const trimmedTitle = form.title.trim();
  // The description, trimmed: the padding a textarea collects is not part of
  // what the task is about.
  const trimmedDescription = form.message.trim();
  // WHAT CLAUDE IS ACTUALLY SENT — title and description as one message, not
  // the description alone. See composeTaskMessage.
  const composed = composeTaskMessage(form.title, form.message);
  // WHICH session, if any, the re-created entry continues. The two sources are
  // treated oppositely:
  //   · the task's own (learned) id survives everything except a template that
  //     is meant to fork — an edit that dropped it would orphan the thread the
  //     task had been building, with nothing in the UI saying so;
  //   · a chat's id is continued only while the task stays a one-off.
  // A ticked "new task each run" refuses BOTH: that template mints a fresh
  // session per occurrence, so any id on it is a thread it must not resume.
  const carriesLearned =
    Boolean(form.learnedSessionId) && !(repeating && form.newTaskEachRun);
  const continued = carriesLearned
    ? (form.learnedSessionId ?? "")
    : form.learnedSessionId || repeating
      ? ""
      : form.sessionId;
  return {
    target: form.target.trim(),
    message: composed,
    // A rule rides WITH its anchor (`due` = the first run); the legacy cron
    // line replaces due exactly as it always did; a one-off is due alone.
    ...(form.rule
      ? { due: form.when, rule: form.rule }
      : form.repeat === "cron" && form.legacyCron
        ? { repeats: form.legacyCron }
        : { due: form.when }),
    permission_mode: form.permission,
    // An edit keeps what it cannot re-ask for — see `continued` above.
    ...(continued ? { session_id: continued } : {}),
    // …and re-states WHERE that id came from, so the re-created entry is still
    // marked as owning a learned thread. Without this the marker would die on
    // the first edit and the next one would read the id as a chat handoff —
    // which is the bug this replaced. Never sent for a chat's id: nothing has
    // learned anything yet.
    ...(continued && carriesLearned ? { session_learned: true } : {}),
    // Empty means "the server decides" for the title and "there isn't one" for
    // the description — in both cases the key is better left off the wire than
    // sent as "". The title's empty branch is not reachable from the form (Save
    // refuses a blank one); the description's is the ordinary case of a task
    // named well enough to need no body, and `message` above still carries the
    // title, so nothing empty reaches `schedule.create`.
    ...(trimmedTitle ? { title: trimmedTitle } : {}),
    ...(trimmedDescription ? { description: trimmedDescription } : {}),
    // Only ever sent on a repeating task: on a one-off there is no "each run"
    // for it to mean anything about.
    ...(repeating && form.newTaskEachRun ? { new_task_each_run: true } : {}),
    // Only ever sent as `true`, and only on a one-off: a repeat's anchor is a
    // time somebody chose by definition, and the server refuses the pairing
    // anyway. Left off the wire otherwise, like every other flag here.
    ...(!repeating && form.timePicked === false ? { immediate: true } : {}),
    // Only on an edit, and only as a non-empty string: a new task replaces
    // nothing, and the key is left off the wire rather than sent as "" for the
    // same reason `title` is.
    ...(form.replacesEntryId ? { replaces: form.replacesEntryId } : {}),
    ...(form.images && form.images.length ? { images: form.images } : {}),
    ...(form.attachments && form.attachments.length
      ? { attachments: form.attachments } : {}),
  };
}

// WHAT SAVE REFUSES. Pulled out of the component (it was an inline `ready`
// expression) so the rules are assertable: ONE prose field is required — the
// Title, because a task nobody can name in a list is not a task and because it
// is now also the first line of what Claude is sent (composeTaskMessage). The
// description is OPTIONAL as of 2026-08-18: "Update the changelog" is a whole
// instruction, and a form that made the user write it twice was asking for
// ceremony rather than information.
//
// Title being required is not softened by the field sometimes opening blank —
// that is the point of the requirement rather than a hole in it. The form fills
// the field from the session wherever a session has a name (initialTitleOf plus
// the /api/tasks lookup behind it), and from the chat draft's first line where
// one arrived; where nothing honest is available it asks.
//
// `.trim()`, because a title of spaces is not a name. The field carries
// `aria-required`, and pressing Save while it is empty SAYS SO — see
// `saveBlockedReason`, which is the same set of rules read out loud.
export function saveEnabled(f: {
  // The description. OPTIONAL — it is read by the gate no more; the parameter
  // stays because everything else about the form's state travels in this one
  // object and dropping it would make the two callers assemble two shapes.
  message: string;
  // The task's name. Required, and prefilled rather than asked for.
  title: string;
  // Where it runs. Required, and the async existence check must not be failing.
  target: string;
  pathError: string | null;
  // A "custom" repeat is only a choice once the recurrence dialog produced a
  // rule; a legacy cron template needs its line; everything else needs a
  // parseable date-time.
  repeatOn: boolean;
  repeat: string;
  customRule: RecurrenceRule | null;
  legacyCron: string;
  pickedOk: boolean;
  // The entry has already been re-created by this modal — saving twice would
  // schedule it twice.
  replaced: boolean;
}): boolean {
  return (
    !f.replaced &&
    f.title.trim() !== "" &&
    f.target.trim() !== "" &&
    f.pathError === null &&
    (f.repeatOn && f.repeat === "custom" ? f.customRule !== null : true) &&
    (f.repeat === "cron" ? f.legacyCron !== "" : f.pickedOk)
  );
}

// WHICH FIELD, and where the caret should go. The other half of `saveEnabled`:
// the same rules, in the same order, said as a sentence a person can act on.
//
// Save used to be `disabled` on a false `saveEnabled`, and that is a dead
// control, not a hint. The commonest way to meet it is the commonest thing to
// forget — open the form, type a name, press Save — and a disabled button
// answers by doing NOTHING: no error, no focus move, the modal just sits there
// (QA, 2026-08-18). That press is not blocked at all any more: a title alone is
// a saveable task, and what goes over the wire as `message` is the title (the
// server's "message: cannot be empty" is satisfied by composeTaskMessage, not by
// a second required field).
//
// So Save stays pressable and this is what a press finds. `field` is the ref key
// to focus, because a sentence naming a field the user then has to hunt for is
// half an answer — `null` for the reasons that are not a field (an already-saved
// edit, an unreachable path).
//
// ORDER IS THE READING ORDER OF THE CARD: title, folder, time, repeat. One
// reason at a time, the topmost — a form that lists everything wrong at once
// reads as a scolding, and fixing the first often fixes the rest. The
// description is not in the list any longer: it is optional, so there is no
// sentence to say about an empty one, and "message" is gone from `field` with
// it rather than kept as a case nothing can return.
export function saveBlockedReason(f: Parameters<typeof saveEnabled>[0]): {
  text: string;
  field: "title" | "target" | null;
} | null {
  if (f.replaced) {
    return {
      text: "This task is already saved — close the card and edit it again to change it.",
      field: null,
    };
  }
  if (f.title.trim() === "") {
    // The sentence asks for the TASK, not for a name, because that is what the
    // field asks for now ("What should Claude do?"). "Give the task a name" sent
    // the user looking for a label to invent, when what is missing is the
    // instruction itself — and this empty field means there is no message to
    // send at all. The caret lands in the same place either way: the primary
    // field is where the answer goes.
    return {
      text: "Say what Claude should do — a task with no instructions has nothing to run.",
      field: "title",
    };
  }
  if (f.target.trim() === "") {
    return { text: "Pick the folder or file this task runs against.", field: "target" };
  }
  // The path check has already written its own sentence into the field; naming
  // it again in the banner would say the same thing twice.
  if (f.pathError !== null) {
    return { text: f.pathError, field: "target" };
  }
  if (f.repeatOn && f.repeat === "custom" && f.customRule === null) {
    return { text: "Finish the custom repeat, or pick one of the presets.", field: null };
  }
  if (f.repeat === "cron" ? f.legacyCron === "" : !f.pickedOk) {
    return { text: "Pick a date and a time for the first run.", field: null };
  }
  return null;
}
