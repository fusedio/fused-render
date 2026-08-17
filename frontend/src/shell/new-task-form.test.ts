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

beforeAll(async () => {
  const mod = await import("./NewJobModal");
  initialRepeatKey = mod.initialRepeatKey;
  applyRepeatToggle = mod.applyRepeatToggle;
  buildSchedulePayload = mod.buildSchedulePayload;
  learnedSessionOf = mod.learnedSessionOf;
  initialAskOf = mod.initialAskOf;
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
