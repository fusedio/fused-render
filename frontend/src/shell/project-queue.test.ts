// THE PROJECT QUEUE'S WIRING, on the shell side (prefs `queue.enabled`).
//
// The rules themselves are tested where they live — the caption in
// platform/lib/queue.test.ts, the lane, the drop and the optimistic claim in
// tasks-lib.test.ts. What is left is the wiring, and it is exactly the kind of
// thing a unit test cannot reach and a browser catches too late: which endpoint
// a press calls, which layer holds the claim, and whether a control is drawn at
// all while the one flag that hides every hover action is down.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SHELL = new URL(".", import.meta.url).pathname;
const API = readFileSync(join(SHELL, "../platform/lib/api.ts"), "utf8");
const VIEWS = readFileSync(join(SHELL, "ScheduleTaskViews.tsx"), "utf8");
const PAGE = readFileSync(join(SHELL, "Scheduled.tsx"), "utf8");
const PREFS = readFileSync(join(SHELL, "Preferences.tsx"), "utf8");
const APPS = readFileSync(join(SHELL, "CurrentAppsSection.tsx"), "utf8");
const TASKS_CSS = readFileSync(join(SHELL, "../styles/tasks.css"), "utf8");
const SCHEDULE_CSS = readFileSync(join(SHELL, "../styles/schedule.css"), "utf8");
const SIDEBAR_CSS = readFileSync(join(SHELL, "../styles/sidebar.css"), "utf8");

const CARD = VIEWS.slice(VIEWS.indexOf("function TaskCard("));
const ROW = VIEWS.slice(VIEWS.indexOf("function TaskNode("), VIEWS.indexOf("function TaskCard("));
const BOARD = VIEWS.slice(
  VIEWS.indexOf("export function TaskBoard("),
  VIEWS.indexOf("function TaskCard("),
);
/** A stylesheet with its prose taken out — a rule named only in a comment is not
 *  a rule (the same guard tasks-lib.test.ts uses). */
const css = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, "");

describe("the three endpoints", () => {
  it("are the design's own, with the bodies it names", () => {
    expect(API).toContain('postJson<QueueAdmission>("/api/tasks/queue/admit", body)');
    // SKIP TAKES EITHER NAME, and the body is the CALLER's — a Tasks row holds a
    // task (`{key}`), a chat's chip holds one entry (`{entry_id}`), and only the
    // second survives the rekey a first run performs on a `pending:<id>` task.
    expect(API).toContain(
      'postJson<{ ok: boolean; position: number }>("/api/tasks/queue/skip", what)',
    );
    expect(API).toContain("what: { key: string } | { entry_id: string },");
    // The Board and the List still name the TASK: the press there means every
    // pending entry that task has waiting.
    expect(VIEWS).toContain("await skipQueue({ key: task.key });");
    expect(API).toContain('postJson<QueueDecision>("/api/tasks/queue/decide", body)');
  });

  it("makes a queued run-now an ANSWER, not a rejection", () => {
    // `ok: false, reason: "queued"` means the folder was busy: the entry stays
    // pending, gains priority, and the row reads Queued at #1. Nothing was lost,
    // so nothing may be thrown — a caller that raised it would show the reader
    // an error for a message that is safely in the line.
    expect(API).toContain("export interface RunNowResult");
    expect(API).toContain("reason?: string;");
    expect(VIEWS).toContain('if (res.ok === false && res.reason === "queued")');
  });

  it("carries the queue's row fields, all optional, so an older server still renders", () => {
    // Every reader treats a missing field as "not queued", which is why there is
    // no third state to draw: `status` is the authority, these only say where.
    for (const field of [
      "queue_key?: string;",
      "queue_position?: number;",
      "queue_ahead?: string;",
      "queue_ahead_title?: string;",
      "queue_priority?: boolean;",
    ]) {
      expect(API).toContain(field);
    }
    // …and the status union gained the word, between the two it sits between.
    expect(API).toContain('status: "upcoming" | "queued" | "in_progress"');
  });

  it("reads the pref beside the chat's, and writes it by its own name", () => {
    expect(API).toContain("queue?: { enabled: boolean };");
    expect(API).toContain('putJson<Prefs>("/api/prefs", { project_queue_enabled: enabled })');
  });
});

