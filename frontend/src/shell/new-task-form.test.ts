// The pure decisions inside the New task form (NewJobModal.tsx): what the
// Repeat checkbox does to the repeat state, which state an Edit opens on, and
// exactly what goes on the wire.
//
// The module is a React component file, so it is imported DYNAMICALLY after a
// handful of browser globals are stubbed — importing it pulls in the router,
// which reads `location` at module init. Nothing below renders anything; these
// three exports are plain functions, which is why they were pulled out of the
// component in the first place.
import { beforeAll, describe, expect, test } from "bun:test";
import type { RecurrenceRule, ScheduledMessage } from "@platform/lib/api";

const g = globalThis as unknown as Record<string, unknown>;
g.location = { pathname: "/scheduled", search: "", hash: "", href: "http://x/scheduled", origin: "http://x" };
g.history = { replaceState() {}, pushState() {}, state: null };
g.window = globalThis;
g.document = { addEventListener() {}, removeEventListener() {}, querySelector: () => null };

type Form = Parameters<typeof import("./NewJobModal").buildSchedulePayload>[0];

let initialRepeatKey: typeof import("./NewJobModal").initialRepeatKey;
let applyRepeatToggle: typeof import("./NewJobModal").applyRepeatToggle;
let buildSchedulePayload: typeof import("./NewJobModal").buildSchedulePayload;
let learnedSessionOf: typeof import("./NewJobModal").learnedSessionOf;
let initialAskOf: typeof import("./NewJobModal").initialAskOf;
let deleteActionFor: typeof import("./NewJobModal").deleteActionFor;
let deleteFailureText: typeof import("./NewJobModal").deleteFailureText;
let sessionTitleOf: typeof import("./NewJobModal").sessionTitleOf;
let titlePlaceholderFor: typeof import("./NewJobModal").titlePlaceholderFor;
let deletePress: typeof import("./NewJobModal").deletePress;
let TITLE_PLACEHOLDER: typeof import("./NewJobModal").TITLE_PLACEHOLDER;
let pastNoteFor: typeof import("./NewJobModal").pastNoteFor;
let PAST_NOTE_ONE_OFF: typeof import("./NewJobModal").PAST_NOTE_ONE_OFF;
let PAST_NOTE_CATCH_UP: typeof import("./NewJobModal").PAST_NOTE_CATCH_UP;

beforeAll(async () => {
  const mod = await import("./NewJobModal");
  initialRepeatKey = mod.initialRepeatKey;
  applyRepeatToggle = mod.applyRepeatToggle;
  buildSchedulePayload = mod.buildSchedulePayload;
  learnedSessionOf = mod.learnedSessionOf;
  initialAskOf = mod.initialAskOf;
  deleteActionFor = mod.deleteActionFor;
  deleteFailureText = mod.deleteFailureText;
  sessionTitleOf = mod.sessionTitleOf;
  titlePlaceholderFor = mod.titlePlaceholderFor;
  deletePress = mod.deletePress;
  TITLE_PLACEHOLDER = mod.TITLE_PLACEHOLDER;
  pastNoteFor = mod.pastNoteFor;
  PAST_NOTE_ONE_OFF = mod.PAST_NOTE_ONE_OFF;
  PAST_NOTE_CATCH_UP = mod.PAST_NOTE_CATCH_UP;
});

// Only the fields these functions read; the rest of a stored entry is noise
// here.
const entry = (over: Partial<ScheduledMessage>): ScheduledMessage =>
  ({
    id: "s1",
    target: "/tmp",
    message: "pull today's news",
    due: "2026-08-17T09:00:00.000Z",
    session_id: "",
    permission_mode: "auto",
    state: "pending",
    created: "",
    fired: "",
    run_id: "",
    error: "",
    ...over,
  }) as ScheduledMessage;

const DAILY: RecurrenceRule = { freq: "day" };

const form = (over: Partial<Form> = {}): Form => ({
  target: " /tmp/work ",
  // The big field: the message AND the description. There is no third text
  // field on the form any more (Akshil, 2026-08-17).
  message: "pull today's news",
  title: "",
  when: "2026-08-17T09:00",
  rule: null,
  repeat: "none",
  legacyCron: "",
  permission: "auto",
  sessionId: "",
  newTaskEachRun: false,
  ...over,
});

