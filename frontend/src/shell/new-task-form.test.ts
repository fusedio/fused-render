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
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { RecurrenceRule, ScheduledMessage } from "@platform/lib/api";

const g = globalThis as unknown as Record<string, unknown>;
g.location = { pathname: "/tasks", search: "", hash: "", href: "http://x/tasks", origin: "http://x" };
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
let initialTitleOf: typeof import("./NewJobModal").initialTitleOf;
let initialTitleStateOf: typeof import("./NewJobModal").initialTitleStateOf;
let firstLine: typeof import("./NewJobModal").firstLine;
let shortTitle: typeof import("./NewJobModal").shortTitle;
let TITLE_MAX: typeof import("./NewJobModal").TITLE_MAX;
let deletePress: typeof import("./NewJobModal").deletePress;
let saveEnabled: typeof import("./NewJobModal").saveEnabled;
let saveBlockedReason: typeof import("./NewJobModal").saveBlockedReason;
let TITLE_PLACEHOLDER: typeof import("./NewJobModal").TITLE_PLACEHOLDER;
let ASK_PLACEHOLDER: typeof import("./NewJobModal").ASK_PLACEHOLDER;
let composeTaskMessage: typeof import("./NewJobModal").composeTaskMessage;
let withoutTitleHeading: typeof import("./NewJobModal").withoutTitleHeading;
let splitDraft: typeof import("./NewJobModal").splitDraft;
let pastNoteFor: typeof import("./NewJobModal").pastNoteFor;
let PAST_NOTE_ONE_OFF: typeof import("./NewJobModal").PAST_NOTE_ONE_OFF;
let PAST_NOTE_CATCH_UP: typeof import("./NewJobModal").PAST_NOTE_CATCH_UP;
let defaultTargetOf: typeof import("./NewJobModal").defaultTargetOf;
let targetVerdict: typeof import("./NewJobModal").targetVerdict;
let splitTargetPath: typeof import("./NewJobModal").splitTargetPath;
let PATH_MISSING: typeof import("./NewJobModal").PATH_MISSING;
let twoLevelsMissing: typeof import("./NewJobModal").twoLevelsMissing;

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
  initialTitleOf = mod.initialTitleOf;
  initialTitleStateOf = mod.initialTitleStateOf;
  firstLine = mod.firstLine;
  shortTitle = mod.shortTitle;
  TITLE_MAX = mod.TITLE_MAX;
  deletePress = mod.deletePress;
  saveEnabled = mod.saveEnabled;
  saveBlockedReason = mod.saveBlockedReason;
  TITLE_PLACEHOLDER = mod.TITLE_PLACEHOLDER;
  ASK_PLACEHOLDER = mod.ASK_PLACEHOLDER;
  composeTaskMessage = mod.composeTaskMessage;
  withoutTitleHeading = mod.withoutTitleHeading;
  splitDraft = mod.splitDraft;
  pastNoteFor = mod.pastNoteFor;
  PAST_NOTE_ONE_OFF = mod.PAST_NOTE_ONE_OFF;
  PAST_NOTE_CATCH_UP = mod.PAST_NOTE_CATCH_UP;
  defaultTargetOf = mod.defaultTargetOf;
  targetVerdict = mod.targetVerdict;
  splitTargetPath = mod.splitTargetPath;
  PATH_MISSING = mod.PATH_MISSING;
  twoLevelsMissing = mod.twoLevelsMissing;
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
  // The SECOND field: the task's description, and the body of the message
  // composed from it and the title. Optional as of 2026-08-18.
  message: "pull today's news",
  // The FIRST field, and the required one as of 2026-08-17 — so a filled one is
  // the ordinary case here. `title: ""` is still passed explicitly by the tests that
  // are about the WIRE contract, which still allows the key to be absent.
  title: "Morning news",
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
  test("a one-off is target, the composed message, the two fields, due and permission", () => {
    expect(buildSchedulePayload(form())).toEqual({
      target: "/tmp/work",
      // What Claude is sent: the title as the first line, the description under
      // it (composeTaskMessage).
      message: "Morning news\n\npull today's news",
      // …and the two halves still stored as themselves, because the task page
      // shows them apart.
      description: "pull today's news",
      title: "Morning news",
      due: "2026-08-17T09:00",
      permission_mode: "auto",
    });
  });

  test("the message is composed and tidy; the description is the field alone", () => {
    const body = buildSchedulePayload(form({ message: "  summarise the inbox  " }));
    // Both sides trimmed on the way into the join: the padding a textarea
    // collects is not part of the instruction, and it would sit between the
    // heading and the body where Claude reads it.
    expect(body.message).toBe("Morning news\n\nsummarise the inbox");
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
    // With no title to head it, the message is the description alone — no
    // leading blank line.
    expect(body.message).toBe("pull today's news");
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

  // THE TASK NUMBER SURVIVES AN EDIT. Editing is cancel + re-create — there is
  // no PATCH — so the entry the user was looking at is replaced by one with a
  // brand new id, and a task that has not run yet is NUMBERED on that id
  // (`pending:<entry-id>`). Nothing said the two were the same task, so the
  // server allocated the next number in the project and TASK-078 became TASK-079
  // on a change of time, with no duplicate row to explain where it went (QA,
  // 2026-08-18). `replaces` is what says it, and the server moves the number
  // across instead of minting a second one.
  test("an edit says which entry it replaces, so the task keeps its number", () => {
    expect(
      buildSchedulePayload(form({ replacesEntryId: "20260818-090000-abc123" })).replaces,
    ).toBe("20260818-090000-abc123");
    // It rides with everything else, including a repeat — a rule's template is
    // an entry like any other and is re-created the same way.
    expect(
      buildSchedulePayload(
        form({ replacesEntryId: "e1", rule: DAILY, repeat: "daily" }),
      ),
    ).toMatchObject({ replaces: "e1", rule: DAILY });
  });

  test("…and a NEW task replaces nothing, so the key stays off the wire", () => {
    // Same discipline as `title`: absent means "there isn't one", and a builder
    // that sent "" would be naming an entry id that does not exist.
    expect("replaces" in buildSchedulePayload(form())).toBe(false);
    expect("replaces" in buildSchedulePayload(form({ replacesEntryId: "" }))).toBe(false);
  });
});

