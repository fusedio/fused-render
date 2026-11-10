// THE SHARED TASK HEAD'S HOSTS, behind the flag (design.md "One task head,
// everywhere", PR 1): each host draws `TaskHead` when `useTaskHeadUnified()` is
// on and its old head, untouched, when it is off. The Tasks page's panel is
// MOUNTED in TaskPeek.head.test.tsx; these three are read off the source, the
// way task-peek-flag.test.ts pins the panel's own head — their mounts need a
// rail card's chassis, the New task card's whole form, and the Cards wall.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const read = (name: string) => readFileSync(join(import.meta.dir, name), "utf8");
const between = (src: string, from: string, to: string) => {
  const i = src.indexOf(from);
  expect(i).toBeGreaterThan(-1);
  const j = src.indexOf(to, i + from.length);
  expect(j).toBeGreaterThan(i);
  return src.slice(i, j);
};

test("the rail's Tasks card: both heads on the shared chassis, the old ones kept behind the flag", () => {
  const src = read("claude-rail/TasksPeek.tsx");
  expect(src).toContain("const unified = useTaskHeadUnified();");
  const shared = between(src, "{unified ? (", ") : chat ? (");
  // The chat head: ← Back where the × stood, focus seat and all; the chevrons
  // walk the card's own list; ↗ Open names where it goes; the chat's own ⋮.
  expect(shared).toContain('lead={{ kind: "back", label: "Back to tasks", onPress: () => setChat(null), ref: api.backRef }}');
  expect(shared).toContain("nav={{ prev: chatEnds.prev, next: chatEnds.next, step: stepChat }}");
  expect(shared).toContain("label: pageDoorLabel(task)");
  expect(shared).toContain("<Kebab");
  expect(shared).toContain("chipsRef={setChipsHost}");
  expect(shared).toContain("kebab={(fit) => task?.session_id ? (");
  expect(shared).toContain("return foldedOpen(fit) && href");
  // The list head: × closing the card, a static "Tasks", "Open tasks" — and
  // no ladder, since it has no ⋮ to fold its only door into.
  expect(shared).toContain("fitEnabled={false}");
  expect(shared).toContain('lead={{ kind: "close", label: "Close Tasks", hint: "Close", onPress: api.close, ref: api.closeRef }}');
  expect(shared).toContain('title="Tasks"');
  expect(shared).toContain('word: "Open tasks"');
  // The old heads are still there, as they were.
  expect(src).toContain('<header className="task-side-peek-head claude-rail-tasks-chat-head">');
  expect(src).toContain('aria-label="Open this task"');
  expect(src).toContain('className="btn claude-rail-peek-open"');
  expect(src).toMatch(/onClick=\{\(\) => api\.openPage\(\)\}>\s*Open tasks\s*<\/button>/);
  // The card's chat hands its worktree pill to the shared head's seat.
  expect(src).toContain("{...(unified ? { headChipsHost: chipsHost } : {})}");
});

test("the New task card's chat: the shared head in place of the dialog's, named by its title", () => {
  const src = read("NewJobModal.tsx");
  expect(src).toContain("const unified = useTaskHeadUnified();");
  const shared = between(src, "{...(chatOpen && unified ? {", "} : {})}");
  expect(shared).toContain("head: ({ titleId }: ModalHeadContext) => (");
  expect(shared).toContain('lead={{ kind: "back", label: "Back", onPress: leaveChat }}');
  expect(shared).toContain("task={liveChat}");
  expect(shared).toContain("titleId={titleId}");
  expect(shared).toContain('label: "Open this chat"');
  expect(shared).toContain("kebab={chatKebab}");
  expect(shared).toContain("chipsRef={setChipsHost}");
  // The folded ↗ Open lands in the ⋮, and the chat's pill in the head's seat.
  expect(src).toContain("extraItems={foldedOpen(fit) && chatHref ? [{");
  expect(src).toContain("{...(unified ? { headChipsHost: chipsHost } : {})}");
  // The old head's swap is untouched.
  expect(src).toContain('className="nt-chat-back"');
  expect(src).toContain('className="nt-chat-open"');
});