describe("which state the form opens on", () => {
  test("a new task opens with Repeat unticked", () => {
    expect(initialRepeatKey(null)).toBe("none");
    expect(initialRepeatKey(undefined)).toBe("none");
    expect(initialRepeatKey(entry({}))).toBe("none");
  });

  test("editing a repeating task opens ticked, on its own rule", () => {
    const key = initialRepeatKey(entry({ rule: DAILY }));
    expect(key).not.toBe("none"); // "not none" IS the checkbox being ticked
    expect(key).toBe("daily");
  });

  test("editing a legacy cron template opens ticked too", () => {
    expect(initialRepeatKey(entry({ repeats: "0 9 * * *" }))).toBe("cron");
  });
});

describe("the Repeat checkbox", () => {
  test("ticking an unset form lands on a real choice, not a blank menu", () => {
    expect(applyRepeatToggle(true, { repeat: "none", customRule: null })).toEqual({
      repeat: "daily",
      customRule: null,
    });
  });

  test("ticking a form that already carries a rule leaves it alone", () => {
    const custom: RecurrenceRule = { freq: "week", byday: [1, 3] };
    expect(applyRepeatToggle(true, { repeat: "custom", customRule: custom })).toEqual({
      repeat: "custom",
      customRule: custom,
    });
  });

  test("unticking CLEARS the rule — nothing stays armed behind the hidden menu", () => {
    expect(
      applyRepeatToggle(false, { repeat: "custom", customRule: { freq: "month" } }),
    ).toEqual({ repeat: "none", customRule: null });
    expect(applyRepeatToggle(false, { repeat: "weekly", customRule: null })).toEqual({
      repeat: "none",
      customRule: null,
    });
  });

  test("and the cleared state submits a plain one-off", () => {
    // The end-to-end of the case above: whatever the dropdown last said, the
    // body that reaches the server carries no rule and no cron line.
    const cleared = applyRepeatToggle(false, { repeat: "custom", customRule: { freq: "month" } });
    const body = buildSchedulePayload(
      form({
        rule: cleared.customRule,
        repeat: cleared.repeat,
        legacyCron: "0 9 * * *", // an edited legacy template, un-repeated
        newTaskEachRun: true, // …and a flag that no longer means anything
      }),
    );
    expect(body.rule).toBeUndefined();
    expect(body.repeats).toBeUndefined();
    expect(body.new_task_each_run).toBeUndefined();
    expect(body.due).toBe("2026-08-17T09:00");
  });
});

describe("the payload", () => {
  test("a one-off is target, the ask (twice), due and permission", () => {
    expect(buildSchedulePayload(form())).toEqual({
      target: "/tmp/work",
      message: "pull today's news",
      // Same text, second key: the one big field is the description too.
      description: "pull today's news",
      due: "2026-08-17T09:00",
      permission_mode: "auto",
    });
  });

  test("the big field IS the description — one field, both keys", () => {
    const body = buildSchedulePayload(form({ message: "  summarise the inbox  " }));
    // The message goes verbatim, because that is literally what Claude
    // receives; the description is the same text tidied up.
    expect(body.message).toBe("  summarise the inbox  ");
    expect(body.description).toBe("summarise the inbox");
  });

  test("a title the user typed rides along, trimmed", () => {
    expect(buildSchedulePayload(form({ title: "  Morning news  " })).title).toBe("Morning news");
  });

  test("an empty title is left OFF the wire — that is what asks the server to fill it in", () => {
    const body = buildSchedulePayload(form({ title: "   " }));
    expect("title" in body).toBe(false);
    // …and the description is still there, because it is not the title's
    // business: the ask always fills it.
    expect(body.description).toBe("pull today's news");
  });

  test("new_task_each_run is sent only when the task actually repeats", () => {
    expect(
      buildSchedulePayload(form({ rule: DAILY, repeat: "daily", newTaskEachRun: true })),
    ).toMatchObject({ rule: DAILY, due: "2026-08-17T09:00", new_task_each_run: true });
    // ticked but not repeating: meaningless, so not sent
    expect(buildSchedulePayload(form({ newTaskEachRun: true })).new_task_each_run).toBeUndefined();
    // repeating but not ticked: the default (every run into the same thread)
    expect(
      buildSchedulePayload(form({ rule: DAILY, repeat: "daily" })).new_task_each_run,
    ).toBeUndefined();
  });

  test("a legacy cron line still replaces due, and can carry the flag", () => {
    const body = buildSchedulePayload(
      form({ repeat: "cron", legacyCron: "0 9 * * *", newTaskEachRun: true }),
    );
    expect(body.repeats).toBe("0 9 * * *");
    expect(body.due).toBeUndefined();
    expect(body.new_task_each_run).toBe(true);
  });

  test("a session is continued by a one-off and dropped by a repeat", () => {
    expect(buildSchedulePayload(form({ sessionId: "abc" })).session_id).toBe("abc");
    expect(
      buildSchedulePayload(form({ sessionId: "abc", rule: DAILY, repeat: "daily" })).session_id,
    ).toBeUndefined();
  });
});

