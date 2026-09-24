// Running a stored draft from the Board (draft-run.ts, design.md §4): which
// drafts the drag will lift, and exactly what a drop puts on the wire.
//
// Imported DYNAMICALLY after a handful of browser globals are stubbed, for the
// same reason new-task-form.test.ts is: the module reaches into NewJobModal.tsx
// (deliberately — the payload has one builder) and importing that pulls in the
// router, which reads `location` at module init. Nothing below renders
// anything; `draftRunPayload` is pure and takes its own `now`.
import { beforeAll, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Task } from "@platform/lib/api";
import { installDomShim } from "@platform/lib/testDomShim";

const g = globalThis as unknown as Record<string, unknown>;
g.location = { pathname: "/tasks", search: "", hash: "", href: "http://x/tasks", origin: "http://x" };
g.history = { replaceState() {}, pushState() {}, state: null };
g.window = globalThis;
// `installDomShim()`'s idempotent (`??=`) `document` stub, not a competing
// ad-hoc one: an unconditional `g.document = {...}` here pre-empted whichever
// OTHER file's shim would have run first, leaving `document` without
// `activeElement` for the rest of the process — see JobPopupCard.test.tsx's
// "an iframe taking focus..." test, which narrows on exactly that member.
installDomShim();

let canRunDraft: typeof import("./draft-run").canRunDraft;
let draftRunPayload: typeof import("./draft-run").draftRunPayload;
let boundDraftPayload: typeof import("./draft-run").boundDraftPayload;
let chatDraftPayload: typeof import("./draft-run").chatDraftPayload;

beforeAll(async () => {
  const mod = await import("./draft-run");
  canRunDraft = mod.canRunDraft;
  draftRunPayload = mod.draftRunPayload;
  boundDraftPayload = mod.boundDraftPayload;
  chatDraftPayload = mod.chatDraftPayload;
});

// A draft row exactly as `/api/tasks` emits one: the client-minted id, and the
// form the card last autosaved — the key names are drafts.TaskDraftForm's, not
// the payload's, which is the translation this module exists to do.
const FORM = {
  title: "Update the changelog",
  description: "Pull the merged PRs since the last tag.",
  target: "/Users/a/proj",
  when: null,
  repeat: null,
  model: "",
  effort: "",
  permission: "",
  attachments: [],
  new_task_each_run: null,
  session_id: "",
  custom_rule: null,
};

function draft(form: Record<string, unknown> | undefined = FORM,
               over: Partial<Task> = {}): Task {
  return {
    key: "draft:d1",
    task_id: "TASK-007",
    kind: "draft",
    draft_kind: "task",
    draft_id: "d1",
    form,
    ...over,
  } as unknown as Task;
}

// 12:34:56 local — the seconds matter: `when` is a minute-precision field, so
// the payload must land on 12:34 whatever second the drop happened in.
const NOW = new Date(2026, 8, 14, 12, 34, 56);

describe("which drafts the drag lifts", () => {
  test("a finished task draft does", () => {
    expect(canRunDraft(draft())).toBe(true);
  });

  test("a chat draft never does — there is no form to run", () => {
    expect(canRunDraft(draft(undefined, { draft_kind: "chat", form: undefined }))).toBe(false);
  });

  test("a session row is not a draft row at all", () => {
    // A Done row wearing a bound form draft is a real task (design.md §4): its
    // draft is a message into that chat and runs from the chat, so the row is
    // `kind: "task"` and the drag leaves it where it is.
    expect(canRunDraft(draft(FORM, { kind: "task" }))).toBe(false);
  });

  test("the card's own Save gate decides the rest", () => {
    // No instruction — the title IS what Claude is sent (composeTaskMessage),
    // so a draft without one has nothing to run…
    expect(canRunDraft(draft({ ...FORM, title: "" }))).toBe(false);
    // …and nowhere to run it.
    expect(canRunDraft(draft({ ...FORM, target: "" }))).toBe(false);
    // A custom repeat pointing at no rule is the third thing Save refuses.
    expect(canRunDraft(draft({ ...FORM, repeat: "custom" }))).toBe(false);
    expect(canRunDraft(draft({
      ...FORM, repeat: "custom", custom_rule: { freq: "day" },
    }))).toBe(true);
    // The DESCRIPTION is optional on the card, so it is optional here.
    expect(canRunDraft(draft({ ...FORM, description: "" }))).toBe(true);
  });

  test("a form written by another build costs the field, not the drop", () => {
    // Loose JSON off disk: every field is read with its own type check
    // (seededDraftForm), so junk in one of them must never throw.
    expect(canRunDraft(draft({ ...FORM, attachments: "nope", custom_rule: 7 }))).toBe(true);
    expect(canRunDraft(draft({}))).toBe(false);
  });
});

