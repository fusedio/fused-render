// THE HEADER'S QUEUED STATE — the dashed ring and the sentence the Tasks row
// has always worn, in the pane that is actually waiting.
//
// The bug (Akshil, 2026-09-17): a send into a busy folder drew the dashed bubble
// and the "1 message waiting" pill, and the header above them — `Claude · alpha
// · T048` — said nothing at all. A chat waiting behind somebody else's run
// looked exactly like an idle one, while the Tasks list three pixels away showed
// the state plainly.
//
// …AND THE FIX'S OWN BUG (Akshil, 2026-09-18): the first cut hung the ring and
// the words off the RIGHT end of the line, in the usage limit's red seat, after
// the task number — so one state read as two different objects on two surfaces
// three pixels apart, and waiting read as failing. The header is now the Tasks
// ROW'S order, which is what this file asserts: ring, TASK-nnn, title, caption,
// left to right, and the project at the far end.
//
// WHAT IS ASSERTED is only what could drift: the ORDER, that the words are
// `queueCaption`'s (never a second wording minted here), that the ring is the
// LIST's ring class (`schedule-ring--queued`) and the caption the LIST's caption
// classes (`tasks-row-queue` / `tasks-queue-text`), that the usage limit's red
// span is not borrowed for it, and that a row which is not queued draws none of
// it.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { Task } from "@platform/lib/api";
import type { QueueFacts } from "@platform/lib/queue";

const { Topbar } = await import("./Topbar");

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

const task = (over: Partial<Task> = {}): Task =>
  ({
    key: "sess-1",
    task_id: "TASK-048",
    title: "say hi",
    project: "/repo/alpha",
    target: "/repo/alpha/x.py",
    session_id: "sess-1",
    status: "queued",
    ...over,
  }) as Task;

function render(queue: QueueFacts | null, over: Partial<Parameters<typeof Topbar>[0]> = {}) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(
      createElement(Topbar, {
        sessionId: "",
        subtitle: "alpha",
        taskId: "TASK-048",
        running: false,
        queue,
        ...over,
      }),
    );
  });
  mounted.push(r);
  return r;
}

/** Every string the tree prints, joined — the header is one line and reads as
 *  one sentence, so that is how it is asserted. */
function text(r: ReactTestRenderer): string {
  const out: string[] = [];
  const walk = (node: unknown): void => {
    if (typeof node === "string") {
      out.push(node);
      return;
    }
    if (Array.isArray(node)) {
      for (const n of node) walk(n);
      return;
    }
    if (node && typeof node === "object" && "children" in node) {
      walk((node as { children: unknown }).children);
    }
  };
  walk(r.toJSON() as unknown);
  return out.join("");
}

function classes(r: ReactTestRenderer): string[] {
  return r.root
    .findAll((n) => typeof n.type === "string" && typeof n.props.className === "string")
    .map((n) => String(n.props.className));
}

function ringClasses(r: ReactTestRenderer): string[] {
  return classes(r).filter((c) => c.includes("schedule-ring"));
}

function has(r: ReactTestRenderer, cls: string): boolean {
  return classes(r).some((c) => c.split(" ").includes(cls));
}

/** WHERE A CLASS SITS IN THE PAINTED ORDER. The serialised tree is depth-first
 *  in document order, so "the ring is before the number" is literally an index
 *  comparison — and that is the whole complaint this file was reopened for. */
function at(r: ReactTestRenderer, marker: string): number {
  const said = JSON.stringify(r.toJSON());
  const i = said.indexOf(marker);
  expect(i).toBeGreaterThan(-1);
  return i;
}