describe("the Preferences switch", () => {
  it("is offered, is opt-in, and publishes so an open composer follows", () => {
    expect(PREFS).toContain("function ProjectQueueSection(");
    expect(PREFS).toContain("<ProjectQueueSection prefs={prefs} onChange={setPrefs} />");
    // `=== true`: a server that predates the field has no queue.
    expect(PREFS).toContain("prefs.queue?.enabled === true");
    expect(PREFS).toContain("await putProjectQueueEnabled(!enabled)");
    // The chat's send path reads the flag from a module cache that is otherwise
    // only refreshed by a mount; without this, a composer already on screen
    // would keep admitting (or not) by the old answer until a navigation.
    expect(PREFS).toContain("publishProjectQueueEnabled(next.queue?.enabled === true)");
  });

  it("says what it does in the words the page uses, not in the queue's internals", () => {
    // "folder", "Queued", "front of the line" — never "lease" and never "holder".
    const section = PREFS.slice(
      PREFS.indexOf("function ProjectQueueSection("),
      PREFS.indexOf("// Local-network sharing"),
    );
    expect(section).toContain("one task at a time per folder");
    expect(section).not.toContain("lease");
    expect(section).not.toContain("holder");
  });
});

describe("the Board's Queued lane", () => {
  it("draws the card's place in the line, and the ⤒ only at the head", () => {
    expect(CARD).toContain("const queue = queueLine(task);");
    expect(CARD).toContain('className={"tasks-card-queue" + (queue.runsNext ? " is-next" : "")}');
    expect(CARD).toContain("{queue.runsNext && (");
    expect(CARD).toContain("{QUEUE_PRIORITY_GLYPH}");
  });

  it("calls SKIP on the drop, never run-now", () => {
    // The drop lands on the lane the Upcoming drag lands on and must not mean
    // the same thing: firing here would be two runs in one folder.
    expect(BOARD).toContain('if (action.kind === "skip") {');
    expect(BOARD).toContain("onQueued?.(await performSkip(task));");
    // …and the warning under the cursor says so, rather than borrowing "Run now".
    expect(BOARD).toContain("Skip the queue — it runs next, nothing is interrupted");
    expect(BOARD).toContain('skip: kind === "skip"');
  });

  it("offers the same verb as a button, unguarded by the hover-actions flag", () => {
    // Archive's precedent: while SHOW_ROW_ACTIONS is down this would otherwise be
    // the Board's only route to the front of a line other than dragging a card
    // out of a lane that is rolled up whenever it is empty.
    expect(CARD).toContain('className="tasks-act tasks-card-act tasks-act--skip"');
    const strip = css(CARD).slice(css(CARD).indexOf('<span className="tasks-card-acts">'));
    const at = strip.indexOf("tasks-act--skip");
    expect(at).toBeGreaterThan(0);
    // Its own guard is `{queue && (`, with no flag in front of it — read off the
    // source with the prose stripped, since the comment above it NAMES the flag.
    expect(strip.slice(0, at)).not.toContain("SHOW_ROW_ACTIONS");
    expect(strip.slice(0, at)).toContain("{queue && (");
    // Drawn and disabled at the head, not dropped: taking a control away on the
    // press that worked is how a reader ends up unsure anything happened.
    expect(strip).toContain("disabled={busy || queue.runsNext}");
  });
});

describe("the List's queued row", () => {
  it("says where it stands, in the flow — never as a second line", () => {
    // Every row here is one line tall, and one row growing to two would break
    // the rhythm the whole column is scanned down. The card is what grows.
    expect(ROW).toContain('className={"tasks-row-queue" + (queue.runsNext ? " is-next" : "")}');
    expect(css(TASKS_CSS)).toContain(".tasks-row-queue {");
    expect(css(TASKS_CSS)).toContain(".tasks-card-queue {");
  });

  it("grows a Skip only on a queued row, and not behind the hover-actions flag", () => {
    expect(ROW).toContain('className="tasks-act tasks-act--skip"');
    const at = ROW.indexOf("tasks-act--skip");
    // The guard immediately above it is the queue's, not SHOW_ROW_ACTIONS's.
    expect(ROW.slice(at - 400, at)).toContain("{queue && (");
    expect(ROW).toContain("aria-label={`Skip the queue for ${task.task_id}`}");
    expect(ROW).toContain("void skip();");
    expect(ROW).toContain("onQueued?.(await performSkip(task));");
  });
});