describe("what a drop puts on the wire", () => {
  test("the stored form, due now, with the draft named so the server drops it", () => {
    const body = draftRunPayload(draft(), NOW)!;
    expect(body).not.toBeNull();
    expect(body.target).toBe("/Users/a/proj");
    // Title and description as ONE message, exactly as Save composes them.
    expect(body.message).toContain("Update the changelog");
    expect(body.message).toContain("Pull the merged PRs since the last tag.");
    expect(body.title).toBe("Update the changelog");
    expect(body.description).toBe("Pull the merged PRs since the last tag.");
    // Now, to the minute the field would have held.
    expect(body.due).toBe("2026-09-14T12:34");
    // Nobody picked that minute — the drag did. That is what keeps the task off
    // the calendar (design: a plan, not a log).
    expect(body.immediate).toBe(true);
    // The draft ends in the same request that creates the task.
    expect(body.draft_id).toBe("d1");
    // Nothing was replaced: a draft is not an entry.
    expect(body.replaces).toBeUndefined();
  });

  test("what the draft never said is left off the wire entirely", () => {
    const body = draftRunPayload(draft(), NOW)!;
    expect(body.model).toBeUndefined();
    expect(body.effort).toBeUndefined();
    expect(body.session_id).toBeUndefined();
    expect(body.images).toBeUndefined();
    expect(body.attachments).toBeUndefined();
    expect(body.repeats).toBeUndefined();
    expect(body.rule).toBeUndefined();
  });

  test("a draft bound to a conversation still lands in it", () => {
    // The draft restates its session on every save precisely so a card nobody
    // has open still knows where the task is going.
    const body = draftRunPayload(draft({ ...FORM, session_id: "sess-9" }), NOW)!;
    expect(body.session_id).toBe("sess-9");
  });

  test("nothing names a second draft to drop", () => {
    // `from_chat_key` is gone with the design that needed it (design "one
    // record", §1): a task draft is never made out of a chat draft any more, so
    // there is no second record for a drop to have to clean up behind it. A
    // stored record that still carries the old field is simply dropped on read.
    const body = draftRunPayload(draft({ ...FORM, from_chat_key: "new:/a/x.html" }), NOW)!;
    expect("from_chat_key" in body).toBe(false);
    expect("draft_key" in body).toBe(false);
  });

  test("a repeat survives the drop, anchored on now", () => {
    const body = draftRunPayload(draft({ ...FORM, repeat: "daily" }), NOW)!;
    expect(body.rule).toEqual({ freq: "day" });
    expect(body.due).toBe("2026-09-14T12:34");
    // `immediate` is a one-off's flag — the server refuses the pairing, and a
    // repeat's anchor is a time somebody chose by definition.
    expect(body.immediate).toBeUndefined();
  });

  test("a weekly preset means the day the task is being sent for", () => {
    // The presets are built around an anchor, and the anchor here is `now`:
    // 2026-09-14 is a Monday.
    const body = draftRunPayload(draft({ ...FORM, repeat: "weekly" }), NOW)!;
    expect(body.rule).toEqual({ freq: "week", byday: [1] });
  });

  test("the attachments ride in both fields, as the card sends them", () => {
    const body = draftRunPayload(draft({
      ...FORM,
      attachments: [{ path: "/shots/1.png", name: "shot.png", kind: "image" }],
    }), NOW)!;
    expect(body.images).toEqual(["/shots/1.png"]);
    expect(body.attachments).toEqual(
      [{ path: "/shots/1.png", name: "shot.png", kind: "image" }]);
  });

  test("a draft the drag would not lift builds no body at all", () => {
    expect(draftRunPayload(draft({ ...FORM, title: "" }), NOW)).toBeNull();
  });
});

// ---- the DONE row's draft (Akshil, 2026-09-14) --------------------------------
// "if I have a done task that has draft and I move it to In Progress it should
// run, why not?" The row is a real task; what the drop sends is the unsent thing
// sitting on it, which is either a bound New task form or composer words.

describe("a bound form, dropped from the row that wears its chip", () => {
  test("is the same translation, given the form on its own", () => {
    // It arrives from `GET /api/drafts` keyed by the id the row names
    // (`Task.bound_draft`), not as a row: a bound draft has none.
    const body = boundDraftPayload("d9", { ...FORM, session_id: "sess-9" }, NOW)!;
    expect(body).not.toBeNull();
    expect(body.message).toContain("Update the changelog");
    expect(body.due).toBe("2026-09-14T12:34");
    expect(body.immediate).toBe(true);
    // Where it is going — the conversation the card was written for…
    expect(body.session_id).toBe("sess-9");
    // …and the draft ends in the same request that sends it.
    expect(body.draft_id).toBe("d9");
  });

  test("still refuses a form the card itself would refuse", () => {
    expect(boundDraftPayload("d9", { ...FORM, title: "" }, NOW)).toBeNull();
    expect(boundDraftPayload("d9", null, NOW)).toBeNull();
  });
});

