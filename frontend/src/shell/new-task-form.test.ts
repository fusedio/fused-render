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
let joinDraft: typeof import("./NewJobModal").joinDraft;
let backChatHref: typeof import("./NewJobModal").backChatHref;
let pastNoteFor: typeof import("./NewJobModal").pastNoteFor;
let PAST_NOTE_ONE_OFF: typeof import("./NewJobModal").PAST_NOTE_ONE_OFF;
let PAST_NOTE_CATCH_UP: typeof import("./NewJobModal").PAST_NOTE_CATCH_UP;
let defaultTargetOf: typeof import("./NewJobModal").defaultTargetOf;
let targetVerdict: typeof import("./NewJobModal").targetVerdict;
let folderFieldRows: typeof import("./NewJobModal").folderFieldRows;
let splitTargetPath: typeof import("./NewJobModal").splitTargetPath;
let PATH_MISSING: typeof import("./NewJobModal").PATH_MISSING;
let twoLevelsMissing: typeof import("./NewJobModal").twoLevelsMissing;
let saveActionLabel: typeof import("./NewJobModal").saveActionLabel;
let seededDraftForm: typeof import("./NewJobModal").seededDraftForm;

beforeAll(async () => {
  const mod = await import("./NewJobModal");
  initialRepeatKey = mod.initialRepeatKey;
  folderFieldRows = mod.folderFieldRows;
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
  joinDraft = mod.joinDraft;
  backChatHref = mod.backChatHref;
  pastNoteFor = mod.pastNoteFor;
  PAST_NOTE_ONE_OFF = mod.PAST_NOTE_ONE_OFF;
  PAST_NOTE_CATCH_UP = mod.PAST_NOTE_CATCH_UP;
  defaultTargetOf = mod.defaultTargetOf;
  targetVerdict = mod.targetVerdict;
  splitTargetPath = mod.splitTargetPath;
  PATH_MISSING = mod.PATH_MISSING;
  twoLevelsMissing = mod.twoLevelsMissing;
  saveActionLabel = mod.saveActionLabel;
  seededDraftForm = mod.seededDraftForm;
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
  // "Default" for both — the form's own default, and the case almost every
  // task is in: nothing goes on the wire and the run detects the project's own
  // config. The tests that are about the two pickers override them.
  model: "",
  effort: "",
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

  test("model and effort go on the wire ONLY when the user picked one", () => {
    // The default is "Default", which is not a value — it is the absence of a
    // flag. Sending "" would store the same thing, but then every task on the
    // machine carries two empty keys and nothing on a stored entry says whether
    // anyone chose anything. The absent key IS the answer.
    const untouched = buildSchedulePayload(form());
    expect("model" in untouched).toBe(false);
    expect("effort" in untouched).toBe(false);

    const picked = buildSchedulePayload(
      form({ model: "opus", effort: "high" }),
    );
    // The KEY travels, not the label: "opus" is what `--model` takes, "Opus" is
    // only how the card says it. Same for "high", whose field is labelled
    // Thinking.
    expect(picked.model).toBe("opus");
    expect(picked.effort).toBe("high");

    // Independent of each other — a task can pin the model and leave the
    // thinking budget alone, which is the common half of the pair.
    const modelOnly = buildSchedulePayload(form({ model: "fable" }));
    expect(modelOnly.model).toBe("fable");
    expect("effort" in modelOnly).toBe(false);
  });

  test("an edit re-states the model it was given — the choice survives cancel + re-create", () => {
    // Editing a task is not a PATCH: the entry is cancelled and a new one
    // created from whatever the form sends. A form that prefilled these but
    // dropped them from the payload would reset every edited task to the
    // default, with nothing on screen saying so.
    const body = buildSchedulePayload(
      form({ model: "opus", effort: "max", replacesEntryId: "s1" }),
    );
    expect(body).toMatchObject({ model: "opus", effort: "max", replaces: "s1" });
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

    test("a LOCKED target speaks but focuses nothing — the field is disabled", () => {
      // Inside an app's Tasks tab the target input is `disabled` (lockTarget),
      // and `.focus()` on a disabled input does nothing at all. A reason
      // returning `field: "target"` there put a sentence in the banner, moved no
      // caret, and offered no control that could answer it — a permanent dead
      // end on a card whose whole point is that a press always says something.
      for (const over of [
        { target: "" },
        { pathError: "This folder or file doesn't exist" },
      ]) {
        const loose = saveBlockedReason(gate(over));
        expect(loose?.field).toBe("target");

        const locked = saveBlockedReason({ ...gate(over), lockTarget: true });
        // Still a refusal — the folder really is gone, so the task cannot run…
        expect(locked).not.toBe(null);
        // …still a sentence, so the banner has something to show…
        expect(locked?.text).toContain("folder is missing");
        // …and the caret stays where it is.
        expect(locked?.field).toBe(null);
      }
    });

    test("a lock changes the fix, never the verdict", () => {
      // saveEnabled does not read `lockTarget` and must not: a locked path that
      // does not exist is exactly as unsaveable as a typed one, and the two
      // readers of this rule set stay in step.
      const over = { pathError: "This folder or file doesn't exist" };
      // Bound first: `saveEnabled` does not declare the field, and an inline
      // literal would be an excess-property error rather than the point of the
      // assertion — which is that the extra field changes nothing.
      const locked = { ...gate(over), lockTarget: true };
      expect(saveEnabled(locked)).toBe(false);
      // And a lock over a form with nothing wrong with its target is silent.
      expect(saveBlockedReason({ ...gate(), lockTarget: true })).toBe(null);
      // The earlier reasons still outrank it — the title is read first.
      expect(saveBlockedReason({ ...gate({ title: "", target: "" }), lockTarget: true })?.field)
        .toBe("title");
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

  test("the row lives inside the recents dropdown, under the folders", () => {
    const s = src();
    const open = s.indexOf('className="schedule-recents"');
    const rowAt = s.indexOf("schedule-recents-new\"");
    expect(open).toBeGreaterThan(-1);
    expect(rowAt).toBeGreaterThan(open);
    // …and AFTER the folder rows, which is a deliberate reversal of where it
    // sat for one round (browser QA, 2026-09-18). Leading the list also meant
    // leading the arrow ring, and ArrowDown-then-Enter — the commonest pair on
    // any typeahead — therefore CREATED a folder rather than picking the one
    // sitting right underneath it. Remembered folders first, "create this one"
    // last, which is where every tag and folder picker puts it. A ring that
    // walks one way while the list reads the other is a reader watching
    // `aria-activedescendant` jump backwards.
    expect(rowAt).toBeGreaterThan(s.indexOf("{pathRows.map("));
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

  test("the verdict never forces the dropdown open", () => {
    const s = src();
    // The reveal flag is GONE (Akshil, 2026-08-20): the verdict is debounced
    // 400ms behind the keystroke, so any "bring the list back for it" logic
    // reopened the dropdown after the user had clicked away — a dropdown that
    // could not stay dismissed. The row rides the open list only.
    expect(s).not.toContain("revealNew");
    // A folder named in the picker refocuses the field WITHOUT the focus
    // handler popping the list over the form.
    expect(s).toContain("suppressOpen.current = true;\n              window.setTimeout(() => pathRef.current?.focus(), 0);");
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

  test("its plus is the row's icon, not label text", () => {
    const s = src();
    // "+ New folder" as label text put the word on a different edge from every
    // other label in the list (Akshil, 2026-08-20) — the plus rides the icon
    // column like the folders' glyphs, and the label is just "New folder".
    expect(s).toContain("{ICON_PLUS}");
    expect(s).not.toContain(">\n                  + New folder");
  });

  test("clicking the verdict row keeps the path and closes the list", () => {
    const s = src();
    // It began as an inert role="status" div; a dead click beside five live
    // rows read as broken (Akshil, 2026-08-20). The path is already in the
    // field, so the click's whole job is the close.
    // The class is composed now — the row also wears `is-active` when the arrow
    // keys are on it — so the pair is asserted rather than the whole attribute.
    expect(s).toContain('"schedule-picker-row schedule-recents-new"');
    const row = s.indexOf("schedule-recents-new\"");
    expect(s.indexOf("onClick={() => setRecentsOpen(false)}", row)).toBeGreaterThan(row);
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

// ---- Run now vs put it on the calendar -------------------------------------
// The card serves two intentions now (Akshil, 2026-08-23): "do this" — typed on
// the List or the Board, where the when-row is folded away and `now` is only a
// default — and "plan this", from the calendar or a slot click. Nothing about
// WHEN it runs differs; what differs is whether the calendar claims it as a
// plan, and what the button says it is about to do.
describe("did anybody actually pick a time", () => {
  test("an untouched when-row on a one-off rides the wire as `immediate`", () => {
    const payload = buildSchedulePayload(form({ timePicked: false }));
    expect(payload.immediate).toBe(true);
    // …and it is still due, and still due at the same minute: the flag changes
    // what the calendar draws, never what the scheduler does.
    expect(payload.due).toBe("2026-08-17T09:00");
  });

  test("a picked time leaves the flag off the wire entirely", () => {
    expect(buildSchedulePayload(form({ timePicked: true })).immediate).toBeUndefined();
    // Absent means the same thing — every caller that is not this form.
    expect(buildSchedulePayload(form({})).immediate).toBeUndefined();
  });

  test("a repeat is never immediate, even with the row untouched", () => {
    // Ticking Repeat marks the time as picked in the component; this is the
    // belt to that braces — a rule's anchor is a chosen time by definition, and
    // the server refuses the pairing anyway.
    const rule = buildSchedulePayload(form({ rule: DAILY, timePicked: false }));
    expect(rule.immediate).toBeUndefined();
    const cron = buildSchedulePayload(
      form({ repeat: "cron", legacyCron: "0 9 * * *", timePicked: false }),
    );
    expect(cron.immediate).toBeUndefined();
  });
});

describe("what the primary button says it will do", () => {
  const now = new Date("2026-08-17T09:00:00");

  test("a time still ahead is a Schedule", () => {
    expect(saveActionLabel(new Date("2026-08-17T09:01:00"), false, now)).toBe("Schedule");
    expect(saveActionLabel(new Date("2026-09-01T08:00:00"), false, now)).toBe("Schedule");
  });

  test("now, or already past, is a Create", () => {
    // MINUTE precision, matching the field: a card opened on this minute and
    // saved unchanged runs, and must not offer to schedule the moment it is in.
    expect(saveActionLabel(new Date("2026-08-17T09:00:40"), false, now)).toBe("Create");
    expect(saveActionLabel(new Date("2026-08-17T08:59:00"), false, now)).toBe("Create");
    expect(saveActionLabel(new Date("2026-08-10T09:00:00"), false, now)).toBe("Create");
  });

  test("a repeat is always a Schedule, past anchor included", () => {
    // A past anchor gets ONE catch-up run and then a standing pattern, and the
    // pattern is the bigger fact — "Create" would name the catch-up and hide it.
    expect(saveActionLabel(new Date("2026-08-10T09:00:00"), true, now)).toBe("Schedule");
    expect(saveActionLabel(new Date("2026-09-10T09:00:00"), true, now)).toBe("Schedule");
  });

  test("an unreadable date does not claim it is about to run", () => {
    expect(saveActionLabel(null, false, now)).toBe("Schedule");
    expect(saveActionLabel(new Date("nonsense"), false, now)).toBe("Schedule");
  });
});

describe("where the when-row lives", () => {
  const src = () => readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");

  test("it is inside More options, which opens itself on a planning card", () => {
    const s = src();
    const more = s.indexOf('<details className="schedule-form-more"');
    expect(more).toBeGreaterThan(-1);
    // The when-row now sits AFTER the disclosure opens, not on the card's face.
    expect(s.indexOf("Google's when-row")).toBeGreaterThan(more);
    // Openness is React state, not a bare `open` attribute: a half-controlled
    // <details> would slam shut on the next re-render, under the user's hand.
    // …and for a re-opened DRAFT that carries a time, which is the one other
    // card whose when-row holds an answer the reader already gave.
    expect(s).toContain(
      "const [moreOpen, setMoreOpen] = useState(planning || saved.when !== null);",
    );
    expect(s).toContain("onToggle={(e) => setMoreOpen(e.currentTarget.open)}");
  });

  test("the three when-controls are what mark the time as picked", () => {
    const s = src();
    // The date grid, the time list, and the Repeat tick — every way a person
    // can state an opinion about when.
    expect(s).toContain("setTimePicked(true);");
    expect(s).toContain("if (on) setTimePicked(true);");
    // And the opening value: an edit inherits what the entry was stored as, a
    // new card is planned exactly when the caller says it is planning.
    // …and a re-opened draft that stored a time is planned by definition: the
    // draft only carries `when` once somebody opened the row and picked one.
    expect(s).toContain("(saved.when !== null ? true : editing ? !editing.immediate : planning)");
  });
});

// ---- the card EDITS the composer's record; it never copies it ---------------
//
// design "one record", §1. The Schedule hop used to carry the sentence here in a
// URL param and a sessionStorage stash, and the card's first autosave minted a
// `draft:<id>` over it carrying a `from_chat_key` that told the server to delete
// the chat's copy. For the length of that round trip the same words were in two
// stores; "Back to chat" had to reverse the whole thing in four ordered steps;
// and pressing Schedule inside the 600 ms debounce meant the move never happened
// at all. The card now opens ON the chat record and autosaves back onto it, so
// there is nothing to move, nothing to reverse, and nothing to race.
//
// Source reads, because what these pin down is which record a call names.

describe("the card edits one record, and says which", () => {
  const src = () => readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");

  test("a chat hop autosaves onto the chat key, words joined back into one string", () => {
    const s = src();
    expect(s).toContain('const recordKey = (chatKey ?? "") || (editing?.session_id ?? "");');
    const save = s.slice(s.indexOf("const autosave = useAutosave(draftBody,"),
                         s.indexOf("const autosaveRef = useRef(autosave);"));
    expect(save).toContain("if (recordKey) {");
    expect(save).toContain("joinDraft(value.title, value.description),");
    // THE SETTINGS RIDE AS A `form` PATCH and the words do NOT: `text` is the
    // composer's own box, which is what makes the round trip lossless
    // (contract §1).
    expect(save).toContain("new_task_each_run: value.new_task_each_run,");
    expect(save.slice(save.indexOf("if (recordKey) {"), save.indexOf("let id = draftIdRef")))
      .not.toContain("description:");
    // STATED, NOT SENT: the chat-keyed road defers too, so a keystroke starts no
    // 600 ms timer of its own.
    expect(save).toContain("{ defer: true },");
  });

  test("…and a card with no chat behind it still mints a task draft", () => {
    const save = src().slice(src().indexOf("const autosave = useAutosave(draftBody,"));
    expect(save).toContain("id = newTaskDraftId();");
    // ONE WRITER, TWO SHAPES: the same syncer, keyed `draft:<id>` instead of on
    // the chat. The card states what it wants; nothing here dispatches — and
    // `defer` means nothing here SENDS either, until a flush moment comes.
    expect(save).toContain("draftSyncer(taskDraftKey(id)).setTask(value, { defer: true });");
  });

  test("…and closing the card is one of those flush moments", () => {
    // Nothing is written per keystroke any more, so the card's own teardown has
    // to carry the last edits out. Discard and Schedule have already reset the
    // autosave, so this finds nothing to send on those roads.
    expect(src()).toContain("useEffect(() => () => autosaveRef.current.flush(), []);");
  });

  test("the move's machinery is gone from the card", () => {
    const s = src();
    for (const gone of ["fromChatKey", "from_chat_key", "originChatKey",
                        "chatKeySpent", "hopSeeded", "writeInitial"]) {
      expect(s).not.toContain(gone);
    }
  });

  test("the card adopts whatever id the write actually landed on", () => {
    // The server can still fold a write into a draft that already holds this
    // conversation and answer the id it landed on (drafts.py `put_task`, Bugbot
    // PR #1126). Keeping the minted id would aim the next autosave, the Discard
    // and Schedule's `draft_id` at a record that does not exist.
    const s = src();
    expect(s).toContain("onTaskId: (id: string) => {");
    expect(s).toContain("if (!id || id === draftIdRef.current) return;");
    expect(s).toContain("draftIdRef.current = id;");
    expect(s).toContain("setDraftId(id);");
  });

  test("Discard deletes whichever record the card was writing into", () => {
    const s = src();
    const discard = s.slice(s.indexOf("const discard = async () => {"),
                            s.indexOf("const picked = useMemo("));
    // `reset(null)` is what stops this card from pushing the emptied form on.
    // The ordering the old code needed — stop, await settle, THEN delete — is
    // the syncer's now: Discard SAYS the record should not exist, to the one
    // thing that is writing it, and waits for the server to agree.
    expect(discard).toContain("autosaveRef.current.reset(null);");
    expect(discard).not.toContain("settle()");
    expect(discard).toContain("sync.markDeleted();");
    expect(discard).toContain("await sync.handoff();");
  });

  test("Back to chat flushes and walks — the record is untouched", () => {
    const s = src();
    const back = s.slice(
      s.indexOf("const backToChat = async () => {"),
      s.indexOf("// The replacement was created but the original could not be withdrawn"),
    );
    // The two-step dirty guard still comes first: one click must not silently
    // abandon an adjusted form (Bugbot, PR #548).
    expect(back.indexOf("if (dirty && !backConfirm)"))
      .toBeLessThan(back.indexOf("autosaveRef.current.flush();"));
    // FLUSH, because the last 600 ms of typing belong in the box being walked
    // back to — and then nothing else at all.
    expect(back).toContain("navigateUrl(backHref);");
    expect(back).not.toContain("saveChatDraft(");
    expect(back).not.toContain("deleteTaskDraft(");
    expect(back).not.toContain("settle()");
  });

  test("…and it is offered on every card that has a record to go back to", () => {
    const s = src();
    expect(s).toContain("const backHref = chatBack || backChatHref(recordKey, target);");
    expect(s).toContain("const canGoBack = !!backHref;");
    expect(s).toContain("{canGoBack && (");
  });

  test("joinDraft is splitDraft run backwards, blank line and all", () => {
    expect(joinDraft("roll up the PRs", "group them by repo"))
      .toBe("roll up the PRs\n\ngroup them by repo");
    // A round trip through both is the identity on anything the split produced.
    const one = "roll up the PRs\n\ngroup them by repo";
    const parts = splitDraft(one);
    expect(joinDraft(parts.title, parts.description)).toBe(one);
    // Either half alone is just that half — no separator for the missing one.
    expect(joinDraft("", "just a body")).toBe("just a body");
    expect(joinDraft("just a title", "")).toBe("just a title");
    expect(joinDraft("", "")).toBe("");
    expect(joinDraft(null, null)).toBe("");
  });

  test("where Back to chat lands, by the shape of the key", () => {
    // A session id: the record's own folder with that thread on the Claude pane.
    expect(backChatHref("sess-9", "/Users/me/proj"))
      .toBe("/explorer/view/Users/me/proj?_side=claude&session_id=sess-9");
    // `new:<file>`: the folder's chat with no session named at all, built out
    // of the key's own file (schedule-lib.chatPaneUrl) — or the composer that
    // opens seeds from a key nothing wrote.
    expect(backChatHref("new:/Users/me/news", "/Users/me/elsewhere"))
      .toBe("/explorer/view/Users/me/news?_side=claude");
    // Nothing to go back to.
    expect(backChatHref("", "/Users/me/proj")).toBe("");
    expect(backChatHref("sess-9", "")).toBe("");
    expect(backChatHref("new:", "/Users/me/proj")).toBe("");
  });

  test("Schedule leaves the delete to the server, and names the right key", () => {
    // `POST /api/schedule` is handed `draft_id` OR `draft_key` and drops the
    // record itself, so this path only has to make sure nothing queued can write
    // it back — one `reset`, where it used to be flush/stop/settle.
    const s = src();
    const submit = s.slice(s.indexOf("// THE DRAFT STOPS HERE."));
    expect(submit.indexOf("autosaveRef.current.reset(null);"))
      .toBeLessThan(submit.indexOf("await scheduleMessage("));
    expect(submit).not.toContain("deleteTaskDraft");
    expect(submit).toContain('draftId: draftIdRef.current ?? "",');
    expect(submit).toContain("draftKey: recordKey,");
    // A card edits ONE record, so the payload's two keys are alternatives.
    expect(buildSchedulePayload(form({ draftKey: "new:/Users/me/news" })).draft_key)
      .toBe("new:/Users/me/news");
    expect("draft_key" in buildSchedulePayload(form())).toBe(false);
    expect("draft_key" in buildSchedulePayload(form({ draftKey: "" }))).toBe(false);
  });

  test("a second press on the same draft is the same card", () => {
    // design §1: "double press → same modal/composer (guard by key)". The card's
    // identity is the RECORD it is open on, so pressing the same row twice — or
    // landing on the hop's URL twice — re-mounts nothing and opens no second
    // draft. (`openSeq` still forces a fresh mount for a DIFFERENT opening: the
    // fields are `useState` initialisers, so a card that stayed mounted would
    // keep the previous record's answers.)
    const page = readFileSync(join(import.meta.dir, "Scheduled.tsx"), "utf8");
    expect(page).toContain("hop.key\n              ? `chat:${hop.key}`");
    expect(page).toContain("? `draft:${draftSeed.id}`");
  });

  test("the page hands the modal the key the hop named, and the route back", () => {
    const page = readFileSync(join(import.meta.dir, "Scheduled.tsx"), "utf8");
    // The whole handoff: which record, and where to go back to.
    expect(page).toContain('const key = q.get("draft") ?? "";');
    // …and the FOLDER, which is the one thing a session key cannot state.
    expect(page).toContain(
      'openChatRecord(key, q.get("from") ?? "", q.get("target") ?? "");',
    );
    expect(page).toContain("chatKey={hop.key}");
    expect(page).toContain("chatBack={hop.from}");
    // …and the params that used to carry the words are gone from the page.
    for (const gone of ['q.get("message")', 'q.get("attachments")',
                        'q.get("session_id")', 'q.get("back")']) {
      expect(page).not.toContain(gone);
    }
  });

  test("a TASK draft's own arm takes a route back too (Akshil, 2026-09-16)", () => {
    // `?draft=<id>` is how a draft ROW opens the card, and a row has nowhere to
    // walk back to. The chat composer's Schedule presses the same arm now — a
    // never-sent chat mints a `draft:<id>` per press instead of writing one
    // `new:<file>` per folder — and that press DID come out of a conversation,
    // so it names the way home and the card draws "Back to chat" for it. The
    // hop carries no key: this card is editing a task draft, not a chat record.
    const page = readFileSync(join(import.meta.dir, "Scheduled.tsx"), "utf8");
    expect(page).toContain('const hopTo: ChatHop = from ? { key: "", from } : NO_HOP;');
    expect(page).toContain(
      "const seed: DraftSeed = { id, form: stored ? { ...stored } : null };",
    );
    expect(page).toContain("openForm(openAt(seed), null, hopTo, seed);");
    expect(page).toContain("openForm(openAt(null), null, hopTo, { id, form: null });");
    // …and the param is SPENT, like every other one this page reads off a link.
    expect(page).toContain(
      'q.delete("draft");\n    q.delete("hop");\n    q.delete("from");',
    );
  });

  test("a hop out of a composer opens on the lead date, a row does not", () => {
    // Bugbot 4028344051. `?draft=<id>` serves two presses that are not the same
    // gesture. A draft ROW is a REOPEN: it takes the time the draft stored, and
    // an immediate draft must stay immediate (`reopenTime`). A Schedule press
    // out of a never-sent chat is a HOP, the same gesture `?new=1` makes, and it
    // has to land the same way — now+2m, card planning, when-row open — or the
    // confirm names a time the task never waits for and it runs at once.
    const page = readFileSync(join(import.meta.dir, "Scheduled.tsx"), "utf8");
    expect(page).toContain('const hopped = q.get("hop") === "1";');
    expect(page).toContain("const lead = new Date(Date.now() + NEW_LINK_LEAD_MS);");
    // …and a hop whose record already names a time still opens on THAT time:
    // one made, walked back from and made again is a reopen of the reader's own
    // answer.
    expect(page).toContain("hopped ? reopenTime(seed) ?? lead : null;");
  });
});

// ---- the record this card is editing changed somewhere else ------------------
//
// design §3. It used to hear `onGone` — the LISTING reporting a row that left —
// and stand its own autosave down, which needed a verifying `GET /api/drafts`
// because `gone` means "this key is not a row", not "this draft was deleted": a
// session-bound card's very first save announced `draft:<id>` gone while the
// card was still open, and stopping on that alone silently disarmed every edit
// after it (Bugbot #1166). The change feed now says which DRAFT keys moved, so
// there is nothing to verify.

describe("the card closes when its record is discarded elsewhere", () => {
  const src = () => readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");

  function goneBody(s: string): string {
    const start = s.indexOf("useEffect(() => onDraftChange((_changed, gone, certain) => {");
    const end = s.indexOf("   * DISCARD — the draft goes, and so does the card.");
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    return s.slice(start, end);
  }

  test("it subscribes to the draft channel, not to the listing's rows", () => {
    const s = src();
    expect(s).toContain('import { onDraftChange } from "./tasksPulse";');
    expect(s).not.toContain("onGone(");
    // …and it no longer has to read the store back to find out what `gone` meant.
    expect(goneBody(s)).not.toContain("fetchDrafts()");
  });

  test("only a key this client holds a version for is acted on — from the FEED", () => {
    // The announced key set is noisy by construction (contract §3), and closing
    // a card on a `gone` for a record that never existed would be the worst
    // possible reading of it. A discard made on THIS page is the exception and
    // says so (`certain`): the delete it came out of has already forgotten the
    // very version this guard asks for, so without the exemption the card sat
    // open over a record that no longer existed.
    const body = goneBody(src());
    expect(body).toContain("if (!certain && draftVersion(mine) === undefined) return;");
    expect(body.indexOf("draftVersion(mine) === undefined"))
      .toBeLessThan(body.indexOf("onClose();"));
  });

  test("and it asks about whichever record the card is writing into", () => {
    const body = goneBody(src());
    expect(body).toContain("const mine = key || (id ? taskDraftKey(id) : \"\");");
    expect(body).toContain('notify({ title: "Discarded elsewhere", tone: "info" });');
  });
});

// ---- a draft remembers which conversation it is going into -------------------
//
// THE BUG (Akshil, 2026-09-12). The composer's Schedule button can hop out of a
// chat that HAS ALREADY RUN, and the task being written is the next message of
// that thread. Press Schedule straight away and it landed there — `session_id`
// was still in page state. Exit the card and the draft on disk knew nothing
// about it, so reopening the draft and scheduling it opened a SECOND session
// with a SECOND task number, and the TASK-nnn the reader had been watching was
// gone.
//
// `from_chat_key` could not stand in: that is where the words were TYPED and it
// is spent the moment the chat's copy is deleted. This is where the task is
// GOING, and it has to be stored.

describe("a draft remembers the conversation it is a message to", () => {
  const src = () => readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");

  test("all three openings answer it, the same way the chat key is answered", () => {
    const s = src();
    // THE KEY FIRST (Akshil, 2026-09-17). A hop out of a chat that has run
    // opens the card on that conversation's own record, which is filed under
    // the session id — so the key the card is editing IS the thread, and a card
    // that read it as "" booked the message as a task of its own beside the
    // chat ("scheduling a task creates double entries"). Then what the stored
    // record said, for a card reopened long after the hop; then the prop, for
    // any caller that still hands one over.
    expect(s).toContain(
      "  const boundSessionId =\n"
      + '    chatKeySession(chatKey ?? "")\n'
      + '    || (chatSessionId ?? "")\n'
      + '    || (saved.sessionId ?? "");',
    );
    expect(s).toContain('sessionId: str("session_id"),');
  });

  test("the autosave body carries it, so the hop's first write stores it", () => {
    const s = src();
    // In the BODY rather than beside it: `from_chat_key` is a side effect (it
    // makes the server delete something) and is spent once, where this is plain
    // state and is restated on every save.
    expect(s).toContain("      session_id: boundSessionId,");
    const body = s.slice(
      s.indexOf("const draftBody: TaskDraftForm | null ="),
      s.indexOf("const chatKeySpent = useRef(false);"),
    );
    expect(body).toContain("session_id: boundSessionId,");
  });

  test("a reopened draft's Schedule payload carries it", () => {
    // The whole bug in one line: without the stored id this read
    // `|| chatSessionId || ""`, and a reopened card has no `chatSessionId` —
    // the hop that made it is long over.
    expect(src()).toContain(
      "sessionId: (!learnedSession && editing?.session_id) || boundSessionId,",
    );
  });

  test("…and the payload sends it as the session to continue", () => {
    // Nothing new on the wire: `session_id` is the field a fresh hop already
    // used, which is why the entry lands in the thread and keeps its number.
    expect(buildSchedulePayload(form({ sessionId: "sess-9" })).session_id)
      .toBe("sess-9");
    // A draft that belongs to nobody leaves the key off entirely, as it always
    // did — a chat with no session yet included.
    expect("session_id" in buildSchedulePayload(form({ sessionId: "" }))).toBe(false);
  });

  test("a draft row still opens the form, session or no session", () => {
    const views = readFileSync(join(import.meta.dir, "ScheduleTaskViews.tsx"), "utf8");
    // The row's rule is the row's: a draft opens the New task modal, and the
    // draft arm is asked before the chat arm.
    expect(views).toContain("const openDraft = onOpenDraft && isDraftTask(task) ? onOpenDraft : null;");
    expect(views).toMatch(
      /const activate = \(\) => \{[\s\S]*?if \(openDraft\) openDraft\(task\);\s*else if \(chat\) openChat\(chat\);/,
    );
    // …and a bound draft is the first row that could ALSO have answered
    // `taskHref`, so the chat arm is refused outright rather than merely
    // out-ranked: otherwise the row draws a real <a href> at the conversation
    // and ⌘-click, middle click and "Open in new tab" all go somewhere the
    // plain click does not.
    expect(views).toContain(
      "const chat = folderMissing || isDraftTask(task)\n"
      + "    ? null\n"
      + "    : openThreadIntent(task, unread);",
    );
  });
});

// ---- a Custom repeat survives being drafted ----------------------------------
//
// THE BUG (Bugbot, PR #1118). The autosave stored `repeat` — the preset KEY —
// and nothing else. Every other key is its own whole answer ("every day" needs
// no second field); "custom" is a pointer at a rule the recurrence dialog built.
// So a Custom draft reopened saying Custom, holding no rule, with Save refused
// (`saveEnabled`) and nothing on the card explaining the dead button.

describe("a Custom repeat survives being drafted", () => {
  const src = () => readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");

  test("the rule comes back off the stored draft", () => {
    const rule: RecurrenceRule = { freq: "week", interval: 2, byday: [1, 3] };
    const seeded = seededDraftForm({
      id: "d1",
      form: { repeat: "custom", custom_rule: rule },
    });
    expect(seeded.repeat).toBe("custom");
    expect(seeded.customRule).toEqual(rule);
  });

  test("and a draft with no rule in it still opens, saying nothing", () => {
    expect(seededDraftForm({ id: "d1", form: {} }).customRule).toBeNull();
    expect(seededDraftForm(null).customRule).toBeNull();
  });

  test("a shape this build cannot read costs the rule and never the card", () => {
    // A draft is loose JSON that may have been written by another build, and
    // this runs inside a `useState` initialiser: a throw here is a card that
    // will not open at all.
    for (const bad of [null, "every week", 7, [], {}, { freq: "fortnight" }]) {
      expect(seededDraftForm({ id: "d1", form: { custom_rule: bad } }).customRule)
        .toBeNull();
    }
  });

  test("the form stores the rule only while the choice points at one", () => {
    const s = src();
    // Null the same moment `repeat` itself goes null, so a draft can never say
    // "custom" with nothing behind it.
    expect(s).toContain('custom_rule: repeatOn && repeat === "custom" ? customRule : null,');
    // …and the card seeds its state back off it, which is the half that makes
    // storing it worth anything.
    expect(s).toContain('if (saved.repeat === "custom" && saved.customRule) return saved.customRule;');
  });
});

// ---- a draft exists only when there are words or files -----------------------
//
// THE RULE (Akshil, 2026-09-12): a task draft is non-empty iff
// `title.strip() or description.strip() or attachments`. The folder, the model,
// the effort, the permission mode, the time and the repeat rule are SETTINGS —
// how a task would run, not a task. They used to mint one, so opening the card
// and changing the folder (or opening the when-row and picking a time) put an
// "Untitled draft" row on the List for a form holding nothing anybody typed.
//
// Source reads, because what these pin is a `useState`-adjacent expression and
// the ORDER of two conditions — a rendered form with a stubbed fetch would only
// show that a write happened, not which change armed it.

describe("only words or files make a draft", () => {
  const src = () => readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");

  test("the content predicate is the three things and nothing else", () => {
    const s = src();
    expect(s).toContain('const draftContent = !!title.trim() || !!message.trim()');
    expect(s).toContain("|| images.some((i) => i.path);");
    // A chip whose upload has not answered names no file yet — the same rule
    // the body's own `attachments` uses.
    expect(s).toContain('.filter((i) => i.path)');
  });

  test("the FIRST write waits for content — a settings-only change mints nothing", () => {
    const s = src();
    expect(s).toContain(
      "const draftBody: TaskDraftForm | null = (!editing || !!recordKey) && dirty\n"
      + "    && (draftContent || draftId !== null || !!recordKey)",
    );
    // The gate is on the BODY, which is what the autosave watches: null is not
    // a value it can write, so no id is minted and no PUT goes out.
    expect(s).toContain("const autosave = useAutosave(draftBody,");
    expect(s).toContain("if (!value) return;");
  });

  test("…and once a draft exists, emptying it is a write, not a silence", () => {
    // The body keeps being produced after the words are gone, so the PUT goes
    // out and the server turns it into a delete (or, for a chat record carrying
    // a form, into "clear the words, keep the settings" — contract §2). Without
    // it the card fell silent at exactly the moment it had something to say —
    // the reported "clear the text, then lose the attachment, and the draft
    // never clears".
    expect(src()).toContain("(draftContent || draftId !== null || !!recordKey)");
  });

  test("and NOTHING is minted by merely opening a card (design §4)", () => {
    // `dirty` is the whole gate: a hop no longer arrives holding words that
    // exist nowhere else — they are already on the record the card is editing —
    // so the `writeInitial` arm that used to force a write at mount is gone with
    // the reason for it. Open the card, press ✕, and nothing at all happened.
    const s = src();
    expect(s).not.toContain("writeInitial");
    expect(s).not.toContain("hopSeeded");
    expect(s).toContain("&& dirty\n");
  });

  test("an EDIT writes too, when it has a record to write into (design §4)", () => {
    // An Edit used to autosave nowhere at all, so ✕ on a card with ten minutes
    // of changes in it dropped every one of them silently. A task that has run
    // has a session, and that session has a chat record.
    const s = src();
    expect(s).toContain('const recordKey = (chatKey ?? "") || (editing?.session_id ?? "");');
    expect(s).toContain("const draftBody: TaskDraftForm | null = (!editing || !!recordKey)");
  });
});

// ---- Delete and Discard are one seat, one skin -------------------------------
//
// They can never both be on a card — `del` belongs to an Edit and `draftId` to a
// new task — so they are not two controls sharing a footer: they are the same
// control under the two names the card can be in. Two weights for one position
// read as a footer that moves its buttons around depending on what you opened
// (Akshil, 2026-09-12).

describe("Delete and Discard share one seat and one skin", () => {
  const src = () => readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");

  test("Discard wears Delete's class, so it takes Delete's far-left seat", () => {
    const s = src();
    // `.btn-danger-text` is what carries `margin-right: auto`; `.new-task-delete`
    // carries the glyph spacing. The old `btn-secondary new-task-discard` skin
    // is gone, and its absence is the assertion that matters.
    expect(s).toContain('className="btn btn-danger-text new-task-delete"');
    expect(s).not.toContain("new-task-discard");
    expect(s).not.toContain('className="btn btn-secondary new-task-discard"');
  });

  test("…and Delete's glyph, with the label and the tooltip that are its own", () => {
    const s = src();
    const discard = s.slice(s.indexOf("{(draftId || (recordKey && !editing)) && ("));
    const button = discard.slice(0, discard.indexOf("</button>"));
    expect(button).toContain("{ICON_TRASH}");
    expect(button).toContain('title="Discard this draft"');
    expect(button).toContain("Discard");
    // No arming step: there is nothing scheduled here to undo.
    expect(button).not.toContain("is-armed");
  });

  test("only one is ever drawn, because only one condition can hold", () => {
    const s = src();
    // An Edit has no draft to discard and a new card has no entry to delete —
    // and the `!editing` is load-bearing since design §4 gave an Edit a record
    // to autosave into (`recordKey`): without it, an Edit on a task that has run
    // would draw both, which is the one thing this seat may not do.
    expect(s).toContain("const del = deleteActionFor(editing);");
    expect(s).toContain("{del && (");
    expect(s).toContain("{(draftId || (recordKey && !editing)) && (");
  });
});

// ---- a Schedule hop out of a chat that already has a form --------------------
//
// design "one record", §1 + contract §1 ("one record, two doors"). A New task
// card bound to a session IS that conversation's unsent message, so the server
// serves the form ON the session's chat record. The hop opens that record; so
// does the Draft chip's press on the thread line. There is no lookup by session
// across task drafts any more, no merge of the composer's words over a stored
// form, and no second draft for the two to disagree about — the words and the
// settings were on one record the whole time.

describe("a Schedule hop out of a chat that already has a form", () => {
  const page = () => readFileSync(join(import.meta.dir, "Scheduled.tsx"), "utf8");
  let chatHopSeed: typeof import("./Scheduled").chatHopSeed;
  beforeAll(async () => {
    ({ chatHopSeed } = await import("./Scheduled"));
  });

  const record = (over: Record<string, unknown> = {}) => ({
    text: "Ship the changelog\n\nthen tag it",
    attachments: [],
    updated_at: 2,
    version: 3,
    form: { target: "/Users/me/proj", when: null, repeat: null, model: "opus" },
    ...over,
  }) as unknown as import("@platform/lib/drafts").ChatDraft;

  test("it opens on the record the key names, settings and all", () => {
    const seed = chatHopSeed("sess-a", record());
    const form = seed?.form as Record<string, unknown>;
    // THE WORDS COME OUT OF `text`, SPLIT — the title is the first line, which
    // is also what the listing row prints (contract §1).
    expect(form.title).toBe("Ship the changelog");
    expect(form.description).toBe("then tag it");
    // …and the settings ride along untouched.
    expect(form.model).toBe("opus");
    expect(form.target).toBe("/Users/me/proj");
  });

  test("a session key IS the thread this task is a message to", () => {
    const seed = chatHopSeed("sess-a", record());
    expect((seed?.form as Record<string, unknown>).session_id).toBe("sess-a");
    // `new:<file>` has no thread to continue, and its `<file>` is the folder the
    // card falls back to when the record names no target of its own.
    const fresh = chatHopSeed("new:/Users/me/news", record({ form: {} }));
    const form = fresh?.form as Record<string, unknown>;
    expect(form.session_id).toBe("");
    expect(form.target).toBe("/Users/me/news");
  });

  test("the tray comes off the record, where the composer left it", () => {
    const file = { path: "/shots/a.png", name: "a", kind: "image" as const };
    const seed = chatHopSeed("sess-a", record({ attachments: [file] }));
    expect((seed?.form as Record<string, unknown>).attachments).toEqual([file]);
  });

  test("no record is no seed — the card opens fresh on the key it was given", () => {
    expect(chatHopSeed("sess-a", null)).toBeNull();
  });

  test("the hop pays for ONE read, and a stale answer is dropped", () => {
    const s = page();
    // The answer can arrive late, so it takes the page's own generation and a
    // press through any other door owns the modal.
    expect(s).toContain("const gen = ++chatDraftGen.current;");
    expect(s).toContain("const found = all && chatHopSeed(key, all.chat[key] ?? null, at);");
    // A LOOKUP THAT FAILED IS NOT "THERE IS NONE" (`fetchDrafts` answers null
    // for a blip). The card opens anyway, on the key it was given — it is the
    // SAME record either way, so an uninformed card costs a moment of empty
    // fields and never a second draft.
    expect(s).toContain("openForm(lead, null, hopTo);");
    // …and a `?new=1` with no key at all is the app page's own link: nothing to
    // read, so it opens in this tick — on whatever folder and route back the
    // link happened to name, which is how an EMPTY never-sent composer's
    // Schedule travels now that it mints no record to hand over (Akshil,
    // 2026-09-16).
    expect(s).toContain("if (!key) {\n      // …THOUGH IT MAY STILL SAY WHERE IT CAME FROM.");
    expect(s).toContain("from ? { key: \"\", from } : NO_HOP,");
    expect(s).toContain("at0 ? { id: \"\", form: { target: at0 } } : null,");
  });
});

// ---- reopening a form does not reschedule it ---------------------------------
//
// THE BUG (Bugbot, PR #1126, 2026-09-12). `NEW_LINK_LEAD_MS` — now+2m — is what
// a FRESH deep link opens on, and the bound-draft doors were handing it to forms
// that already existed. A Date in `creating` makes the card `planning`,
// `planning` opens `timePicked` true, and `timePicked` is what puts `when` into
// the next autosave — so merely reopening a draft that had been left IMMEDIATE
// rewrote it as scheduled two minutes out, and Schedule sent it that way.

describe("a reopened form opens on its own time, or on none", () => {
  const page = () => readFileSync(join(import.meta.dir, "Scheduled.tsx"), "utf8");
  let reopenTime: typeof import("./Scheduled").reopenTime;
  beforeAll(async () => {
    ({ reopenTime } = await import("./Scheduled"));
  });

  const seedWith = (when: unknown) =>
    ({ id: "draft-0001", form: { when } }) as import("./NewJobModal").DraftSeed;

  test("no stored time is no time at all — an immediate draft stays immediate", () => {
    // The store keeps `when` null until somebody opens the when-row and picks
    // one, so null here is the positive statement "run it now", not a gap.
    expect(reopenTime(seedWith(null))).toBeNull();
    expect(reopenTime(seedWith(""))).toBeNull();
    expect(reopenTime({ id: "draft-0001", form: null })).toBeNull();
    expect(reopenTime(null)).toBeNull();
  });

  test("a stored time comes back as the time it says", () => {
    const at = reopenTime(seedWith("2026-09-20T08:30"));
    expect(at).toBeInstanceOf(Date);
    // The field's format is local, so it reads back as the local minute the
    // reader picked rather than drifting by the zone offset.
    expect(at?.getFullYear()).toBe(2026);
    expect(at?.getMonth()).toBe(8);
    expect(at?.getDate()).toBe(20);
    expect(at?.getHours()).toBe(8);
    expect(at?.getMinutes()).toBe(30);
  });

  test("an unreadable one reads as none, and never as an Invalid Date", () => {
    // The card seeds its own field from the string verbatim, so nothing is lost
    // by declining to guess — and an Invalid Date is still `instanceof Date`,
    // which is exactly the thing that would flip `planning` back on.
    expect(reopenTime(seedWith("whenever"))).toBeNull();
    expect(reopenTime(seedWith(1758350000000))).toBeNull();
  });

  test("every door takes it, because they are all ONE door now", () => {
    const s = page();
    // The Draft chip's press on the thread line, a draft row's press, and the
    // composer's own hop are three callers of one function — so the rule cannot
    // be right in one of them and wrong in another, which is what it was.
    expect(s).toContain("const openChatRecord = (key: string, from: string, at: string) => {");
    expect(s).toContain("const found = all && chatHopSeed(key, all.chat[key] ?? null, at);");
    expect(s).toContain("openForm(found ? reopenTime(found) : lead, null, hopTo, found);");
    expect(s).toContain("openChatRecord(session, draftChatUrl(task), task.project || task.file || \"\");");
    expect(s).toContain("openChatRecord(task.key, draftChatUrl(task), task.project || task.file || \"\");");
    // The lead date survives exactly where it means something: a card with no
    // stored record behind it.
    expect(s).toContain("const lead = new Date(Date.now() + NEW_LINK_LEAD_MS);");
    expect(s).toContain("openForm(lead, null, hopTo);");
  });

  test("the ordinary draft row never had the bug — it passes no time and still does", () => {
    // `openDraft` opens on the stored form alone, so the card decides
    // `timePicked` from the form's own `when` and an immediate draft is left
    // immediate. Pinned so the lead date cannot drift into this door either.
    const s = page();
    const open = s.slice(s.indexOf("const openDraft = (task: Task) => {"),
                         s.indexOf("const openBoundDraft = (task: Task) => {"));
    expect(open).toContain(
      "openForm(null, null, NO_HOP, { id: task.draft_id, form: task.form ?? null });");
    expect(open).not.toContain("NEW_LINK_LEAD_MS");
  });
});

// ---- the path inside an app (design.md §2) -----------------------------------
//
// The app page's Tasks tab mounts this same card scoped to one folder, and a
// task made there runs against that app. The combobox used to ask anyway —
// prefilled, but every answer it accepted took the reader out of the app they
// were standing in. Source assertions, because the card is a component and the
// lock is markup rather than a function.
describe("the path locks to the app the card was opened in", () => {
  const card = () => readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");
  const page = () => readFileSync(join(import.meta.dir, "Scheduled.tsx"), "utf8");

  test("the scoped page is the only caller that locks it", () => {
    // `scope` IS the app page's Tasks tab (AppPage.tsx) — the unscoped /tasks
    // route passes none, and its card keeps every folder it ever had.
    expect(page()).toContain("lockTarget={!!scope}");
  });

  test("one `disabled` closes the field, the list and the picker together", () => {
    const src = card();
    // Disabling the input is what makes the whole combobox inert: no focus, so
    // `openRecents` never fires, so the dropdown — and Browse, and the picker
    // behind it — cannot be reached. A second rule per affordance is how the
    // three start disagreeing.
    expect(src).toContain("disabled={lockTarget}");
    expect(src).toContain("readOnly={lockTarget}");
    // …and it stops claiming to be a combobox, since nothing can expand.
    expect(src).toContain('role={lockTarget ? undefined : "combobox"}');
  });

  test("it says why, and the line is attached to the field", () => {
    const src = card();
    expect(src).toContain("Tasks here run against this project.");
    // Described-by, not merely printed underneath: a reader who never sees the
    // layout still hears the reason from the field itself.
    expect(src).toContain("? lockedTargetId");
  });
});

// ---- the header says which task this came out of (design.md B, Option 1) -----
describe("the source-task chip", () => {
  const card = () => readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");
  const page = () => readFileSync(join(import.meta.dir, "Scheduled.tsx"), "utf8");

  test("the session is what resolves it, from the listing the page already holds", () => {
    const src = page();
    // Both doors name the same conversation: the composer's hop, and the draft
    // that hop saved (which is what survives closing and reopening the card).
    // The hop's KEY is the session when the chat has one (`new:<file>` is the
    // shape that has none), and the seed restates it for a card reopened from a
    // stored record. Through `chatKeySession`, which is that rule written down
    // once: the card asks the same question of the same key, and the two
    // answering differently is what put a scheduled follow-up beside its own
    // conversation instead of in it.
    expect(src).toContain("const session = chatKeySession(hop.key)");
    expect(src).toContain("|| seededDraftForm(draftSeed).sessionId || \"\";");
    expect(src).toContain("return tasks.find((t) => t.session_id === session) ?? null;");
    // No second fetch: `tasks` is the poll this page runs anyway.
    expect(src).not.toContain("getTaskBySession");
  });

  test("an Edit never wears one", () => {
    // That heading already IS the task, and the card is not continuing anything.
    expect(page()).toContain("if (editing) return null;");
    expect(card()).toContain('title={editing ? "Edit task" : "New task"}');
    expect(card()).toContain("{...(editing || !sourceTask ? {} : {");
  });

  test("pressing it puts the card away first, then opens the peek", () => {
    const src = page();
    const open = src.slice(src.indexOf("const openSourceTask = (task: Task) => {"));
    const body = open.slice(0, open.indexOf("\n  };"));
    // The peek slides in BESIDE a page that is currently behind a modal, so
    // opening one under the card would look like the press did nothing.
    expect(body.indexOf("closeCard();")).toBeLessThan(body.indexOf("openPeek(task.key)"));
    // The peek is the only door this CHIP has: pressing it must not leave the
    // page. (The page does navigate elsewhere now — a draft row's press opens
    // its chat, design §1 — so the claim is about this handler, not the file.)
    expect(body).not.toContain("navigateUrl");
  });

  test("where there is no peek, the chip is a statement and not a dead button", () => {
    // The page hands over the name alone…
    expect(page()).toContain("onOpen: peekable ? () => openSourceTask(sourceTask) : null,");
    // …and the card draws a span for it.
    expect(card()).toContain(
      '<span className="new-task-source">from {shortTaskId(sourceTask.taskId)}</span>');
  });

  test("it is beside the heading, never inside it", () => {
    // The `h2` is what the dialog is NAMED by (Modal's `aria-labelledby`), so a
    // chip inside it renamed the dialog "New task from TASK-003" and put a
    // `<button>` inside a heading — read out by a heading walk, unpressable from
    // one. `titleAside` is the head's other slot, same row, same placement.
    expect(card()).toContain("titleAside: sourceTask.onOpen");
    const MODAL = readFileSync(
      join(import.meta.dir, "..", "platform", "ui", "modal", "Modal.tsx"),
      "utf8",
    );
    expect(MODAL).toContain('<div className="modal-head-title">');
    expect(MODAL).toContain("{titleAside}");
  });
});


// ---- What the folder field's drop offers ------------------------------------
//
// "when I clear the path and search, it should search from projects — the same
// project options I have in the filter beside the New task button" (Akshil,
// 2026-09-19). The field is an address being edited nearly all of the time, and
// then the drop is the folders this card remembers, untouched by typing. Clear
// it, type a word, and it is a search over the very array the toolbar's Project
// filter offers.
//
// `folderFieldRows` is the whole of that decision, and it is a pure function so
// that it can be read here rather than inferred from four conditions inside a
// 5000-line render.
describe("the folder field's two lists", () => {
  const HOME = "/Users/me";
  const RECENTS = [
    "/Users/me/Desktop/fused/fused-render",
    "/Users/me/Desktop/aviary",
  ];
  const PROJECTS = [
    "/Users/me/Desktop/aviary",
    "/Users/me/Desktop/fused/fused-render",
    "/Users/me/Work/lens",
  ];
  const ask = (target: string, extra: Partial<Parameters<typeof folderFieldRows>[0]> = {}) =>
    folderFieldRows({
      target,
      defaultTarget: "/Users/me/Desktop/fused",
      open: true,
      recents: RECENTS,
      projects: PROJECTS,
      home: HOME,
      ...extra,
    });

  test("a bare word searches the page's projects", () => {
    const { rows, searching } = ask("render");
    expect(searching).toBe(true);
    expect(rows.map((r) => r.path)).toEqual(["/Users/me/Desktop/fused/fused-render"]);
    // The NAME leads the row and the WHOLE path follows it — a folder the
    // reader has not typed their way to has to say where it is, in full.
    expect(rows[0]).toEqual({
      path: "/Users/me/Desktop/fused/fused-render",
      name: "fused-render",
      where: "/Users/me/Desktop/fused/fused-render",
    });
  });

  test("the match is on the NAME, like the filter's own", () => {
    // `projectMatches` (tasks-lib, PR #1229): case-folded substring of the
    // basename, never of the path. "desktop" is a segment two of these three
    // share and a name none of them has, so it finds nothing — exactly what
    // the filter beside the New task button answers.
    expect(ask("AVIARY").rows.map((r) => r.name)).toEqual(["aviary"]);
    expect(ask("desktop").rows).toEqual([]);
    expect(ask("work").rows).toEqual([]);
  });

  test("the projects keep their own order, and nothing is capped", () => {
    // Their own order is the one the filter menu prints (tasks-lib
    // `projectOptions`, alphabetical by the NAME it shows). A second ranking
    // here would put the same folders in two orders in two controls.
    expect(ask("r").rows.map((r) => r.path)).toEqual([
      "/Users/me/Desktop/aviary",
      "/Users/me/Desktop/fused/fused-render",
    ]);
    // …and no five-row cap, unlike the recents: the panel scrolls, and a
    // search that hid the eighth answer would be a search you cannot trust.
    const nine = Array.from({ length: 9 }, (_, i) => `/p/repo${i}`);
    expect(ask("repo", { projects: nine }).rows).toHaveLength(9);
  });

  test("a search that finds nothing is still a search", () => {
    // Which is what puts "No project matches" on screen rather than the
    // remembered folders — an empty answer is an answer.
    const { rows, searching } = ask("zzz");
    expect(searching).toBe(true);
    expect(rows).toEqual([]);
  });

  test("text that NAMES A PLACE is an address, and leaves the recents alone", () => {
    // A leading `/`, `~` or a drive letter is the Explorer's own test for "this
    // is an address" (`isPathShapedQuery`). The field opens pre-filled with one,
    // so this is the common case: typing edits an address, and the drop goes on
    // offering what the card remembers.
    for (const typed of ["/Users/me/Desk", "~/Desktop/fu", "C:/proj", "../up"]) {
      const { rows, searching } = ask(typed);
      expect(searching).toBe(false);
      expect(rows.map((r) => r.path)).toEqual(RECENTS);
    }
  });

  test("the default text is nobody having typed anything", () => {
    // The card opens on a path. Answering it with a search would be the form
    // searching for its own default — and it is the TEXT that decides, not a
    // "has been edited" flag, so leaving the field and coming back answers the
    // same way.
    expect(ask("/Users/me/Desktop/fused").searching).toBe(false);
    expect(ask("  /Users/me/Desktop/fused  ").searching).toBe(false);
    expect(ask("").searching).toBe(false);
    expect(ask("   ").searching).toBe(false);
    expect(ask("").rows.map((r) => r.path)).toEqual(RECENTS);
    // …and a bare-word DEFAULT is not a search either, for the same reason.
    expect(ask("scratch", { defaultTarget: "scratch" }).searching).toBe(false);
  });

  test("a shut drop answers nothing", () => {
    expect(ask("render", { open: false }).searching).toBe(false);
  });

  test("the remembered folders are capped, and say where they SIT", () => {
    const many = ["/a/1", "/a/2", "/a/3", "/a/4", "/a/5", "/a/6", "/a/7"];
    const { rows } = ask("", { recents: many });
    expect(rows).toHaveLength(5);
    expect(rows[0]).toEqual({ path: "/a/1", name: "1", where: "/a" });
  });

  test("no projects is simply a search that finds nothing", () => {
    // The app page's scoped card and a deep link both open with no listing
    // behind them; a search there is empty rather than broken.
    const { rows, searching } = ask("render", { projects: [] });
    expect(searching).toBe(true);
    expect(rows).toEqual([]);
  });
});


// ---- Tab, in the folder field ------------------------------------------------
describe("Tab in the folder field", () => {
  const src = () => readFileSync(join(import.meta.dir, "NewJobModal.tsx"), "utf8");

  test("Tab with nothing arrowed to leaves the typed path alone", () => {
    // `completionKeyAction` answers Tab with row 0 when nothing is highlighted
    // (`tabDefaultIndex`), which is right for the Explorer — there row 0
    // completes the segment being typed — and wrong here, where row 0 is a
    // folder from last week: Tab out of a freshly typed path replaced it
    // (review, 2026-09-19). The guard comes BEFORE `preventDefault`, so the key
    // goes on doing what Tab does.
    const s = src();
    const guard = s.indexOf('if (act.type === "tab-accept" && pathAt < 0) return;');
    expect(guard).toBeGreaterThan(-1);
    expect(guard).toBeLessThan(s.indexOf('act.type === "tab-accept" || act.type === "enter-accept"'));
  });

  test("Tab fills the field, Enter answers with it — and nothing else branches", () => {
    // The `stepping` reduction that used to sit here read `row.is_dir` and
    // `row.where`, and with every row a folder both arms came out the same way
    // for Tab; `!row.where` made ENTER on a root-level folder behave like Tab.
    const s = src();
    expect(s).not.toContain("const stepping =");
    expect(s).toContain('if (act.type === "tab-accept") acceptPath(row);');
    // …and the dead icon arm went with the dead flag: every row this list
    // builds is a folder.
    expect(s).not.toContain("p.is_dir ? ICON_FOLDER : ICON_FILE");
  });
});