describe("the queue's ink", () => {
  it("wears the page's own blue on every surface that names the state", () => {
    // `--activity` stopped being a status hue in 2026-08 and became "a thing
    // about to happen, or that you can make happen" — which is exactly what a
    // queued task is. One state, one colour, on the ring, the row and the card.
    expect(SCHEDULE_CSS).toContain(".schedule-ring--queued { color: var(--activity); }");
    expect(css(TASKS_CSS)).toContain(
      ".tasks-row-queue.is-next,\n.tasks-card-queue.is-next {\n  color: var(--activity);\n}",
    );
    // …and no hex was minted for it.
    expect(css(SCHEDULE_CSS)).not.toMatch(/schedule-ring--queued[^}]*#[0-9a-f]{3,6}/);
  });

  it("gives the caption no width and no breakpoint — it measures, it does not guess", () => {
    // The sentence's length is the DATA's: an id can be TASK-7 or TASK-1041, and
    // a folder can hold a dozen waiting tasks. So each surface is told how to
    // give way — the row shrinks and ellipsises, the card wraps — and neither is
    // told a size or asked how wide the viewport is.
    const row = css(TASKS_CSS).slice(
      css(TASKS_CSS).indexOf(".tasks-row-queue {"),
      css(TASKS_CSS).indexOf("}", css(TASKS_CSS).indexOf(".tasks-row-queue {")),
    );
    expect(row).toContain("min-width: 0");
    expect(row).toContain("overflow: hidden");
    expect(row).not.toMatch(/(?<!-)\bwidth:\s*\d/);
    // AND IT GIVES WAY BEFORE THE TITLE DOES, by a shrink RATIO rather than by
    // a size (browser QA round 2). `flex: 0 1 auto` beside a title that also
    // shrinks by 1 splits a shortfall in proportion to each item's base width,
    // which at a 400px pane left the 150px caption AND the 72px title at ~1px
    // — both of the row's pieces of writing invisible at once. The caption has
    // to absorb the whole shortfall and reach zero before the title gives a
    // pixel, and only a much larger factor says that.
    const shrink = /flex:\s*0\s+(\d+)\s+auto/.exec(row);
    expect(shrink).not.toBe(null);
    expect(Number(shrink![1])).toBeGreaterThan(1);
    // THE ELLIPSIS LIVES ON THE WORDS, NOT ON THE FLEX BOX AROUND THEM. The
    // caption is `inline-flex` (the ⤒ sits beside the sentence) and
    // `text-overflow` never reaches a flex item, so the declaration was inert
    // where it shipped and the sentence clipped mid-glyph. The inner span is
    // the block-with-inline-content the property needs.
    expect(ROW).toContain('<span className="tasks-queue-text">{queue.text}</span>');
    const words = css(TASKS_CSS).slice(
      css(TASKS_CSS).indexOf(".tasks-queue-text {"),
      css(TASKS_CSS).indexOf("}", css(TASKS_CSS).indexOf(".tasks-queue-text {")),
    );
    expect(words).toContain("min-width: 0");
    expect(words).toContain("overflow: hidden");
    expect(words).toContain("text-overflow: ellipsis");
    expect(words).not.toMatch(/(?<!-)\bwidth:\s*\d/);
    const card = css(TASKS_CSS).slice(
      css(TASKS_CSS).indexOf(".tasks-card-queue {"),
      css(TASKS_CSS).indexOf("}", css(TASKS_CSS).indexOf(".tasks-card-queue {")),
    );
    expect(card).toContain("overflow-wrap: anywhere");
    expect(card).not.toMatch(/(?<!-)\bwidth:\s*\d/);
    // …AND NEITHER DOES THE SKIP BUTTON, which is the queue's other piece of
    // permanent row chrome. `.tasks-act` reserves a 22px box at rest so a row
    // does not reflow under the pointer — right for actions that exist on no
    // row while SHOW_ROW_ACTIONS is down, and wrong for one drawn on every
    // queued row: at a 400px pane the reservation plus its gap left the title
    // at 2.8px. On a LIST ROW it shrinks away with the caption, before the
    // title gives a pixel, and comes back the moment a keyboard reaches it.
    const skip = css(TASKS_CSS).slice(
      css(TASKS_CSS).indexOf(".tasks-row .tasks-act--skip {"),
      css(TASKS_CSS).indexOf("}", css(TASKS_CSS).indexOf(".tasks-row .tasks-act--skip {")),
    );
    expect(skip).toContain("min-width: 0");
    expect(skip).toContain("overflow: hidden");
    expect(/flex-shrink:\s*(\d+)/.exec(skip)).not.toBe(null);
    expect(Number(/flex-shrink:\s*(\d+)/.exec(skip)![1])).toBeGreaterThan(1);
    expect(skip).not.toMatch(/(?<!-)\bwidth:\s*\d/);
    expect(css(TASKS_CSS)).toContain(".tasks-row .tasks-act--skip:focus-visible {");
    // No media query was added for any of it.
    expect(css(TASKS_CSS)).not.toMatch(/@media[^{]*\{[^}]*tasks-(row|card)-queue/);
    expect(css(TASKS_CSS)).not.toMatch(/@media[^{]*\{[^}]*tasks-act--skip/);
  });
});

describe("the page holds the optimistic claims, not the view", () => {
  it("keeps them above the views, so a navigation cannot undo them", () => {
    // A claim exists to outrun the poll, and a board or a list is remounted by
    // every navigation — a store inside one would be undone by the very answer
    // it was written to beat.
    expect(PAGE).toContain("const [queueOverrides, setQueueOverrides] = useState<QueueOverrides>");
    expect(PAGE).toContain("onQueued={noteQueued}");
    expect(PAGE).toContain("applyQueueOverrides(tasks, queueOverrides)");
  });

  it("retires a claim on the server's own answer, full listing or delta", () => {
    expect(PAGE).toContain("expireQueueOverrides(cur, (r.tasks ?? []).map((t) => t.key))");
    expect(PAGE).toContain("const spoken = [...rows.map((t) => t.key), ...gone];");
    expect(PAGE).toContain("expireQueueOverrides(before, spoken)");
  });

  it("paints BEFORE the scope and the filters, so a moved row is filtered as moved", () => {
    expect(PAGE.indexOf("applyQueueOverrides")).toBeLessThan(PAGE.indexOf("const inScope"));
    expect(PAGE).toContain("scope ? painted.filter(");
    // The SIDEBAR gets the unpainted rows: a claim is this page's optimism about
    // a press made on this page, and the rail is not the place to carry it.
    expect(PAGE).toContain("publishTasks(r.tasks ?? []);");
    expect(PAGE).not.toContain("publishTasks(painted");
  });
});

describe("the sidebar", () => {
  it("says '· 2 queued' in words, and keeps its one dot for running", () => {
    // The row already spends its one dot slot on running-or-unread, and what a
    // reader wants from a queue is the NUMBER, which a dot cannot say.
    expect(APPS).toContain('<span className="current-app-queued">{"· " + queuedLabel(app.queued)}</span>');
    expect(APPS).toContain("{app.queued > 0 && (");
    expect(APPS).toContain("rows.filter((r) => isQueued(r)).map((r) => r.project");
    expect(css(SIDEBAR_CSS)).toContain(".current-app-queued {");
    // …and it gives way before the app's NAME does — identity outranks state.
    const rule = css(SIDEBAR_CSS).slice(
      css(SIDEBAR_CSS).indexOf(".current-app-queued {"),
      css(SIDEBAR_CSS).indexOf("}", css(SIDEBAR_CSS).indexOf(".current-app-queued {")),
    );
    expect(rule).toContain("min-width: 0");
    expect(rule).toContain("text-overflow: ellipsis");
  });
});