test("a queued chat reads in the Tasks row's order: ring, number, title, caption", async () => {
  const r = render(
    { status: "queued", queue_position: 1, queue_ahead: "TASK-046" },
    { task: task() },
  );
  // The ring is the LIST's ring and it is drawn by the row's own status, inside
  // the peek's identity block — not bolted on beside it.
  expect(ringClasses(r).some((c) => c.includes("schedule-ring--queued"))).toBe(true);
  expect(at(r, "schedule-ring--queued")).toBeLessThan(at(r, "task-side-peek-id"));
  expect(at(r, "task-side-peek-id")).toBeLessThan(at(r, "task-side-peek-title"));
  // …and the sentence TRAILS THE TITLE, in the Tasks row's own two spans.
  expect(at(r, "task-side-peek-title")).toBeLessThan(at(r, "tasks-row-queue"));
  expect(has(r, "tasks-queue-text")).toBe(true);
  // The project keeps the far end of the line.
  expect(at(r, "tasks-row-queue")).toBeLessThan(at(r, "task-side-peek-project"));
  // The words are the queue's one builder's, and the header adds none of its own.
  expect(text(r)).toContain("after TASK-046 | 1st");
  // NO ⤒ AND NO SECOND COLOUR, for the reason the row and the card dropped them
  // (Akshil, 2026-09-19): a skip changes the ORDER, and the order is the words.
  expect(has(r, "tasks-queue-glyph")).toBe(false);
  expect(has(r, "is-next")).toBe(false);
  // AND NOT THE USAGE LIMIT'S RED SEAT (`.c-tb-paused`), which was the first
  // cut's mistake: that span is Blocked's red, and waiting is not failing.
  expect(has(r, "c-tb-paused")).toBe(false);
  // The header sits on one line: the caption is a `tasks-row-queue` span in the
  // same flex row, and the row is told so (`.c-topbar.is-queued`, composer.css).
  expect(has(r, "is-queued")).toBe(true);
});

test("the id in `after TASK-x` is a DOOR, exactly as the Tasks row's is", async () => {
  // The header said the same words as the Tasks row and gave the reader nothing
  // to press (Akshil, 2026-09-18). The one question a person has about the thing
  // in their way is what it is doing, and the answer is the holder's own
  // conversation — so the id is an `<a>` wherever the row carries the pair the
  // link is built from (`queue_ahead_session` + `queue_ahead_target`,
  // platform/lib/queue.queueAheadHref).
  const r = render(
    {
      status: "queued",
      queue_position: 2,
      queue_ahead: "TASK-046",
      queue_ahead_title: "Nightly deploy",
      queue_ahead_session: "sess-46",
      queue_ahead_target: "/repo/alpha/deploy.py",
    },
    { task: task() },
  );
  const link = r.root.findAll(
    (n) => n.type === "a" && String(n.props.className || "").includes("tasks-queue-ahead"),
  );
  expect(link).toHaveLength(1);
  expect(String(link[0].props.href)).toContain("session_id=sess-46");
  expect(String(link[0].props.href)).toContain("deploy.py");
  expect(link[0].props.title).toBe("Nightly deploy");
  expect(text(r)).toContain("after TASK-046 | 2nd");
});

test("…and it stays plain text when there is nowhere for it to go", async () => {
  // An older server sends neither field, and a link to nothing is worse than a
  // name: the id keeps its words and loses its underline.
  const r = render(
    { status: "queued", queue_position: 2, queue_ahead: "TASK-046" },
    { task: task() },
  );
  expect(r.root.findAll((n) => n.type === "a")).toHaveLength(0);
  expect(text(r)).toContain("after TASK-046");
});

test("a queued chat with no task row still reads ring, Claude, caption", async () => {
  const r = render({ status: "queued", queue_position: 1, queue_ahead: "TASK-046" });
  expect(ringClasses(r).some((c) => c.includes("schedule-ring--queued"))).toBe(true);
  expect(at(r, "schedule-ring--queued")).toBeLessThan(at(r, "c-tb-title"));
  expect(at(r, "c-tb-title")).toBeLessThan(at(r, "tasks-row-queue"));
  expect(text(r)).toContain("Claude");
  expect(text(r)).toContain("after TASK-046 | 1st");
  expect(has(r, "c-tb-paused")).toBe(false);
  // The ring takes the ✻'s seat rather than sitting beside it — one glyph at the
  // left end of the line, and on a waiting chat it is the waiting one.
  expect(has(r, "c-spark")).toBe(false);
});

