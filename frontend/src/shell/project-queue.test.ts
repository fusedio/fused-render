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
import { canRunNext, waitingCardText } from "@platform/lib/queue";

const SHELL = new URL(".", import.meta.url).pathname;
const API = readFileSync(join(SHELL, "../platform/lib/api.ts"), "utf8");
const VIEWS = readFileSync(join(SHELL, "ScheduleTaskViews.tsx"), "utf8");
const PAGE = readFileSync(join(SHELL, "Scheduled.tsx"), "utf8");
const PREFS = readFileSync(join(SHELL, "Preferences.tsx"), "utf8");
const APPS = readFileSync(join(SHELL, "CurrentAppsSection.tsx"), "utf8");
const LIB = readFileSync(join(SHELL, "tasks-lib.ts"), "utf8");
const TASKS_CSS = readFileSync(join(SHELL, "../styles/tasks.css"), "utf8");
const SCHEDULE_CSS = readFileSync(join(SHELL, "../styles/schedule.css"), "utf8");
const SIDEBAR_CSS = readFileSync(join(SHELL, "../styles/sidebar.css"), "utf8");
const TOKENS_CSS = readFileSync(join(SHELL, "../styles/tokens.css"), "utf8");
/** The chat's own half of the queue's skin — an app stylesheet, read from the
 *  shell's suite because the COLOUR is one decision for the whole app and this
 *  is where it is held still. */
const CHAT_CSS = readFileSync(join(SHELL, "../apps/claude/styles/sched.css"), "utf8");
/** The Recent-chats row's stylesheet — same reason as CHAT_CSS: the waiting mark
 *  is one decision and every surface that draws it is held still here. */
const HOME_CSS = readFileSync(join(SHELL, "../apps/claude/styles/home.css"), "utf8");

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
    expect(API).toContain('postJson<SkipResult>("/api/tasks/queue/skip", what)');
    // …and Skip answers with the LINE IT JUST CHANGED as well, the same five
    // `ahead_*` fields admit and run-now carry (🟡 review, 2026-09-12).
    const skip = API.slice(API.indexOf("export interface SkipResult"));
    const shape = skip.slice(0, skip.indexOf("}"));
    for (const field of ["ahead_key?", "ahead?", "ahead_title?", "ahead_session?",
                         "ahead_target?"]) {
      expect(shape).toContain(field);
    }
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

describe("the Board's waiting cards", () => {
  it("draws the card's place in the line, and the ⤒ only at the head", () => {
    expect(CARD).toContain("const queue = queueCaption(task);");
    expect(CARD).toContain('className={"tasks-card-queue" + (queue.runsNext ? " is-next" : "")}');
    expect(CARD).toContain("{queue.runsNext && (");
    expect(CARD).toContain("{QUEUE_PRIORITY_GLYPH}");
    // …and the caption's one actionable token is a LINK into the conversation
    // that is in the way. The same component the List row draws, because it is
    // one sentence (ScheduleTaskViews.QueueCaptionText).
    expect(CARD).toContain("<QueueCaptionText queue={queue} />");
    expect(VIEWS).toContain("export function QueueCaptionText({ queue }: { queue: QueueCaption })");
    expect(VIEWS).toContain("href={queue.aheadHref}");
    // The press must not also fire the card it sits in.
    expect(VIEWS).toContain("onClick={(e) => e.stopPropagation()}");
  });

  it("draws them INSIDE In Progress, under a dashed rule, running first", () => {
    // `queued` had a lane of its own between Upcoming and In Progress for a day.
    // Work that is due, asked for and about to run is work in progress in every
    // sense a person means it — the only thing separating a queued task from a
    // running one is which second its folder frees (schedule-lib.laneOf; the
    // grouping and the counts are tested in tasks-lib.test.ts).
    expect(BOARD).toContain("const splitAt = laneSplitAt(col.key, cards);");
    expect(BOARD).toContain("{ix === splitAt && (");
    expect(BOARD).toContain('<p className="schedule-tv-lane-split" aria-hidden="true">');
    expect(BOARD).toContain("{LANE_SPLIT_LABEL}");
    // The header counts the two halves rather than printing a bare total over a
    // column holding three running tasks and four waiting ones.
    expect(BOARD).toContain("{laneCountLabel(col.key, lane)}");
  });

  it("calls RUN NEXT on the drop, never run-now", () => {
    // The drop lands on the lane the Upcoming drag lands on and must not mean
    // the same thing: firing here would be two runs in one folder.
    expect(BOARD).toContain('if (action.kind === "skip") {');
    expect(BOARD).toContain("onQueued?.(await performSkip(task));");
    // …and the warning under the cursor says so, in the verb's own words rather
    // than borrowing "Run now".
    expect(BOARD).toContain("RUN_NEXT_HINT");
    expect(BOARD).toContain('skip: kind === "skip"');
    // The one wording, from the one place — never a literal in a view. (The
    // file's other "Skip" is the repeat-occurrence verb on a message row, which
    // is a different feature and keeps its own word.)
    expect(VIEWS).not.toContain("Skip the queue");
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
    expect(strip).toContain("RUN_NEXT_DONE_HINT");
  });
});