// ONE prominent field is required (Akshil, 2026-08-18): the Title. It names the
// task in the list AND it is the first line of what Claude is sent
// (composeTaskMessage), so a task with a name is a task with an instruction. The
// description was required for a day, on the reasoning that it was the only text
// Claude received; composing the two ended that, and making the user type
// "Update the changelog" twice was the cost of the old rule.
describe("what Save refuses", () => {
  // Everything Save wants, so each test can take exactly one thing away.
  const gate = (over: Partial<Parameters<typeof saveEnabled>[0]> = {}) => ({
    message: "pull today's news",
    title: "Morning news",
    target: "/tmp/work",
    pathError: null,
    repeatOn: false,
    repeat: "none",
    customRule: null,
    legacyCron: "",
    pickedOk: true,
    replaced: false,
    ...over,
  });

  test("a filled form saves", () => {
    expect(saveEnabled(gate())).toBe(true);
  });

  test("an EMPTY description SAVES — the title is the instruction", () => {
    expect(saveEnabled(gate({ message: "" }))).toBe(true);
    // Whitespace-only is the same case, and it is not refused either: what goes
    // on the wire is composed from the title, which is not empty.
    expect(saveEnabled(gate({ message: "   " }))).toBe(true);
    expect(saveEnabled(gate({ message: "\n\n  \t\n" }))).toBe(true);
    // The proof that the loosening is safe: `schedule.create` refuses an empty
    // message, and the payload's is the title.
    expect(buildSchedulePayload(form({ message: "", title: "Update the changelog" })).message)
      .toBe("Update the changelog");
  });

  test("…and an EMPTY title is refused — it is the one required field now", () => {
    // The change of 2026-08-17. A blank Title used to save (the server named the
    // task from the transcript); it now refuses, because the field arrives
    // prefilled and a blank one means the user deliberately cleared it — and
    // because a task with neither a name nor a description has nothing to send.
    expect(saveEnabled(gate({ title: "" }))).toBe(false);
    // Spaces are not a name.
    expect(saveEnabled(gate({ title: "   " }))).toBe(false);
    // And no amount of description buys the title back: they are one message,
    // but the name is the half the list is read by.
    expect(saveEnabled(gate({ title: "", message: "pull today's news" }))).toBe(false);
  });

  test("the rest of the gate is unchanged by the swap", () => {
    // These were the other refusals before this pass and they still are — the
    // change moved a field, it did not loosen anything.
    expect(saveEnabled(gate({ target: "  " }))).toBe(false);
    expect(saveEnabled(gate({ pathError: "This folder or file doesn't exist" }))).toBe(false);
    expect(saveEnabled(gate({ replaced: true }))).toBe(false);
    expect(saveEnabled(gate({ pickedOk: false }))).toBe(false);
    // A "custom" repeat is only a choice once the dialog produced a rule…
    expect(saveEnabled(gate({ repeatOn: true, repeat: "custom", customRule: null }))).toBe(false);
    expect(saveEnabled(gate({ repeatOn: true, repeat: "custom", customRule: DAILY }))).toBe(true);
    // …and a legacy cron template needs its line, but not a parseable date.
    expect(saveEnabled(gate({ repeat: "cron", legacyCron: "" }))).toBe(false);
    expect(saveEnabled(gate({ repeat: "cron", legacyCron: "0 9 * * *", pickedOk: false }))).toBe(
      true,
    );
  });

  // WHY A REFUSAL HAS TO SPEAK. Save used to be `disabled` on a false
  // `saveEnabled`, and a dead button answers a press with nothing at all — no
  // message, no caret moved, the card just sitting there. The commonest way to
  // meet it is the commonest thing to forget: open the form, type a name, press
  // Save, and the description is still empty (QA, 2026-08-18). The rules did not
  // change — an empty description is a task with nothing to do, and
  // `schedule.create` refuses it on the server too — only the silence did.
  describe("…and how it says so", () => {
    test("a form that saves has nothing to say", () => {
      expect(saveBlockedReason(gate())).toBe(null);
    });

    test("an empty second field has nothing to say — it is not a refusal", () => {
      // Nothing is missing: the additional instructions are optional, and the
      // sentence about a task with no instructions has moved to the field that
      // now asks for them (below).
      expect(saveBlockedReason(gate({ message: "" }))).toBe(null);
      expect(saveBlockedReason(gate({ message: "\n\n  \t" }))).toBe(null);
    });

    test("every refusal names a field, and the sentence is a thing to DO", () => {
      // The one missing-prose refusal there is, and it asks for the TASK: the
      // field says "What should Claude do?", so a banner saying "give the task a
      // name" would send the user looking for a label to invent instead of the
      // instruction that is actually absent. It still focuses the primary field,
      // which is where that answer goes.
      const noTitle = saveBlockedReason(gate({ title: "   " }));
      expect(noTitle?.field).toBe("title");
      expect(noTitle?.text).toContain("Say what Claude should do");
      expect(noTitle?.text).not.toContain("name");

      expect(saveBlockedReason(gate({ target: "" }))?.field).toBe("target");
      // A path that failed its existence check already wrote a sentence for a
      // human; the banner repeats THAT rather than inventing a second one.
      expect(saveBlockedReason(gate({ pathError: "This folder or file doesn't exist" })))
        .toEqual({ text: "This folder or file doesn't exist", field: "target" });
    });

    test("the reasons that are not a field still say something, and focus nothing", () => {
      // Nothing to put a caret in: the repeat lives behind a dialog and the
      // date-time behind two popovers, so the sentence is the whole answer.
      for (const over of [
        { repeatOn: true, repeat: "custom", customRule: null },
        { pickedOk: false },
        { repeat: "cron", legacyCron: "" },
        { replaced: true },
      ]) {
        const blocked = saveBlockedReason(gate(over));
        expect(blocked?.field).toBe(null);
        expect((blocked?.text ?? "").length).toBeGreaterThan(0);
      }
    });

    test("it agrees with saveEnabled on every case saveEnabled decides", () => {
      // The two are ONE rule set with two readers, so they must not drift: a
      // form saveEnabled refuses has a reason, and one it allows has none. This
      // is what keeps a new refusal from being added silently to only one of
      // them.
      const cases: Partial<Parameters<typeof saveEnabled>[0]>[] = [
        {}, { message: "" }, { message: "  " }, { title: "" }, { title: "\t" },
        { target: "" }, { pathError: "no such folder" }, { replaced: true },
        { pickedOk: false }, { repeatOn: true, repeat: "custom", customRule: null },
        { repeatOn: true, repeat: "custom", customRule: DAILY },
        { repeat: "cron", legacyCron: "" },
        { repeat: "cron", legacyCron: "0 9 * * *", pickedOk: false },
        { title: "", message: "" },
      ];
      for (const over of cases) {
        expect(saveBlockedReason(gate(over)) === null).toBe(saveEnabled(gate(over)));
      }
    });

    test("the topmost problem is the one reported, in the card's reading order", () => {
      // One reason at a time. A form with everything wrong reads back the FIRST
      // field on the card, not a list — fixing the top one often fixes the rest,
      // and a scolding is not a hint.
      expect(saveBlockedReason(gate({ title: "", message: "", target: "" }))?.field).toBe("title");
      expect(saveBlockedReason(gate({ message: "", target: "" }))?.field).toBe("target");
      // Except `replaced`, which outranks everything: the task IS saved, so
      // naming a missing field would be telling the user to fix a form whose
      // work is already done.
      expect(saveBlockedReason(gate({ replaced: true, title: "" }))?.field).toBe(null);
    });
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

  test("a chat draft fills a NEW task's description with its BODY, and never outranks the entry", () => {
    // The draft's first line has gone to the title (see the split below), so
    // what is left for this field is the rest of it.
    expect(initialAskOf(null, "Port the reader\nstart with the parquet path"))
      .toBe("start with the parquet path");
    // A one-line draft is entirely a name: nothing is left over.
    expect(initialAskOf(null, "draft from the composer")).toBe("");
    expect(initialAskOf(entry({}), "draft from the composer")).toBe("pull today's news");
    expect(initialAskOf(undefined)).toBe("");
  });

  test("the ask and the title both survive the save → edit → save round trip", () => {
    const saved = buildSchedulePayload(
      form({ message: "pull today's news", title: "Morning news" }),
    );
    // What Claude is sent is BOTH fields, the title as the first line…
    expect(saved.message).toBe("Morning news\n\npull today's news");
    // …and the server still stores the description as itself.
    expect(saved.description).toBe("pull today's news");
    // What the server would have stored, read back into the form's two fields.
    const stored = entry({
      message: saved.message,
      description: saved.description,
      title: saved.title,
    });
    // The description field opens on the description, NOT on the composed
    // message — opening on that would put the title back inside the body and the
    // next Save would compose the heading twice.
    expect(initialAskOf(stored)).toBe("pull today's news");
    expect(stored.title).toBe("Morning news");

    // And re-saving that edit sends the same three values back — an edit is
    // cancel + re-create, so anything the form fails to re-state is LOST, and
    // nothing may be gained either: the message is composed once, not once per
    // round trip.
    const again = buildSchedulePayload(
      form({ message: initialAskOf(stored), title: stored.title ?? "" }),
    );
    expect(again.message).toBe(saved.message);
    expect(again.description).toBe("pull today's news");
    expect(again.title).toBe("Morning news");
  });

  test("a task saved BEFORE the two were composed still opens on its message", () => {
    // No description stored at all (every task from before the field existed):
    // the message is the only prose there is, and it is not a composed one, so it
    // fills the field whole.
    const old = entry({ message: "pull today's news", description: undefined });
    expect(initialAskOf(old)).toBe("pull today's news");
    // And one whose message DOES open with its title has the heading peeled off,
    // so an edit does not stack a second copy of the name on top of the first.
    const composed = entry({
      title: "Morning news",
      message: "Morning news\n\npull today's news",
      description: undefined,
    });
    expect(initialAskOf(composed)).toBe("pull today's news");
    // Exact, not fuzzy: prose that merely starts with the same words is prose.
    expect(initialAskOf(entry({ title: "Morning", message: "Morning news please", description: undefined })))
      .toBe("Morning news please");
  });

  test("a description-only task is sent as its description, with no blank first line", () => {
    // The other half of the compose rule: either side alone is sent alone.
    expect(buildSchedulePayload(form({ title: "", message: "pull today's news" })).message)
      .toBe("pull today's news");
    expect(buildSchedulePayload(form({ title: "Morning news", message: "" })).message)
      .toBe("Morning news");
    expect(buildSchedulePayload(form({ title: "Morning news", message: "" })).description)
      .toBeUndefined();
  });

  test("an UNTITLED task opens its title BLANK, not on a copy of its message", () => {
    // Every task stored before the field existed, and every one saved while it
    // was optional, has no title. Editing one used to derive the first line of
    // the stored message — which is the duplication bug arriving by the back
    // door, since that message is also what fills the description below it.
    // Blank is the synchronous answer; the session lookup fills it if the thread
    // has a name, and Save asks if it does not.
    const stored = entry({ message: "pull today's news" });
    expect(stored.title).toBeUndefined();
    expect(initialTitleOf(stored)).toBe("");
    // The description is untouched by any of it — the ask still opens filled.
    expect(initialAskOf(stored)).toBe("pull today's news");
  });
});

// TITLE AND DESCRIPTION ARE ONE MESSAGE (Akshil, 2026-08-18). The card collects
// a name and a body; Claude is sent both, the name as the first line. That is
// what makes the description optional — the title is already an instruction —
// and it is what a chat handoff is split across.
describe("the first message the task sends", () => {
  test("the title is its first line and the description its body", () => {
    expect(composeTaskMessage("Morning news", "pull today's news")).toBe(
      "Morning news\n\npull today's news",
    );
    // A BLANK line, not a bare newline: it is the plainest heading markdown has,
    // and a single newline would run the name into the body as one paragraph.
    expect(composeTaskMessage("Morning news", "pull today's news")).toContain("\n\n");
  });

  test("either side alone is sent alone, with no stray blank line", () => {
    expect(composeTaskMessage("Update the changelog", "")).toBe("Update the changelog");
    expect(composeTaskMessage("Update the changelog", "  \n ")).toBe("Update the changelog");
    expect(composeTaskMessage("", "pull today's news")).toBe("pull today's news");
    expect(composeTaskMessage("", "")).toBe("");
    // Nothing composed ever opens or closes on whitespace — a message that did
    // would reach Claude with an empty heading above it.
    for (const composed of [
      composeTaskMessage(" Morning news ", " pull today's news "),
      composeTaskMessage(" Morning news ", ""),
      composeTaskMessage("", " pull today's news "),
    ]) {
      expect(composed).toBe(composed.trim());
    }
  });

  test("the heading comes back off when an Edit has only the message to read", () => {
    expect(withoutTitleHeading("Morning news\n\npull today's news", "Morning news")).toBe(
      "pull today's news",
    );
    // Only the exact join is peeled. A message that merely begins with the same
    // words is prose, and prose is left alone.
    expect(withoutTitleHeading("Morning news please", "Morning")).toBe("Morning news please");
    expect(withoutTitleHeading("Morning news\npull today's news", "Morning news")).toBe(
      "Morning news\npull today's news",
    );
    expect(withoutTitleHeading("pull today's news", "")).toBe("pull today's news");
  });

  // THE TITLE-ONLY TASK, which is the ordinary case now that the second field is
  // optional: the composer appends nothing, so there is no `\n\n` prefix for the
  // inverse to spot. It used to hand the whole message back as the additional
  // instructions, and the next Save composed `title\n\ntitle` — one more copy of
  // the name per edit, for ever (Bugbot, PR #595).
  test("a task that is ALL title inverts to no additional instructions", () => {
    expect(withoutTitleHeading("Update the changelog", "Update the changelog")).toBe("");
    // Whitespace the wire may have picked up does not make it look like prose.
    expect(withoutTitleHeading("  Update the changelog\n", "Update the changelog")).toBe("");
    expect(withoutTitleHeading("Update the changelog", "  Update the changelog  ")).toBe("");
  });

  test("compose → peel is a true inverse, in all three shapes", () => {
    for (const [title, additional] of [
      ["Update the changelog", ""],
      ["Morning news", "pull today's news"],
      ["", "pull today's news"],
    ]) {
      const composed = composeTaskMessage(title, additional);
      expect(withoutTitleHeading(composed, title)).toBe(additional);
    }
  });

  test("editing a title-only task leaves the second field empty, edit after edit", () => {
    // End to end, through the values the ?edit= flow actually passes: the entry
    // the page found goes to the modal whole, and these two functions are the
    // only readers of its prose.
    const saved = buildSchedulePayload(form({ title: "Update the changelog", message: "" }));
    expect(saved.message).toBe("Update the changelog");
    expect(saved.description).toBeUndefined();

    // The server has no `description` to store, so the Edit falls back to the
    // message — which is the case the bug lived in.
    let stored = entry({
      title: saved.title,
      message: saved.message,
      description: undefined,
    });
    // Three round trips, because the bug COMPOUNDED: one copy of the name per
    // save, and nothing on the card said where it came from.
    for (let i = 0; i < 3; i += 1) {
      expect(initialAskOf(stored)).toBe("");
      expect(initialTitleOf(stored)).toBe("Update the changelog");
      const again = buildSchedulePayload(
        form({ title: initialTitleOf(stored), message: initialAskOf(stored) }),
      );
      expect(again.message).toBe("Update the changelog");
      expect(again.description).toBeUndefined();
      stored = entry({
        title: again.title,
        message: again.message,
        description: undefined,
      });
    }
  });

  // The chat composer's Schedule button hands over one block of prose
  // (`?new=1&message=…`) and the card has two fields to put it in. It is
  // PARTITIONED, not copied: what the title takes, the description loses.
  describe("splitting a chat draft across the two fields", () => {
    test("first line names the task, the rest describes it", () => {
      expect(splitDraft("Port the parquet reader\nstart with the path handling")).toEqual({
        title: "Port the parquet reader",
        description: "start with the path handling",
      });
      // And the two put back together are the draft again — the round trip that
      // proves nothing was said twice and nothing dropped.
      const s = splitDraft("Port the parquet reader\n\nstart with the path handling");
      expect(composeTaskMessage(s.title, s.description)).toBe(
        "Port the parquet reader\n\nstart with the path handling",
      );
    });

    test("a one-line draft is all name and no body", () => {
      expect(splitDraft("Update the changelog")).toEqual({
        title: "Update the changelog",
        description: "",
      });
      // Which is a saveable task, and one whose message is that single line.
      expect(saveEnabled({
        message: "", title: "Update the changelog", target: "/tmp/work", pathError: null,
        repeatOn: false, repeat: "none", customRule: null, legacyCron: "",
        pickedOk: true, replaced: false,
      })).toBe(true);
    });

    test("nothing to split is two empty fields, not a title of spaces", () => {
      expect(splitDraft(null)).toEqual({ title: "", description: "" });
      expect(splitDraft(undefined)).toEqual({ title: "", description: "" });
      expect(splitDraft("   \n\n  ")).toEqual({ title: "", description: "" });
    });

    test("a long first line is kept WHOLE — the line break is the only cut", () => {
      // No clamp (Akshil, 2026-08-18). The field asks what Claude should do, and
      // two thirds of a sentence is not an answer to that; the user can shorten
      // their own line, and the one before this rule could not lengthen a clamped
      // one without retyping it.
      const head = "port the parquet reader and work out why the path handling "
        + "drops the drive letter on windows before anything else happens";
      expect(head.length).toBeGreaterThan(80);
      expect(splitDraft(head + "\nstart with the tests")).toEqual({
        title: head,
        description: "start with the tests",
      });
      // And it is still a PARTITION: the long line is in one field, not in both.
      expect(splitDraft(head + "\nstart with the tests").description).not.toContain(head);
    });

    test("the draft's name fills the field, and outranks the session lookup", () => {
      // The lookup would land a beat later with the name of the CONVERSATION the
      // draft was written in, replacing a name the user just typed.
      const open = initialTitleStateOf(null, "sess-1", "Port the parquet reader");
      expect(open.title).toBe("Port the parquet reader");
      expect(open.lookupSession).toBe("");
      // With no draft, the lookup runs exactly as it did.
      expect(initialTitleStateOf(null, "sess-1").lookupSession).toBe("sess-1");
      expect(initialTitleStateOf(null, "sess-1", "   ").lookupSession).toBe("sess-1");
    });

    test("…but a stored title still outranks the draft — an Edit never loses its name", () => {
      const open = initialTitleStateOf(
        entry({ title: "Morning news", session_id: "sess-1" }),
        "sess-1",
        "Port the parquet reader",
      );
      expect(open.title).toBe("Morning news");
      expect(open.lookupSession).toBe("");
    });
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
    // "Fresh task each run" means a fresh session per occurrence; an id on that
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

// Title is required (see "what Save refuses"), and this block is what makes that
// fair — and what the 2026-08-17 review rewrote. The title now names the
// SESSION, in this order:
//
//   1. the task's stored title, if a user set one;
//   2. the session's own resolved name — Claude Code's `ai-title`;
//   3. the session's FIRST user message, shortened to a name;
//   4. blank, and Save asks.
//
// Never the message being scheduled. That was the bug: `firstLine(ask)` filled
// Title from the composer's draft, so scheduling a long message from a chat
// duplicated it — once as the title, once as the description. "The description
// is what we type in the chat box" (Akshil, 2026-08-17).
describe("naming the task", () => {
  const task = (over: Record<string, unknown> = {}) => ({
    session_id: "sess-1",
    title: "Porting the parquet reader",
    title_source: "ai",
    ...over,
  });

  // The message the user is scheduling: long, one line, and the thing that must
  // never become a name.
  const LONG =
    "Go through the whole scheduling stack and work out why a recurring task "
    + "that has already learned a session id stops resuming that thread after "
    + "an edit, then fix it and add a regression test for it";

  test("the placeholder asks for the TASK, not for a label", () => {
    // It said "Title" — what the value is used for, not what the user is being
    // asked to write — and people answered it with a label ("News") and put the
    // real instruction in the field underneath. The question is the same one the
    // chat composer asks, because the answer is the same text.
    expect(TITLE_PLACEHOLDER).toBe("What should Claude do?");
    expect(TITLE_PLACEHOLDER).not.toContain("optional");
  });

  test("…and the second field is the OVERFLOW of that question, and says it is optional", () => {
    // Never the same question twice: the field above asks what the task is, so
    // this one asks only for what that answer left out.
    expect(ASK_PLACEHOLDER).toBe("Additional instructions (optional)");
    expect(ASK_PLACEHOLDER).toContain("optional");
    expect(ASK_PLACEHOLDER).not.toContain("What should Claude do");
  });

  test("step 2: the session's own `ai-title` is what the form prefills", () => {
    expect(sessionTitleOf([task()], "sess-1")).toBe("Porting the parquet reader");
  });

  test("step 1 via the API: a title the user gave this thread is a name too", () => {
    expect(sessionTitleOf([task({ title_source: "user" })], "sess-1")).toBe(
      "Porting the parquet reader",
    );
  });

  test("step 3: a message-sourced title IS the session's first message, so it counts", () => {
    // The reversal of 2026-08-17. This branch used to be dropped on the grounds
    // that it echoed a derivation the form already had locally. That derivation
    // is gone — it was the bug — so this is now the ONLY route to "the first
    // message that we had", and refusing it would send a session with no
    // `ai-title` yet straight to blank.
    expect(
      sessionTitleOf([task({ title: "port the parquet reader", title_source: "message" })], "sess-1"),
    ).toBe("port the parquet reader");
  });

  test("…but not a title the server read off a SCHEDULED ENTRY", () => {
    // The server's second source, and the one this refuses. With no readable
    // transcript, `_title` names the row from the earliest message scheduled at
    // the session — which on a task made in this form is the ask itself, so
    // taking it would be the duplication bug arriving by way of the server.
    // `title_source: "entry"` is the server saying so, which is why nothing here
    // has to compare strings.
    expect(sessionTitleOf([task({ title: LONG.slice(0, 200), title_source: "entry" })], "sess-1"))
      .toBe("");
    expect(sessionTitleOf([task({ title: "summarise the inbox", title_source: "entry" })], "sess-1"))
      .toBe("");
  });

  test("a first prompt the new ask CONTINUES keeps its name", () => {
    // THE 2026-08-17 review finding. This was refused while the client guessed
    // at provenance: it dropped a `message` title whenever the composed ask's
    // first line began with it, and "pull today's news and file it" begins with
    // "pull today's news" — a real session first prompt the draft merely carries
    // on from. Title came out blank and Save stayed disabled until the user
    // retyped a name the app already had.
    // The draft is not an input any more — the ask this call used to take is
    // gone from the signature, so no composer text can take a session's own name
    // away and the type checker is what enforces it.
    const first = task({ title: "pull today's news", title_source: "message" });
    expect(sessionTitleOf([first], "sess-1")).toBe("pull today's news");
  });

  test("no session, no match and a blank title all resolve to nothing", () => {
    expect(sessionTitleOf([task()], "")).toBe("");
    expect(sessionTitleOf([task()], "sess-2")).toBe("");
    expect(sessionTitleOf([task({ title: "   " })], "sess-1")).toBe("");
    expect(sessionTitleOf([], "sess-1")).toBe("");
  });

  test("REPEAT no longer takes the name away — it only takes the session away", () => {
    // The old preview was suppressed under a ticked Repeat, because a repeat
    // refuses the chat's session and the preview would have been a lie. A VALUE
    // in a required field cannot be withdrawn like that: the user still has to
    // name the task, and the conversation it came from is still the best name
    // anyone has. The payload rule it used to mirror is unchanged.
    expect(
      buildSchedulePayload(form({ sessionId: "sess-1", rule: DAILY, repeat: "daily" })).session_id,
    ).toBeUndefined();
    expect(saveEnabled({
      message: "pull today's news",
      title: "Porting the parquet reader",
      target: "/tmp/work",
      pathError: null,
      repeatOn: true,
      repeat: "daily",
      customRule: null,
      legacyCron: "",
      pickedOk: true,
      replaced: false,
    })).toBe(true);
  });

  // A GUARD, not the fix. The fix is server-side: four readers of a transcript's
  // first user message each had their own idea of what counted as machinery, so
  // /api/tasks served rows titled `<live-app-state>` and
  // `<command-message>making-a-release</command-message>` (44 of them in one real
  // store). Those are gone at the source. This refuses them anyway, because of
  // what happens to a bad prefill HERE and nowhere else: a `user`-set title
  // outranks every other source forever, so one leaked string the user does not
  // notice before pressing Save becomes that task's permanent name. One already
  // is, in one real store — which is the proof that the cost is asymmetric and
  // worth a second check the server has already made.
  test("a leaked machinery string is never prefilled into the Title field", () => {
    for (const leaked of [
      "<live-app-state>",
      "<command-message>making-a-release</command-message>",
      "<command-name>/clear</command-name>",
      "<pane-shot>",
      // The annotation block opens with a sentence, not a tag, so a "<" test
      // alone would have let this one straight through.
      "The user annotated 1 element in the left preview of this file. anchorId =",
    ]) {
      // Every source, including the ones that are normally taken verbatim: a
      // `user` title is exactly how the one bad row in the real store got there,
      // so re-prefilling it on an Edit would keep the mistake alive.
      for (const source of ["user", "ai", "message", "entry"]) {
        expect(sessionTitleOf([task({ title: leaked, title_source: source })], "sess-1")).toBe("");
      }
      expect(initialTitleOf(entry({ title: leaked }))).toBe("");
    }
  });

  // THE REVIEW FINDING on the guard above (2026-08-18): refusing the prefill is
  // only half a rescue. The field opened on `initialTitleOf`, which blanks a
  // leaked title, while the /api/tasks lookup gated on the RAW stored field — so
  // on exactly the rows the guard exists to rescue the two halves disagreed. A
  // non-empty leaked string short-circuited the lookup, the field arrived blank
  // and STAYED blank, and Title is required, so Save was refused on a task the
  // user cannot easily rename. One answer now serves both halves.
  test("a leaked stored title still lets the session's own name through", () => {
    const stored = entry({ title: "<live-app-state>", session_id: "sess-1" });
    const open = initialTitleStateOf(stored, stored.session_id);
    // Nothing usable is stored, so the field opens blank…
    expect(open.title).toBe("");
    // …and the session lookup must RUN — this is the half that used to see the
    // leaked string and return early.
    expect(open.lookupSession).toBe("sess-1");
    // …landing the session's own resolved name, exactly as if no title had ever
    // been stored, because as far as this form is concerned none usable was.
    const resolved = sessionTitleOf([task()], open.lookupSession);
    expect(resolved).toBe("Porting the parquet reader");
    // Which is a name, so the requirement is met without the user retyping one.
    expect(saveEnabled({
      message: "pull today's news",
      title: resolved,
      target: "/tmp/work",
      pathError: null,
      repeatOn: false,
      repeat: "none",
      customRule: null,
      legacyCron: "",
      pickedOk: true,
      replaced: false,
    })).toBe(true);
  });

  test("a real stored title asks for no lookup at all", () => {
    const open = initialTitleStateOf(entry({ title: "Morning news", session_id: "sess-1" }), "sess-1");
    expect(open.title).toBe("Morning news");
    // "" means "do not fetch": step 1 is the top of the precedence and an async
    // overwrite of a stored name would be data loss.
    expect(open.lookupSession).toBe("");
    // The other refusal the same "" carries: nothing to ask about.
    expect(initialTitleStateOf(null, "").lookupSession).toBe("");
    expect(initialTitleStateOf(entry({ title: "   " }), "").lookupSession).toBe("");
  });

  test("…and markup the user typed as a name is still their name to keep", () => {
    // The guard is deliberately narrow. It refuses a prefill that OPENS with a
    // tag or with the annotation sentence; it does not go hunting for angle
    // brackets, because "<div> renders twice" is a perfectly good name for a
    // thread about that bug and refusing it would be the same class of mistake
    // as the drop that started all this.
    expect(sessionTitleOf([task({ title: "fix why <div> renders twice" })], "sess-1")).toBe(
      "fix why <div> renders twice",
    );
    expect(initialTitleOf(entry({ title: "annotated elements are misaligned" }))).toBe(
      "annotated elements are misaligned",
    );
  });

  test("a slash-command title is a name the server read, and it survives", () => {
    // The server's new fifth source (`title_source: "command"`): a session whose
    // only user records are `/making-a-release` is named that, because it is true
    // and useful. Taken verbatim like the other names — it is already one — and
    // NOT caught by the guard above, which tests the opening tag, not the slash.
    expect(
      sessionTitleOf([task({ title: "/making-a-release", title_source: "command" })], "sess-1"),
    ).toBe("/making-a-release");
  });

  test("step 1: a stored title outranks everything, and an edit never loses it", () => {
    expect(initialTitleOf(entry({ title: "Morning news" }))).toBe("Morning news");
    // Even against a message that would once have derived something else…
    expect(initialTitleOf(entry({ title: "Morning news", message: "pull the news" }))).toBe(
      "Morning news",
    );
    // …and a stored title of spaces is not one, so it falls through to blank and
    // the session lookup gets its turn.
    expect(initialTitleOf(entry({ title: "   " }))).toBe("");
  });

  // THE regression, and what is left of it. The bug was DUPLICATION: the message
  // being scheduled filled the description AND was copied into the title, so a
  // long message arrived twice and the task was named after its own body. A chat
  // draft's first line does fill the primary field now (splitDraft) — and that is
  // the opposite operation, a partition: what the first field takes, the second
  // one loses. What must still never happen is a title DERIVED from a message
  // nobody put there, which is every path below.
  test("a long scheduled message is never COPIED into the title", () => {
    expect(LONG.length).toBeGreaterThan(150);

    // A one-line draft is one answer to one question, and it goes in the field
    // that asks it — whole, because the line break is the only cut.
    const split = splitDraft(LONG);
    expect(split.title).toBe(LONG);
    // The duplication is what is refused: it is in one field, not in both.
    expect(split.description).toBe("");
    expect(initialAskOf(null, LONG)).toBe("");
    // Nothing synchronous puts a message in a title on any other path.
    expect(initialTitleOf(null)).toBe("");

    // Editing the task that draft created: the message is stored, and it is
    // still not a name.
    const stored = entry({ message: LONG });
    expect(initialTitleOf(stored)).toBe("");
    expect(initialAskOf(stored)).toBe(LONG);

    // Nor by way of the session lookup, when the server hands the entry's own
    // message back as the row's name (`title_source: "entry"` — the only branch
    // that can be the message being scheduled).
    expect(
      sessionTitleOf([task({ title: LONG.slice(0, 200), title_source: "entry" })], "sess-1"),
    ).toBe("");

    // And nothing anywhere in the resolved chain is that long.
    for (const resolved of [
      initialTitleOf(stored),
      sessionTitleOf([task()], "sess-1"),
      sessionTitleOf([task({ title: LONG.slice(0, 200), title_source: "entry" })], "sess-1"),
    ]) {
      expect(resolved.length).toBeLessThanOrEqual(TITLE_MAX);
      expect(LONG.startsWith(resolved) && resolved !== "").toBe(false);
    }
  });

  // Step 3 is a message being turned into a NAME, so it is clamped. Steps 1 and 2
  // are names already and are taken verbatim — shortening them would edit either
  // the user's words or Claude's.
  describe("shortening a first message into a name", () => {
    test("a short one is left exactly alone", () => {
      expect(shortTitle("port the parquet reader")).toBe("port the parquet reader");
      expect(shortTitle("x".repeat(TITLE_MAX))).toBe("x".repeat(TITLE_MAX));
    });

    test("a long one is cut on a word boundary, with no ellipsis", () => {
      const line = "one two three four five six seven eight nine ten eleven twelve";
      expect(line.length).toBeGreaterThan(TITLE_MAX);
      expect(shortTitle(line)).toBe("one two three four five six seven eight nine ten eleven");
      expect(shortTitle(line)).not.toContain("…");
      expect(shortTitle(line)).not.toContain("...");
      // Cut on a boundary means the next character in the original is the space
      // the cut replaced — never the middle of "twelve".
      expect(line[shortTitle(line).length]).toBe(" ");
    });

    test("the word straddling the limit is kept whole when the limit IS the space", () => {
      const line = "x".repeat(TITLE_MAX) + " tail";
      expect(shortTitle(line)).toBe("x".repeat(TITLE_MAX));
    });

    test("one unbroken word has no boundary, so it is cut hard", () => {
      // The only mid-word cut, and unavoidable: there is nowhere else to cut.
      expect(shortTitle("a".repeat(200))).toBe("a".repeat(TITLE_MAX));
    });

    test("it takes one line, and never trails whitespace", () => {
      expect(shortTitle("summarise the inbox\nthen file it")).toBe("summarise the inbox");
      expect(shortTitle("   ")).toBe("");
      expect(shortTitle("")).toBe("");
      expect(shortTitle("word ".repeat(40))).toBe(shortTitle("word ".repeat(40)).trimEnd());
    });
  });

  test("firstLine reduces prose to something an <input> can hold", () => {
    expect(firstLine("summarise the inbox\nthen file it\nand report")).toBe(
      "summarise the inbox",
    );
    expect(firstLine("  padded  \n more ")).toBe("padded");
    expect(firstLine("   ")).toBe("");
  });

  test("with nothing to go on it IS blank — and Save says so", () => {
    // The New task button with an empty form: there is no honest name to
    // derive, so the field opens empty and the requirement bites. That is the
    // one path where the user must type a title, and it is the path where they
    // are typing everything else anyway.
    expect(initialTitleOf(null)).toBe("");
    expect(initialTitleOf(undefined)).toBe("");
    expect(saveEnabled({
      message: "pull today's news",
      title: initialTitleOf(null),
      target: "/tmp/work",
      pathError: null,
      repeatOn: false,
      repeat: "none",
      customRule: null,
      legacyCron: "",
      pickedOk: true,
      replaced: false,
    })).toBe(false);
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
    // THE BOUNDARY IS THE MINUTE, not the millisecond (2026-08-18): the picker has
    // minute precision, and the current minute is the only way it can say "now" —
    // which is the value the card now opens on, so this minute is silent...
    expect(note(NOW, false, null)).toBeNull();
    expect(
      note(at("2026-08-19T10:00"), false, null, at("2026-08-19T10:00:45")),
    ).toBeNull();
    // ...and the minute before it is not.
    expect(note(at("2026-08-19T09:59"), false, null)).toBe(PAST_NOTE_ONE_OFF);
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

// ---- The default target ---------------------------------------------------

describe("defaultTargetOf", () => {
  test("uses the server's RESOLVED workspace, not home + a guessed suffix", () => {
    // FUSED_RENDER_DIR is a supported override the workspace migration
    // deliberately leaves alone; guessing `${home}/Fused` handed those users a
    // folder that may not exist, and the server's 400.
    expect(defaultTargetOf({ home: "/Users/x", fused_dir: "/data/work" })).toBe(
      "/data/work",
    );
  });

  test("normalizes a Windows workspace path", () => {
    expect(
      defaultTargetOf({ home: "C:\\Users\\v", fused_dir: "C:\\Users\\v\\Fused" }),
    ).toBe("C:/Users/v/Fused");
  });
});

// ---- the when-row opens on now -------------------------------------------------
describe("the when-row's default", () => {
  test("is the current time, not an hour from it", () => {
    const src = readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");
    // A task typed into this card is overwhelmingly one to RUN (Akshil,
    // 2026-08-18). Opening an hour out made the commonest case a two-step: wind
    // the time back, then save.
    expect(src).toContain("initialTime ?? new Date()");
    expect(src).not.toContain("3600_000");
    // Anything the CALLER hands in still wins — a deep link that names a time,
    // and an Edit's stored `due` ahead of both.
    const init = src.slice(src.indexOf("const [when, setWhen] = useState("));
    const body = init.slice(0, init.indexOf(");"));
    expect(body.indexOf("editing?.due")).toBeLessThan(body.indexOf("initialTime"));
  });
});

// ---- "recent" means one thing -------------------------------------------------
// The app had two recents. The home page shows the folders this machine has
// Claude sessions in, newest session first (the Claude Sessions strip); this
// form showed a localStorage array only it ever wrote, so
// a person who had spent the morning in a repo opened New task and was offered
// folders the form happened to remember. One noun per concept
// (design-principles §1), and recents are exactly the "recognition over recall"
// affordance §4 asks for — so the home page's list leads this one.
describe("the folder recents come from the app's own recents", () => {
  test("the leading tier is the Claude sessions source, top five", () => {
    const src = readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");
    // Both surfaces use the same server-side ordering. Home takes its bounded
    // endpoint; the form retains the exhaustive API and slices its five rows.
    expect(src).toContain("getClaudeSessionFolders()");
    expect(readFileSync(join(import.meta.dir, "Home.tsx"), "utf8"))
      .toContain("getHomeClaudeSessionFolders(Math.min(limit, MAX_ROW))");
    // Five, and the server already answers newest-session-first, so the slice
    // is the whole of the ordering — the folders reach the list in the order
    // they arrived in, with no client-side re-sort to disagree with the strip.
    expect(src).toContain("const SESSION_FOLDERS_SHOWN = 5;");
    expect(src).toContain("r.folders.slice(0, SESSION_FOLDERS_SHOWN)");
    expect(src).not.toContain("sessionFolders.sort");
    // Enough of them to be worth opening: the dropdown shows five, and the
    // leading tier can now fill it on its own.
    expect(src).toContain("const RECENTS_SHOWN = 5;");
    // NOT /api/recents. That is the explorer's recently-OPENED FILES — the wrong
    // shape (files, not places to work) and, on a machine with dozens of
    // sessions, three entries long.
    expect(src).not.toContain("@apps/explorer/lib/recents");
    expect(src).not.toContain("hydrateRecents");
  });

  test("the form's own memory follows it, and nothing is offered twice", () => {
    const src = readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");
    // Order: the app's recents, then folders picked through Browse or saved on a
    // task, then existing tasks' targets as padding. The middle tier is KEPT — a
    // folder deliberately chosen here may hold no Claude session at all, so the
    // shared source would never learn it.
    const list = src.slice(src.indexOf("const readRecentList = useCallback("));
    const body = list.slice(0, list.indexOf("}, [recentTargets, sessionFolders]);"));
    expect(body.indexOf("sessionFolders")).toBeLessThan(body.indexOf("readRecents()"));
    expect(body.indexOf("readRecents()")).toBeLessThan(body.indexOf("recentTargets"));
    // Deduped, so a folder two tiers know is offered once.
    expect(body).toContain("seen.has(p)");
    // The fetch is fire-and-forget: a suggestion list that fails to load costs
    // suggestions, never the form.
    expect(src).toContain("getClaudeSessionFolders().then(");
    expect(src).toContain("      () => {},");
  });
});

// ---- One new folder, and only one -------------------------------------------
// The path field accepts a folder that does not exist YET (Akshil, 2026-08-20).
// `targetVerdict` is the whole decision: it is handed the path and whatever the
// PARENT's listing came back with, and answers with one of three things.
describe("the path field's verdict on a folder that isn't there yet", () => {
  test("a name the parent already holds is a plain target, not a new folder", () => {
    // How a FILE target reaches here: listing the path itself failed (it is not
    // a directory), so the parent was listed and the basename found in it.
    expect(targetVerdict("/Users/a/fused/notes.md", ["notes.md", "src"]))
      .toEqual({ kind: "ok" });
  });

  test("a missing last segment under an existing parent is a new folder", () => {
    expect(targetVerdict("/Users/a/fused/ABC1", ["src", "notes.md"]))
      .toEqual({ kind: "new-folder", name: "ABC1", parent: "/Users/a/fused" });
  });

  test("a trailing slash names the same folder", () => {
    // Typing a path usually ends with the separator; it must not turn the name
    // into an empty segment and read as junk.
    expect(targetVerdict("/Users/a/fused/ABC1/", ["src"]))
      .toEqual({ kind: "new-folder", name: "ABC1", parent: "/Users/a/fused" });
  });

  test("a backslash path is normalised before it is split", () => {
    expect(targetVerdict("C:\\Users\\a\\ABC1", ["Desktop"]))
      .toEqual({ kind: "new-folder", name: "ABC1", parent: "C:/Users/a" });
  });

  test("two missing levels is refused, and says which one is missing", () => {
    // null = the PARENT could not be listed either, so this is not "name me a
    // folder", it is "build me a tree" — the ask a typo makes by accident.
    expect(targetVerdict("/Users/a/new1/new2", null)).toEqual({
      kind: "bad",
      text: twoLevelsMissing("/Users/a/new1"),
    });
    expect(twoLevelsMissing("/Users/a/new1")).toContain("Only one new folder");
    expect(twoLevelsMissing("/Users/a/new1")).toContain("/Users/a/new1");
  });

  test("a path with no last segment to create is the old refusal", () => {
    // "." and ".." name somewhere that exists by definition, so arriving here
    // with one means the string was junk rather than a new name.
    expect(targetVerdict("/Users/a/fused/..", ["src"]))
      .toEqual({ kind: "bad", text: PATH_MISSING });
    expect(targetVerdict("/Users/a/fused/.", ["src"]))
      .toEqual({ kind: "bad", text: PATH_MISSING });
  });

  test("splitTargetPath keeps a drive root's slash", () => {
    // Bare "C:" reads as cwd-relative everywhere else in the shell.
    expect(splitTargetPath("C:/ABC1")).toEqual({ parent: "C:/", base: "ABC1" });
    expect(splitTargetPath("/ABC1")).toEqual({ parent: "/", base: "ABC1" });
    expect(splitTargetPath("/Users/a/fused/ABC1"))
      .toEqual({ parent: "/Users/a/fused", base: "ABC1" });
  });

  test("a new folder does not block Save — only a bad path does", () => {
    // The verdict feeds two separate pieces of state, and only `bad` becomes
    // `pathError`. The new-folder row is not a refusal.
    const src = readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");
    expect(src).toContain('setPathError(v.kind === "bad" ? v.text : null);');
    expect(src).toContain('setNewFolder(v.kind === "new-folder" ? v.name : null);');
  });

  test("the picker's New folder only NAMES one — nothing is written on cancel", () => {
    const src = readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");
    // No /api/fs/mkdir from this modal: the folder is created by the save, so
    // backing out of the card leaves nothing behind on disk.
    expect(src).not.toContain("mkdir");
    expect(src).toContain("+ New folder");
    // Escape backs out of the naming row before it backs out of the panel.
    expect(src).toContain("if (namingOpen.current) {");
  });
});

// ---- The verdict is shown in the dropdown, not under the field ---------------
// "this UI should be in dropdown" (Akshil, 2026-08-20). The new-folder answer
// used to be a row that appeared BELOW the path input and pushed the rest of the
// card down as you typed; it now renders as the first row of the path field's
// own dropdown, in the row shape of the folders listed under it.
describe("where the new-folder answer is shown", () => {
  const src = () => readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");
  const css = () =>
    readFileSync(join(import.meta.dir, "../styles/schedule.css"), "utf8");

  test("the row lives inside the recents dropdown", () => {
    const s = src();
    const open = s.indexOf('className="schedule-recents"');
    const rowAt = s.indexOf("schedule-recents-new\"");
    expect(open).toBeGreaterThan(-1);
    expect(rowAt).toBeGreaterThan(open);
    // …and BEFORE the recents rows it sorts above.
    expect(rowAt).toBeLessThan(s.indexOf("recents.slice(0, RECENTS_SHOWN)"));
  });

  test("no inline note is left under the path field", () => {
    // The old row's two markers: its own class, and the sentence it carried.
    expect(src()).not.toContain("schedule-form-new");
    expect(src()).not.toContain("is created when the task is saved");
    expect(css()).not.toContain(".schedule-form-new {");
  });

  test("the badge is kept, and now reads inside a dropdown row", () => {
    expect(src()).toContain('<span className="schedule-new-badge">New folder</span>');
    expect(src()).toContain("Created when the task is saved");
    expect(css()).toContain(".schedule-new-badge {");
  });

  test("the field only points at the row while the row is on screen", () => {
    // aria-describedby aimed at a node that is not in the document says nothing,
    // and the row only exists while the list is open.
    expect(src()).toContain("newFolder && recentsOpen");
  });

  test("a verdict that lands on a shut dropdown reopens it", () => {
    const s = src();
    // Armed by a keystroke or by naming a folder in the picker — never by the
    // prefill an Edit opens with, which would pop a list nobody asked for.
    expect(s).toContain("revealNew.current = true;");
    expect(s).toContain("if (!newFolder || pathError || recentsOpen || !revealNew.current) return;");
  });
});

// ---- The second verb: "+ New folder" under Browse ----------------------------
describe("the + New folder button below Browse", () => {
  const src = () => readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");

  test("it sits after Browse in the same dropdown", () => {
    const s = src();
    const browse = s.indexOf("Browse…");
    const mk = s.indexOf("schedule-recents-mk");
    expect(browse).toBeGreaterThan(-1);
    expect(mk).toBeGreaterThan(browse);
    // Same row vocabulary as Browse, so the prefs-section button skin cannot
    // shrink-wrap it (that is what .schedule-form .schedule-picker-row fixes).
    expect(s).toContain('className="schedule-picker-row schedule-recents-mk"');
  });

  test("it opens the picker already naming — one flow, not a second one", () => {
    const s = src();
    expect(s).toContain("openPicker(true)");
    expect(s).toContain("const [naming, setNaming] = useState(!!startNaming);");
    // Keyed so it arrives naming even over a Browse panel still animating out.
    expect(s).toContain('key={pickerNaming ? "naming" : "browse"}');
  });

  test("a folder named in the picker ends in the same dropdown row", () => {
    const s = src();
    // onName is the picker saying "this one was NAMED, not clicked" — only then
    // does the field take focus back and the list come up with the verdict.
    expect(s).toContain("onName?.();");
    expect(s).toContain("onName={() => {");
    expect(s).toContain("pathRef.current?.focus()");
  });
});