test("a queued chat nobody has answered for beats the skeleton", async () => {
  // `pending` is "the listing has not answered yet", and a brand-new send keyed
  // `pending:<entry id>` is exactly the chat this state matters most on: a
  // placeholder over a KNOWN state would hide the only answer the header has.
  const r = render({ status: "queued", queue_position: 2 }, { pending: true });
  expect(has(r, "c-tb-skel")).toBe(false);
  expect(text(r)).toContain("2nd");
});

test("the placeless answer is the status word, not a half-sentence", async () => {
  // The server could not place it — an honest answer, and NOT the head. It reads
  // "queued" rather than "0th" or the old "in line" (platform/lib/queue).
  expect(text(render({ status: "queued" }))).toContain("queued");
});

test("a running row draws no queued state at all", async () => {
  const r = render({ status: "in_progress", queue_position: 2 }, { task: task({ status: "in_progress" }) });
  expect(has(r, "tasks-row-queue")).toBe(false);
  expect(has(r, "is-queued")).toBe(false);
  expect(ringClasses(r).some((c) => c.includes("schedule-ring--queued"))).toBe(false);
});

test("no row is the header main has always drawn", async () => {
  const r = render(null);
  const said = text(r);
  expect(said).toContain("Claude");
  expect(said).toContain("alpha");
  expect(said).toContain("T048");
  expect(has(r, "tasks-row-queue")).toBe(false);
  expect(ringClasses(r)).toEqual([]);
  expect(has(r, "c-spark")).toBe(true);
});

test("the usage limit keeps its own red seat", async () => {
  // `.c-tb-paused` was never the queue's: it stays exactly where it was, for the
  // one state it names (platform/lib/usage-limit).
  const r = render(null, { status: "paused · resumes 4:00 AM" });
  expect(has(r, "c-tb-paused")).toBe(true);
  expect(text(r)).toContain("paused · resumes 4:00 AM");
});

test("a done session row beside a queued live row wears the queued row's ring, number and title", async () => {
  // Bugbot, PR #1194: the identity came from the SESSION's row (done, old
  // title) and the caption from the live `pending:<entry>` row (queued). The
  // header must be one row: dashed ring, the waiting message's number and
  // title, the caption — and the session row's project at the far end.
  const r = render(
    {
      key: "pending:e-9",
      task_id: "TASK-052",
      title: "say B",
      status: "queued",
      queue_position: 2,
      queue_ahead: "TASK-046",
    } as QueueFacts,
    { task: task({ status: "done", title: "the old question", task_id: "TASK-048" }), running: true },
  );
  expect(ringClasses(r).some((c) => c.includes("schedule-ring--queued"))).toBe(true);
  expect(ringClasses(r).some((c) => c.includes("schedule-ring--in_progress"))).toBe(false);
  const said = text(r);
  expect(said).toContain("T052");
  expect(said).toContain("say B");
  expect(said).not.toContain("the old question");
  expect(said).toContain("after TASK-046 | 2nd");
  expect(has(r, "task-side-peek-project")).toBe(true);
});

test("a queued row from another folder lends only its number, title and ring", async () => {
  // Bugbot, PR #1194 (low): the live row is a whole listing row, and a send
  // queued into ANOTHER folder's task carries that folder as its project. The
  // header keeps the open chat's project, target and session.
  const r = render(
    {
      key: "pending:e-7",
      task_id: "TASK-060",
      title: "queued elsewhere",
      status: "queued",
      queue_position: 1,
      project: "/repo/beta",
      target: "/repo/beta/y.py",
      session_id: "sess-other",
    } as QueueFacts,
    { task: task({ status: "done", project: "/repo/alpha", target: "/repo/alpha/x.py" }) },
  );
  const project = r.root.findAll(
    (n) => typeof n.type === "string" && String(n.props.className || "").includes("task-side-peek-project"),
  );
  expect(project.length).toBeGreaterThan(0);
  expect(JSON.stringify(r.toJSON())).toContain("alpha");
  expect(JSON.stringify(r.toJSON())).not.toContain("beta");
  expect(text(r)).toContain("T060");
  expect(text(r)).toContain("queued elsewhere");
});