describe("the List's waiting row", () => {
  it("says where it stands, in the flow — never as a second line", () => {
    // Every row here is one line tall, and one row growing to two would break
    // the rhythm the whole column is scanned down. The card is what grows.
    expect(ROW).toContain('className={"tasks-row-queue" + (queue.runsNext ? " is-next" : "")}');
    expect(ROW).toContain("<QueueCaptionText queue={queue} />");
    expect(css(TASKS_CSS)).toContain(".tasks-row-queue {");
    expect(css(TASKS_CSS)).toContain(".tasks-card-queue {");
  });

  it("grows a Run next only on a waiting row, and not behind the hover-actions flag", () => {
    expect(ROW).toContain('className="tasks-act tasks-act--skip"');
    const at = ROW.indexOf("tasks-act--skip");
    // The guard immediately above it is the queue's, not SHOW_ROW_ACTIONS's.
    expect(ROW.slice(at - 400, at)).toContain("{queue && (");
    expect(ROW).toContain("aria-label={`${RUN_NEXT_LABEL} for ${task.task_id}`}");
    expect(ROW).toContain("void skip();");
    expect(ROW).toContain("onQueued?.(await performSkip(task));");
  });

  it("offers it only when ANOTHER WAITING TASK is ahead — one rule, three surfaces", () => {
    // Every queued task has something in front of it; usually it is the run
    // HOLDING the folder, which this press can never touch. So at position 1 the
    // button's only possible outcome was the state the reader was already in
    // (Akshil, 2026-09-12). `canRunNext` is `queue_position > 1`, and the List
    // row, the Board card and the chat's own card all read that one function.
    expect(canRunNext({ status: "queued", queue_position: 2, queue_ahead: "TASK-056" })).toBe(true);
    expect(canRunNext({ status: "queued", queue_position: 1, queue_ahead: "TASK-056" })).toBe(false);
    for (const src of [ROW, CARD]) {
      expect(src).toContain("{queue && (canRunNext(task) || queue.runsNext) && (");
    }
    // …and `runsNext` still keeps the DISABLED draw, so a press that worked does
    // not take its own control off the row.
    expect(ROW).toContain("disabled={acting || queue.runsNext}");
    expect(CARD).toContain("disabled={busy || queue.runsNext}");
    // THE CAPTION IS NOT GATED ON IT. `1 message waiting · behind TASK-056` is
    // true at the head of the line and stays printed; only the button goes.
    expect(waitingCardText(1, { status: "queued", queue_position: 1, queue_ahead: "TASK-056" })).toBe(
      "1 message waiting · behind TASK-056",
    );
  });

  it("says what each MESSAGE in an expanded thread is doing, in a word", () => {
    // It used to live only in the ring's tooltip, which is to say nowhere a
    // person reading down a thread would find it — and with the queue on, a
    // `running` row and a `queued` row sit one line apart in two strengths of one
    // hue (tasks-lib.messageState carries the four words).
    // …AND NEVER WITH THE FLAG DOWN (`queueOn`, 🔴 review 2026-09-12): this word
    // is derived on the client from "pending and past due", which is true in
    // either build, where every other `queued` on the page comes from a server
    // status that is never written while the queue is off.
    expect(ROW).toContain("const tone = messageState(task, m, queueOn);");
    expect(ROW).toContain("const queueOn = useProjectQueueEnabled();");
    expect(ROW).toContain('<span className={"tasks-msg-state tasks-msg-state--" + tone.column}>');
    expect(ROW).toContain("{tone.word}");
    expect(ROW).toContain("<StatusIcon\n                    status={tone.column}");
    expect(css(TASKS_CSS)).toContain(".tasks-msg-state {");
    expect(css(TASKS_CSS)).toContain(".tasks-msg-state--queued {");
    expect(css(TASKS_CSS)).toContain(".tasks-msg-state--in_progress {");
  });
});