// The form asks for prose ONCE. Everything the server stores as two values —
// `message` and `description` — has to fold back into that one field when an
// Edit opens, and come out the other side unchanged when it is saved again.
describe("what an Edit opens the big field on", () => {
  test("a task written by the two-field form: message and description agree", () => {
    expect(initialAskOf(entry({ description: "pull today's news" }))).toBe("pull today's news");
  });

  test("prose that lives only in description still fills the field", () => {
    // Not blank, and not re-created empty: the field is the description now, so
    // a description with no message behind it is the answer.
    expect(initialAskOf(entry({ message: "", description: "a note" }))).toBe("a note");
  });

  test("a chat draft fills a NEW task, and never outranks the entry", () => {
    expect(initialAskOf(null, "draft from the composer")).toBe("draft from the composer");
    expect(initialAskOf(entry({}), "draft from the composer")).toBe("pull today's news");
    expect(initialAskOf(undefined)).toBe("");
  });

  test("the ask and the title both survive the save → edit → save round trip", () => {
    const saved = buildSchedulePayload(
      form({ message: "pull today's news", title: "Morning news" }),
    );
    // What the server would have stored, read back into the form's two fields.
    const stored = entry({
      message: saved.message,
      description: saved.description,
      title: saved.title,
    });
    expect(initialAskOf(stored)).toBe("pull today's news");
    expect(stored.title).toBe("Morning news");

    // And re-saving that edit sends the same three values back — an edit is
    // cancel + re-create, so anything the form fails to re-state is LOST.
    const again = buildSchedulePayload(
      form({ message: initialAskOf(stored), title: stored.title ?? "" }),
    );
    expect(again.message).toBe("pull today's news");
    expect(again.description).toBe("pull today's news");
    expect(again.title).toBe("Morning news");
  });

  test("an untitled task edits back untitled — the server keeps naming it", () => {
    const saved = buildSchedulePayload(form({ title: "" }));
    expect("title" in saved).toBe(false);
    const stored = entry({ message: saved.message, description: saved.description });
    const again = buildSchedulePayload(
      form({ message: initialAskOf(stored), title: stored.title ?? "" }),
    );
    expect("title" in again).toBe(false);
    expect(again.description).toBe("pull today's news");
  });
});

// Editing is cancel + re-create, so every edit re-states the whole task — and
// what it fails to re-state is LOST. The thread a repeating task has been
// building is exactly that kind of thing: nothing on the form asks for it, the
// backend wrote it onto the template after run 1, and re-creating without it
// orphans everything the task had done. So the two ids that can reach the
// payload are told apart: the CHAT's (a handoff, from the deep link) and the
// TASK's own (learned, already on the entry).
describe("whose session the entry is carrying", () => {
  test("the id the server MARKED as learned is the task's own thread", () => {
    expect(
      learnedSessionOf(entry({ rule: DAILY, session_id: "own", session_learned: true })),
    ).toBe("own");
    expect(
      learnedSessionOf(
        entry({ repeats: "0 9 * * *", session_id: "own", session_learned: true }),
      ),
    ).toBe("own");
  });

  test("an UNMARKED id is only a chat handoff, so it is not a learned thread", () => {
    // It still travels — as a chat's id, under a chat's rules — so ticking
    // Repeat on a task scheduled from a conversation does not sign that
    // conversation up to be appended to forever. A repeating entry is read the
    // same way: repeating-ness says nothing about where the id came from.
    expect(learnedSessionOf(entry({ session_id: "chat" }))).toBe("");
    expect(learnedSessionOf(entry({ rule: DAILY, session_id: "chat" }))).toBe("");
    expect(
      learnedSessionOf(entry({ session_id: "chat", session_learned: false })),
    ).toBe("");
  });

  test("a marker on an entry hand-edited to nonsense claims nothing", () => {
    // The store is a JSON file a person may edit, and the server reads flags
    // the same strict way (`_flag`). Anything that is not exactly `true` is
    // read as "the user supplied this id".
    const junk = entry({ session_id: "own" }) as Omit<ScheduledMessage, "session_learned"> & {
      session_learned: unknown;
    };
    junk.session_learned = "true";
    expect(learnedSessionOf(junk as ScheduledMessage)).toBe("");
  });

  test("and a task that has not started one has nothing to carry", () => {
    expect(learnedSessionOf(entry({ rule: DAILY }))).toBe("");
    // A marker with no id behind it is not an id.
    expect(learnedSessionOf(entry({ rule: DAILY, session_learned: true }))).toBe("");
    expect(learnedSessionOf(null)).toBe("");
    expect(learnedSessionOf(undefined)).toBe("");
  });
});