describe("composer words, sent into their own conversation", () => {
  const chat = (over: Record<string, unknown> = {}) => ({
    sessionId: "sess-9",
    target: "/Users/a/proj",
    text: "and one more thing — check the logs",
    ...over,
  });

  test("the words go verbatim, due now, into the session", () => {
    const body = chatDraftPayload(chat(), NOW)!;
    expect(body).not.toBeNull();
    // The user typed a MESSAGE, not a form: nothing is split, joined or
    // re-ordered on the way out.
    expect(body.message).toBe("and one more thing — check the logs");
    expect(body.session_id).toBe("sess-9");
    expect(body.target).toBe("/Users/a/proj");
    expect(body.due).toBe("2026-09-14T12:34");
    expect(body.immediate).toBe(true);
  });

  test("names nothing and describes nothing — the task already has both", () => {
    const body = chatDraftPayload(chat(), NOW)!;
    // A title names a TASK, and this one is named. Left off, the server keeps
    // the name the row is showing.
    expect(body.title).toBeUndefined();
    // And a description would be worse: the row reads its description off the
    // LAST entry that has one (routers/tasks.py `_description`), so sending
    // these words as one would rewrite what the task says it is about.
    expect(body.description).toBeUndefined();
    // Nothing is being repeated, replaced or forked either.
    expect(body.rule).toBeUndefined();
    expect(body.repeats).toBeUndefined();
    expect(body.replaces).toBeUndefined();
    expect(body.new_task_each_run).toBeUndefined();
  });

  test("no separate delete: `session_id` is what clears the chip", () => {
    const body = chatDraftPayload(chat(), NOW)!;
    // POST /api/schedule drops the chat draft filed under the session it is
    // scheduling into, so the chip goes in the same request the words do —
    // there is no `draft_id` here and no second call to half-fail.
    expect(body.draft_id).toBeUndefined();
    expect("draft_key" in body).toBe(false);
    const src = readFileSync(join(import.meta.dir, "draft-run.ts"), "utf8");
    expect(src).not.toContain("deleteChatDraft");
  });

  test("attachments ride in both fields, as an entry stores them", () => {
    const body = chatDraftPayload(chat({
      attachments: [{ path: "/shots/1.png", name: "log.png", kind: "image" }],
    }), NOW)!;
    expect(body.images).toEqual(["/shots/1.png"]);
    expect(body.attachments).toEqual(
      [{ path: "/shots/1.png", name: "log.png", kind: "image" }]);
  });

  test("builds nothing without words or without a conversation", () => {
    // A composer holding only files is a half-made thought: the server has
    // nothing to hand Claude.
    expect(chatDraftPayload(chat({ text: "   " }), NOW)).toBeNull();
    expect(chatDraftPayload(chat({ sessionId: "" }), NOW)).toBeNull();
  });

  test("the tempdir paths never travel as they are", () => {
    // A composer attachment lives in the claude template's own shots dir, and
    // POST /api/schedule refuses any path outside `schedule.shots_dir()` — so
    // the BYTES move, through the hop the composer's Schedule button uses.
    const src = readFileSync(join(import.meta.dir, "draft-run.ts"), "utf8");
    expect(src).toContain("copyToTaskShots");
  });
});

describe("there is one payload builder", () => {
  test("this module translates the form and builds nothing", () => {
    // The rule that keeps a drop and a Save agreeing: what POST /api/schedule
    // is owed is answered in NewJobModal.buildSchedulePayload, once.
    const src = readFileSync(join(import.meta.dir, "draft-run.ts"), "utf8");
    expect(src).toContain("buildSchedulePayload({");
    // No second opinion about the wire's own key names.
    expect(src).not.toContain("permission_mode");
    expect(src).not.toContain("new_task_each_run:");
    expect(src).not.toContain("delay_seconds");
  });

  test("the draft's real body is read back, never taken off the row", () => {
    // The listing carries a PREVIEW — one clipped line for a chip — so a drop
    // that sent `task.draft.preview` would silently truncate the user's own
    // sentence. Both branches fetch first.
    const src = readFileSync(join(import.meta.dir, "draft-run.ts"), "utf8");
    expect(src).not.toContain("draft.preview");
    expect(src).toContain("await fetchDrafts()");
    expect(src).toContain("await fetchChatDraft(sessionId)");
  });
});