describe("the queue's ink", () => {
  it("is IN PROGRESS'S OWN YELLOW, and `--activity` is gone from every queued surface", () => {
    // Blue was the queue's colour for a day and a gold of its own for a day. The
    // lane fold retired both: `queued` is DRAWN INSIDE In Progress now, so a ring
    // of any other hue sits directly under the running one in the same column and
    // reads as a different kind of work. One state, one colour; the DASH is the
    // difference (below).
    expect(css(SCHEDULE_CSS)).toContain(
      ".schedule-ring--queued {\n  color: var(--status-queued);\n  border-style: dashed;\n}",
    );
    expect(css(TASKS_CSS)).toContain(
      ".tasks-row-queue.is-next,\n.tasks-card-queue.is-next {\n  color: var(--status-queued);\n}",
    );
    // NOT `--activity`, on any surface that names the state. Read off the real
    // rules with the prose stripped, since the comments discuss the retired hue.
    for (const rule of [
      ".schedule-ring--queued",
      ".tasks-row-queue.is-next",
      ".tasks-msg-state--queued",
      ".schedule-tv-lane-split",
    ]) {
      const at = css(TASKS_CSS + SCHEDULE_CSS).indexOf(rule);
      expect(at).toBeGreaterThan(-1);
      expect(css(TASKS_CSS + SCHEDULE_CSS).slice(at, at + 260)).not.toContain("--activity");
    }
    expect(css(CHAT_CSS)).not.toContain("--activity");
    expect(css(CHAT_CSS)).toContain("var(--status-queued)");
  });

  it("spends NO hue of its own — the token is an alias of the running yellow", () => {
    // A third yellow is a colour the reader has to be taught before it says
    // anything, and it loses at ring size against the amber it sits beside
    // (Akshil, 2026-09-12). So `--status-queued` resolves to `--status-progress`
    // in BOTH palettes and the gold literals are gone.
    expect(css(TOKENS_CSS).match(/--status-queued: var\(--status-progress\);/g)).toHaveLength(2);
    expect(TOKENS_CSS).not.toContain("#e9c95a");
    expect(TOKENS_CSS).not.toContain("#9a7b0d");
    // ONCE PER THEME, and never twice in one: test_theme.py requires every dark
    // token to have a light value, and two definitions in one palette is a token
    // whose winner is source order.
    expect(css(TOKENS_CSS).match(/--status-queued:/g)).toHaveLength(2);
    // …and no SURFACE mints one: every one of them still goes through the token,
    // so the alias remains the only place this colour is decided.
    expect(css(SCHEDULE_CSS)).not.toMatch(/schedule-ring--queued[^}]*#[0-9a-f]{3,6}/);
    expect(css(CHAT_CSS)).not.toMatch(/c-waiting[^}]*#[0-9a-f]{3,6}/);
    expect(css(TASKS_CSS)).not.toMatch(/tasks-row-queue[^}]*#[0-9a-f]{3,6}/);
  });

  it("says WAITING with the DASH, on every ring that has one", () => {
    // The shape is what separates the two states now, so it has to be on every
    // mark that wears the hue: the shell's status ring (List, Board, Cards), the
    // Recent-chats dot, and the chat's own waiting card.
    expect(css(SCHEDULE_CSS)).toMatch(/\.schedule-ring--queued \{[^}]*border-style: dashed;/);
    // …and the ring it overrides is the solid one, so the dash is a delta rather
    // than a second ring.
    expect(css(SCHEDULE_CSS)).toMatch(/\.schedule-ring \{[^}]*border: 2px solid currentColor;/);
    expect(css(CHAT_CSS)).toMatch(
      /\.c-waitcard \.wc-ring \{[^}]*border: 2px dashed currentColor;/,
    );
    expect(css(HOME_CSS)).toMatch(
      /\.c-chat-row\.is-waiting \.c-dot::after \{[^}]*border: 2px dashed var\(--status-queued\);/,
    );
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
    // — both of the row's pieces of writing invisible at once.
    const shrink = /flex:\s*0\s+(\d+)\s+auto/.exec(row);
    expect(shrink).not.toBe(null);
    expect(Number(shrink![1])).toBeGreaterThan(1);
    // THE ELLIPSIS LIVES ON THE WORDS, NOT ON THE FLEX BOX AROUND THEM.
    expect(ROW).toContain('<span className="tasks-queue-text">');
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
    // …AND NEITHER DOES THE RUN NEXT BUTTON, which is the queue's other piece of
    // permanent row chrome. `.tasks-act` reserves a 22px box at rest so a row
    // does not reflow under the pointer — right for actions that exist on no row
    // while SHOW_ROW_ACTIONS is down, and wrong for one drawn on every waiting
    // row: at a 400px pane the reservation plus its gap left the title at 2.8px.
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
    // No media query was added for any of it — here or in the chat, whose pane is
    // 340px in a sidebar and 900px in a canvas and knows neither.
    expect(css(TASKS_CSS)).not.toMatch(/@media[^{]*\{[^}]*tasks-(row|card)-queue/);
    expect(css(TASKS_CSS)).not.toMatch(/@media[^{]*\{[^}]*tasks-act--skip/);
    expect(css(CHAT_CSS)).not.toMatch(/@media[^{]*\{[^}]*c-wait/);
    // …and the divider is a border on a growing row rather than a measured rule.
    const split = css(SCHEDULE_CSS).slice(
      css(SCHEDULE_CSS).indexOf(".schedule-tv-lane-split {"),
      css(SCHEDULE_CSS).indexOf("}", css(SCHEDULE_CSS).indexOf(".schedule-tv-lane-split {")),
    );
    expect(split).not.toMatch(/(?<!-)\bwidth:\s*\d/);
  });
});

describe("the Status filter", () => {
  it("offers Queued exactly the way it offers Needs attention — clubbed", () => {
    // The menu offers the LANES THE BOARD DRAWS (Akshil, 2026-09-06: "blocked
    // should be clubbed and needs attention"), so `needs_attention` has never had
    // a tick of its own: one Blocked tick brings the broken run AND the one
    // waiting on you, because that is the column a reader would go looking in.
    //
    // `queued` mirrors it exactly. It is drawn in In Progress, so In Progress is
    // the tick — and turning it on brings the running cards and the waiting ones
    // together, which is the only reading that agrees with the board a press
    // takes the reader back to.
    expect(VIEWS).toContain("const statusColumns = hideArchiveStatus");
    expect(VIEWS).toContain('? BOARD_LANES.filter((c) => c.key !== "archived")\n    : BOARD_LANES;');
    // The MATCH is by lane for the same reason, so a stored `queued` (or
    // `needs_attention`) can never leave a filter applied that no tick shows.
    expect(VIEWS).toContain(
      "const laneOn = (key: BoardColumn) => filters.statuses.some((s) => laneOf(s) === laneOf(key));",
    );
    expect(LIB).toContain("const lane = laneOf(taskColumn(task));");
    expect(LIB).toContain("if (!filters.statuses.some((s) => laneOf(s) === lane)) return false;");
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
  it("says '· 2 waiting' in words, and keeps its one dot for running", () => {
    // The row already spends its one dot slot on running-or-unread, and what a
    // reader wants from a queue is the NUMBER, which a dot cannot say. The WORD
    // is "waiting" — `queued` is the status word, and a count beside "1 running"
    // is a person being told what their machine is doing (tasks-lib.queuedLabel).
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