// The round trip the inferred reading could not survive (Bugbot, PR #555): a
// chaining template learns a thread, the user unticks Repeat — the learned id
// deliberately rides onto the one-off — and later ticks Repeat back on. Read
// off repeating-ness, the id looked like a chat handoff at that last step and
// was dropped, orphaning everything the task had built. Read off the marker,
// which the re-create re-states, it survives.
describe("repeat → one-off → repeat", () => {
  test("a learned thread survives the demotion and the promotion back", () => {
    const learned = entry({ rule: DAILY, session_id: "S", session_learned: true });
    // 1. demoted: the id travels onto the one-off, still marked.
    const demoted = buildSchedulePayload(
      form({ learnedSessionId: learnedSessionOf(learned) }),
    );
    expect(demoted.session_id).toBe("S");
    expect(demoted.session_learned).toBe(true);
    expect(demoted.rule).toBeUndefined();

    // 2. promoted back: the stored one-off is what the form reads next time.
    const oneOff = entry({ session_id: "S", session_learned: true });
    const promoted = buildSchedulePayload(
      form({ rule: DAILY, repeat: "daily", learnedSessionId: learnedSessionOf(oneOff) }),
    );
    expect(promoted.session_id).toBe("S");
    expect(promoted.session_learned).toBe(true);
  });

  test("but a chat-scheduled one-off promoted to repeating still drops the chat", () => {
    const chat = entry({ session_id: "chat" });
    expect(
      buildSchedulePayload(
        form({
          rule: DAILY,
          repeat: "daily",
          sessionId: chat.session_id,
          learnedSessionId: learnedSessionOf(chat),
        }),
      ).session_id,
    ).toBeUndefined();
  });

  test("and the marker is never claimed for a chat's id", () => {
    expect(buildSchedulePayload(form({ sessionId: "chat" })).session_learned).toBeUndefined();
    // Nor for a learned id the fork flag refuses.
    expect(
      buildSchedulePayload(
        form({ rule: DAILY, repeat: "daily", newTaskEachRun: true, learnedSessionId: "own" }),
      ).session_learned,
    ).toBeUndefined();
  });
});

describe("which session an edit carries", () => {
  test("editing a chaining repeating task keeps the thread it learned", () => {
    const body = buildSchedulePayload(
      form({ rule: DAILY, repeat: "daily", learnedSessionId: "sess-learned" }),
    );
    expect(body.session_id).toBe("sess-learned");
    expect(body.rule).toEqual(DAILY);
  });

  test("…and a legacy cron template chains the same way", () => {
    expect(
      buildSchedulePayload(
        form({ repeat: "cron", legacyCron: "0 9 * * *", learnedSessionId: "sess-learned" }),
      ).session_id,
    ).toBe("sess-learned");
  });

  test("but a template that forks every run carries no thread at all", () => {
    // "New task each run" means a fresh session per occurrence; an id on that
    // template is a thread it must NOT resume.
    expect(
      buildSchedulePayload(
        form({
          rule: DAILY,
          repeat: "daily",
          newTaskEachRun: true,
          learnedSessionId: "sess-learned",
        }),
      ).session_id,
    ).toBeUndefined();
  });

  test("the task's own thread outranks a chat the form was opened from", () => {
    // Editing from a composer deep link: the chat's id is the one the repeat
    // rule was always meant to refuse, so the entry's wins.
    expect(
      buildSchedulePayload(
        form({ rule: DAILY, repeat: "daily", sessionId: "chat", learnedSessionId: "own" }),
      ).session_id,
    ).toBe("own");
    expect(
      buildSchedulePayload(form({ sessionId: "chat", learnedSessionId: "own" })).session_id,
    ).toBe("own");
  });

  test("a repeat created fresh FROM a chat still refuses the chat's session", () => {
    // The unchanged half of the rule: no learned id, so there is nothing to
    // continue and the open conversation is not hijacked.
    expect(
      buildSchedulePayload(
        form({ sessionId: "chat", learnedSessionId: "", rule: DAILY, repeat: "daily" }),
      ).session_id,
    ).toBeUndefined();
  });

  test("and a one-off is untouched by any of it", () => {
    expect(buildSchedulePayload(form({ sessionId: "chat" })).session_id).toBe("chat");
    // The fork flag is meaningless on a one-off, so it does not eat the
    // session either.
    expect(
      buildSchedulePayload(form({ learnedSessionId: "own", newTaskEachRun: true })).session_id,
    ).toBe("own");
  });
});

