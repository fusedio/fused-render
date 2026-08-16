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

beforeAll(async () => {
  const mod = await import("./NewJobModal");
  initialRepeatKey = mod.initialRepeatKey;
  applyRepeatToggle = mod.applyRepeatToggle;
  buildSchedulePayload = mod.buildSchedulePayload;
  learnedSessionOf = mod.learnedSessionOf;
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
  message: "pull today's news",
  title: "",
  description: "",
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
  test("a one-off is target, message, due and permission", () => {
    expect(buildSchedulePayload(form())).toEqual({
      target: "/tmp/work",
      message: "pull today's news",
      due: "2026-08-17T09:00",
      permission_mode: "auto",
    });
  });

  test("title and description ride along, trimmed", () => {
    const body = buildSchedulePayload(
      form({ title: "  Morning news  ", description: "  a note  " }),
    );
    expect(body.title).toBe("Morning news");
    expect(body.description).toBe("a note");
  });

  test("an empty title is left OFF the wire — that is what asks the server to fill it in", () => {
    const body = buildSchedulePayload(form({ title: "   ", description: "" }));
    expect("title" in body).toBe(false);
    expect("description" in body).toBe(false);
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

// Editing is cancel + re-create, so every edit re-states the whole task — and
// what it fails to re-state is LOST. The thread a repeating task has been
// building is exactly that kind of thing: nothing on the form asks for it, the
// backend wrote it onto the template after run 1, and re-creating without it
// orphans everything the task had done. So the two ids that can reach the
// payload are told apart: the CHAT's (a handoff, from the deep link) and the
// TASK's own (learned, already on the entry).
describe("whose session the entry is carrying", () => {
  test("a repeating template's id is the thread it LEARNED on its first run", () => {
    expect(learnedSessionOf(entry({ rule: DAILY, session_id: "own" }))).toBe("own");
    expect(learnedSessionOf(entry({ repeats: "0 9 * * *", session_id: "own" }))).toBe("own");
  });

  test("a one-off's id is only a chat handoff, so it is not a learned thread", () => {
    // It still travels — as a chat's id, under a chat's rules — so ticking
    // Repeat on a task scheduled from a conversation does not sign that
    // conversation up to be appended to forever.
    expect(learnedSessionOf(entry({ session_id: "chat" }))).toBe("");
  });

  test("and a task that has not started one has nothing to carry", () => {
    expect(learnedSessionOf(entry({ rule: DAILY }))).toBe("");
    expect(learnedSessionOf(null)).toBe("");
    expect(learnedSessionOf(undefined)).toBe("");
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