test("the Cards popup: the shared head, the doors in its ⋮, the chat's chips in its seat", () => {
  const src = read("TaskCards.tsx");
  expect(src).toContain("const unified = useTaskHeadUnified();");
  // Only for a task with a session: without one there is no ⋮, and the old
  // head's Archive / Delete / folder-deleted door are where those acts live.
  expect(src).toContain("const sharedHead = unified && !!task.session_id;");
  const shared = between(src, "{...(sharedHead ? {", "// The footer exists only while");
  // The × is the chassis' guarded close, not the raw prop.
  expect(shared).toContain("head: ({ titleId, close }: ModalHeadContext) => (");
  expect(shared).toContain('lead={{ kind: "close", label: "Close", onPress: close }}');
  expect(shared).toContain("titleId={titleId}");
  expect(shared).toContain("chips={<TaskPrChip task={task} />}");
  expect(shared).toContain("<Kebab");
  expect(shared).toContain("...(foldedOpen(fit) && explorer ? [{");
  expect(shared).toContain("...(gone ? [{");
  expect(shared).toContain("breakout: { picking: breakoutCtl.picking, onToggle: breakoutCtl.toggle }");
  // The ChatMount: the shared head's seat and Breakout report, or the old
  // slim line.
  expect(src).toContain("? { headChipsHost: chipsHost, onBreakoutControl: setBreakoutCtl }");
  expect(src).toContain(": { headerInPeek: true, headerExtra: <TaskPrChip task={task} /> })}");
  // The old head's doors are still drawn when the flag is off.
  expect(src).toContain('className="btn btn-secondary modal-head-act"');
});

test("the Tasks page's panel: the old slim line with the old head, the chip seat with the new", () => {
  const src = read("TaskPeek.tsx");
  expect(src).toContain("? { headChipsHost: chipsHost }");
  expect(src).toContain(": { headerInPeek: true, headerExtra: <TaskPrChip task={task} /> })}");
  // An archive or a delete the ⋮ reports after the fact still advances along
  // an order that held the task.
  // …by the TASK KEY the ⋮ was built for (the order is keyed by key, and the
  // ⋮ reports a session id).
  expect(src).toContain("advancePast(t.key, lastOrderWithKey.current)");
  expect(src).not.toContain("advancePast(s, lastOrderWithKey.current)");
  expect(src).toContain("if (list.includes(key)) lastOrderWithKey.current = list;");
});

test("the New task card's FORM: the chassis' own head pieces on the shared head, × in the lead", () => {
  const src = read("NewJobModal.tsx");
  const form = between(src, "{...(!chatOpen && unified ? {", "// …AND WITH THE SHARED HEAD ON");
  expect(form).toContain("head: ({ titleId, close, busy: closing, armed, parts }: ModalHeadContext) => (");
  // The ✕'s two states carry over: gone while busy, armed after a dirty close.
  expect(form).toContain("lead={closing ? null : {");
  expect(form).toContain('label: armed ? "Close and discard changes" : "Close"');
  expect(form).toContain("onPress: close,");
  // The same nodes the chassis' head is given: crumb, name, source chip, pills.
  expect(form).toContain("{parts.titleLead}");
  expect(form).toContain("<h2 id={titleId}>{parts.title}</h2>");
  expect(form).toContain("{parts.titleAside}");
  expect(form).toContain("chips={parts.headActions}");
  expect(form).toContain("fitEnabled={false}");
});

test("the chat's own head (ClaudeChat): one shared head behind the flag, the strip and Topbar kept for off", () => {
  const src = readFileSync(join(import.meta.dir, "../apps/claude/ClaudeChat.tsx"), "utf8");
  expect(src).toContain("const headUnified = useTaskHeadUnified() && !compact && !peek;");
  expect(src).toContain("{unifiedHead ? inLead(unifiedHead) : !compact && !peek && !mergedHeadRow ? inLead(");
  expect(src).toContain("{headUnified ? null : showHead ? (");
  // The legacy strip and its ← Chats are still there for the flag off.
  expect(src).toContain('<div className="c-anntools" ref={stripRef}>');
  expect(src).toContain('←<span className="c-back-word"> Chats</span>');
});