// The gap this closed (Akshil, 2026-08-17): every cancel the UI offered was
// scoped to ONE occurrence, so a repeating rule could be skipped run by run for
// ever and kept minting more. The modal is where a template is addressable — an
// occurrence's Edit resolves to its template — so the stop lives there.
describe("the modal's Delete action", () => {
  test("is not offered when creating", () => {
    expect(deleteActionFor(null)).toBeNull();
    expect(deleteActionFor(undefined)).toBeNull();
  });

  test("names the SERIES when editing a recurring template", () => {
    const action = deleteActionFor(entry({ id: "tpl", state: "recurring", rule: DAILY }));
    expect(action).not.toBeNull();
    expect(action!.series).toBe(true);
    // Cancelling the TEMPLATE id is what stops the rule; an occurrence id would
    // only skip one run.
    expect(action!.id).toBe("tpl");
    expect(action!.label).toBe("Delete task");
    expect(action!.confirm).toBe("Delete and stop all future runs?");
    // The consequence is spelled out, not implied.
    expect(action!.confirm).toContain("future runs");
  });

  test("names the ONE RUN when editing a pending one-off", () => {
    const action = deleteActionFor(entry({ id: "s1", state: "pending" }));
    expect(action).not.toBeNull();
    expect(action!.series).toBe(false);
    expect(action!.id).toBe("s1");
    // Same label — the user is deleting a task either way…
    expect(action!.label).toBe("Delete task");
    // …and the second press is where the two part company.
    expect(action!.confirm).toBe("Delete and cancel this run?");
    expect(action!.confirm).not.toContain("future runs");
  });

  test("is not offered for anything the server would refuse", () => {
    // `sending` is deliberately not cancellable — the helper is already away
    // (schedule.cancel) — and a terminal entry has nothing left to stop. A
    // button that 404s on press is worse than no button, so there is none.
    for (const state of ["sending", "sent", "missed", "error", "cancelled"] as const) {
      expect(deleteActionFor(entry({ state }))).toBeNull();
    }
  });
});

describe("the Delete confirm step", () => {
  test("the first press arms and carries no id — nothing can be cancelled by it", () => {
    const press = deletePress(deleteActionFor(entry({ id: "tpl", state: "recurring" })), false);
    expect(press).toEqual({ do: "arm" });
    expect(press).not.toHaveProperty("id");
  });

  test("only the second press produces the id to cancel", () => {
    expect(deletePress(deleteActionFor(entry({ id: "tpl", state: "recurring" })), true)).toEqual({
      do: "delete",
      id: "tpl",
    });
  });

  test("a press decides nothing at all when there is nothing to delete", () => {
    // The refusal reaches the press too, not just the render: a `sending`
    // entry has no action, so neither press can produce an id.
    const away = deleteActionFor(entry({ state: "sending" }));
    expect(away).toBeNull();
    expect(deletePress(away, false)).toBeNull();
    expect(deletePress(away, true)).toBeNull();
  });
});

describe("what a failed Delete says", () => {
  const notFound = Object.assign(new Error("no cancellable scheduled message with id 'tpl'"), {
    status: 404,
  });

  test("a 404 reads as already-gone, not as a failure", () => {
    // The race worth being honest about: the run fired, or another tab
    // cancelled it. The server's id-bearing sentence reads as a bug, so it is
    // translated — and only here.
    expect(deleteFailureText(notFound, false)).toBe(
      "This task is already gone — it has run, or it was cancelled somewhere else.",
    );
    expect(deleteFailureText(notFound, true)).toBe(
      "This task is already stopped — nothing is scheduled to run from it any more.",
    );
    for (const series of [true, false]) {
      expect(deleteFailureText(notFound, series)).not.toContain("failed");
      expect(deleteFailureText(notFound, series)).not.toContain("'tpl'");
    }
  });

  test("anything else keeps the server's own words", () => {
    const boom = Object.assign(new Error("the schedule file is read-only"), { status: 500 });
    expect(deleteFailureText(boom, true)).toBe("the schedule file is read-only");
  });

  test("and a wordless failure still says something", () => {
    expect(deleteFailureText(new Error(""), false)).toBe("The task could not be deleted.");
    expect(deleteFailureText(null, false)).toBe("The task could not be deleted.");
  });
});

// Scheduling from an open chat: the session already has a name, so the form
// PREVIEWS it on Title instead of prefilling it. Prefilling would freeze the
// name — an explicit title beats Claude Code's per-turn `ai-title` for ever —
// and it would look like something the user typed (Akshil, 2026-08-17).
describe("the Title placeholder", () => {
  const task = (over: Record<string, unknown> = {}) => ({
    session_id: "sess-1",
    title: "Porting the parquet reader",
    title_source: "ai",
    ...over,
  });

  test("previews the session's own name", () => {
    expect(sessionTitleOf([task()], "sess-1")).toBe("Porting the parquet reader");
    expect(titlePlaceholderFor("Porting the parquet reader", false)).toBe(
      "Porting the parquet reader",
    );
  });

  test("a title the user already gave this thread is a name too", () => {
    expect(sessionTitleOf([task({ title_source: "user" })], "sess-1")).toBe(
      "Porting the parquet reader",
    );
  });

  test("but the first line of a message is not a name", () => {
    // `message`-sourced is the message echoed back; the generic placeholder
    // already says the title fills itself in, so this adds nothing and reads
    // like the wrong field.
    expect(sessionTitleOf([task({ title_source: "message" })], "sess-1")).toBe("");
  });

  test("no session, no match and no title all fall back to the generic line", () => {
    expect(sessionTitleOf([task()], "")).toBe("");
    expect(sessionTitleOf([task()], "sess-2")).toBe("");
    expect(sessionTitleOf([task({ title: "   " })], "sess-1")).toBe("");
    expect(titlePlaceholderFor("", false)).toBe(TITLE_PLACEHOLDER);
    expect(titlePlaceholderFor("   ", false)).toBe(TITLE_PLACEHOLDER);
    expect(TITLE_PLACEHOLDER).toBe("Title — optional, filled in automatically");
  });

  test("ticking Repeat takes the preview away, because it takes the session away", () => {
    // A repeat refuses the chat's session and opens its own thread, so there is
    // no conversation whose name would carry over — the preview would be a lie
    // from the moment the box is ticked.
    expect(titlePlaceholderFor("Porting the parquet reader", true)).toBe(TITLE_PLACEHOLDER);
    // …and this is the payload rule it mirrors, asserted above too.
    expect(
      buildSchedulePayload(form({ sessionId: "sess-1", rule: DAILY, repeat: "daily" })).session_id,
    ).toBeUndefined();
  });
});

// What the when-row says about a time already gone. Two answers, because the
// server has two behaviours: a one-off runs once as soon as it can (SCH-3b),
// and a past-ANCHORED rule materializes one catch-up on its latest past slot
// and then keeps its pattern (SCH-13b). The note used to be scoped to the
// one-off, which left a repeat firing on save with the form silent about it
// (Bugbot, PR #555).
describe("the past-time note", () => {
  // Wednesday 10:00. The week these dates sit in starts Sunday Aug 16.
  const NOW = new Date("2026-08-19T10:00");
  const at = (iso: string) => new Date(iso);
  const TUE = at("2026-08-18T09:00"); // past
  const THU = at("2026-08-20T09:00"); // future
  const note = (
    picked: Date | null,
    repeatOn: boolean,
    rule: RecurrenceRule | null,
    now = NOW,
  ) => pastNoteFor(picked, repeatOn, rule, now);

  test("a past one-off says it runs as soon as it can", () => {
    expect(note(TUE, false, null)).toBe(PAST_NOTE_ONE_OFF);
    // The boundary is inclusive: this instant has passed.
    expect(note(NOW, false, null)).toBe(PAST_NOTE_ONE_OFF);
  });

  test("a past ANCHOR under a ticked Repeat says its own thing instead", () => {
    expect(note(TUE, true, DAILY)).toBe(PAST_NOTE_CATCH_UP);
  });

  test("and the two sentences are genuinely different, because the outcome is", () => {
    expect(PAST_NOTE_CATCH_UP).not.toBe(PAST_NOTE_ONE_OFF);
    // The repeat's promise is the pair the one-off cannot make: one run now,
    // and then the pattern.
    expect(PAST_NOTE_CATCH_UP).toContain("catch-up");
    expect(PAST_NOTE_CATCH_UP).toContain("schedule");
  });

  test("an anchor a YEAR back is still that one sentence — never a backlog", () => {
    // `_coalesce` collapses every intervening slot, so the wording must not
    // scale with how far back the anchor is, and neither may the note.
    expect(note(at("2025-01-01T09:00"), true, DAILY)).toBe(PAST_NOTE_CATCH_UP);
    expect(note(at("2025-01-01T09:00"), true, { freq: "hour" })).toBe(PAST_NOTE_CATCH_UP);
    expect(PAST_NOTE_CATCH_UP).toContain("Just the one");
  });

  test("a FUTURE time is silent, Repeat ticked or not", () => {
    expect(note(THU, false, null)).toBeNull();
    expect(note(THU, true, DAILY)).toBeNull();
    expect(note(THU, true, { freq: "month", monthly: "nth-weekday" })).toBeNull();
  });

  test("a repeat with no anchor to catch up from is silent too", () => {
    // A legacy cron template computes its first run from now by construction,
    // and an unfinished Custom cannot be saved at all. Neither is the one-off
    // wording — that would promise a run the rule will not make.
    expect(note(TUE, true, null)).toBeNull();
  });

  test("a past anchor whose FIRST slot is still ahead is silent", () => {
    // Tuesday anchor, only Thursday ticked: the series starts on the Thursday
    // (recur's partial first week), which has not come round yet.
    expect(note(TUE, true, { freq: "week", byday: [4] })).toBeNull();
    // Tuesday anchor, only Monday ticked: the anchor's own Monday is behind
    // the anchor, so the series starts a week on.
    expect(note(TUE, true, { freq: "week", byday: [1] })).toBeNull();
  });

  test("…but one whose first slot has already gone is not", () => {
    // The anchor's own day counts when it is one of the chosen ones.
    expect(note(TUE, true, { freq: "week", byday: [1, 2] })).toBe(PAST_NOTE_CATCH_UP);
    // Sunday anchor, Wednesday ticked: the first slot is this morning, gone an
    // hour ago — the walk forward from the anchor, not the anchor itself.
    expect(note(at("2026-08-16T09:00"), true, { freq: "week", byday: [3] })).toBe(
      PAST_NOTE_CATCH_UP,
    );
  });

  test("a rule whose `until` ran out before it began is silent", () => {
    const anchor = at("2026-08-10T09:00");
    expect(note(anchor, true, { ...DAILY, until: "2026-08-09" })).toBeNull();
    // Inclusive on the DATE, so an end ON the first slot's day still runs it.
    expect(note(anchor, true, { ...DAILY, until: "2026-08-10" })).toBe(PAST_NOTE_CATCH_UP);
  });

  test("an unparseable time says nothing at all", () => {
    expect(note(new Date("nonsense"), false, null)).toBeNull();
    expect(note(null, true, DAILY)).toBeNull();
  });
});

// The note is a STATEMENT, not an objection: "start this pattern, and run the
// one I missed" is a legitimate ask, so Save stays armed. Asserted against the
// source because `ready` is the component's own local — the guard that matters
// is that it never learns about the note.
describe("the past-time note never blocks Save", () => {
  test("`ready` does not consult it", async () => {
    const src = await Bun.file(
      new URL("./NewJobModal.tsx", import.meta.url).pathname,
    ).text();
    const ready = src.slice(src.indexOf("const ready ="));
    expect(ready).not.toBe("");
    expect(ready.slice(0, ready.indexOf(";"))).not.toContain("pastNote");
  });

  test("and a past-anchored repeat still builds a whole payload", () => {
    const body = buildSchedulePayload(
      form({ when: "2025-01-01T09:00", rule: DAILY, repeat: "daily" }),
    );
    expect(body.rule).toEqual(DAILY);
    expect(body.due).toBe("2025-01-01T09:00");
  });
});
