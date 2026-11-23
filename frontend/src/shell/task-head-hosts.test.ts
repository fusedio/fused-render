// THE SHARED TASK HEAD'S HOSTS (design.md "One task head, everywhere"): each
// host draws `TaskHead`. The Tasks page's panel is
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

test("the rail's Tasks card: both heads on the shared chassis", () => {
  const src = read("claude-rail/TasksPeek.tsx");
  const shared = between(src, "{chat ? (", '<div className="claude-rail-peek-body">');
  // The chat head: ← Back where the × stood, focus seat and all; ↗ Open names
  // where it goes; the chat's own ⋮. No ↑↓ (Akshil, 2026-11-16), and with the
  // pin no card has a ×: the pin is the head's first seat.
  expect(shared).toContain('lead={{ kind: "back", label: "Back to tasks", onPress: () => setChat(null), ref: api.backRef }}');
  expect(shared).toContain("pinNode={<RailPinButton pinned={api.pin.pinned} onToggle={api.pin.onToggle} />}");
  expect(shared).not.toContain("nav={{");
  expect(shared).toContain("label: pageDoorLabel(task)");
  expect(shared).toContain("<Kebab");
  expect(shared).toContain("chipsRef={setChipsHost}");
  expect(shared).toContain("kebab={task?.session_id ? (");
  // ↗ Open never folds, so no host's ⋮ carries it (Akshil, 2026-11-10).
  expect(shared).not.toContain("foldedOpen");
  // The list head: no ×, the pin where the keyboard lands, a static "Tasks",
  // "Open tasks" — and no ladder: it has nothing that folds.
  expect(shared).toContain("fitEnabled={false}");
  expect(shared).toContain("lead={null}");
  expect(shared).toContain("pinNode={<RailPinButton btnRef={api.closeRef} pinned={api.pin.pinned} onToggle={api.pin.onToggle} />}");
  expect(shared).toContain('title="Tasks"');
  expect(shared).toContain('word: "Open tasks"');
  // No old heads left (2026-11-21: the shared head is the only one).
  expect(src).not.toContain('<header className="task-side-peek-head claude-rail-tasks-chat-head">');
  expect(src).not.toContain("ArrowUpRight");
  // The card's chat hands its worktree pill to the shared head's seat.
  expect(src).toContain("headChipsHost={chipsHost}");
  expect(src).toContain("headStatusHost={statusHost}");
});

test("the New task card's chat: the shared head in place of the dialog's, named by its title", () => {
  const src = read("NewJobModal.tsx");
  const shared = between(src, "{...(chatOpen ? {\n        head:", "} : {})}");
  expect(shared).toContain("head: ({ titleId }: ModalHeadContext) => (");
  expect(shared).toContain('lead={{ kind: "back", label: "Back", onPress: leaveChat }}');
  expect(shared).toContain("task={liveChat}");
  expect(shared).toContain("titleId={titleId}");
  expect(shared).toContain('label: "Open this chat"');
  expect(shared).toContain("kebab={chatKebab}");
  expect(shared).toContain("chipsRef={setChipsHost}");
  // The dialog keeps its name for the chat.
  expect(src).toContain('{...(chatOpen ? { ariaLabel: `${chatTitle || "New task"} — chat` } : {})}');
  // The chat's pill in the head's seat; no folded ↗ Open in the ⋮.
  expect(src).not.toContain("foldedOpen");
  expect(src).toContain("headChipsHost={chipsHost}");
  // No old head left: no Back of its own, no door of its own.
  expect(src).not.toContain('className="nt-chat-back"');
  expect(src).not.toContain('className="nt-chat-open"');
  expect(src).not.toContain("ArrowUpRight");
});

test("the Cards popup: the shared head, the doors in its ⋮, the chat's chips in its seat", () => {
  const src = read("TaskCards.tsx");
  // For EVERY task, a session-less one too (its ring files it, TaskHead).
  const shared = between(src, "head={({ titleId, close }: ModalHeadContext) => (", "// The footer exists only while");
  // The × is the chassis' guarded close, not the raw prop.
  expect(shared).toContain('lead={{ kind: "close", label: "Close", onPress: close }}');
  expect(shared).toContain("titleId={titleId}");
  expect(shared).toContain("chips={<TaskPrChip task={task} />}");
  expect(shared).toContain("<Kebab");
  expect(shared).not.toContain("foldedOpen");
  // The disabled "folder deleted" door stays in the ⋮.
  expect(shared).toContain("...(gone ? [{");
  // No session: no chat ⋮ — with the folder gone, a plain ⋮ carries the
  // greyed door and Delete (the old head's doors), so neither is lost.
  expect(shared).toContain(") : gone ? (");
  const bare = between(shared, ") : gone ? (", ") : null}");
  // The chat ⋮'s own trigger and keyboard-operable menu (Kebab's), not a
  // pointer-only ContextMenu.
  expect(bare).toContain('className="c-kebabbtn"');
  expect(bare).toContain("<DropdownMenuTrigger");
  expect(bare).toContain("{`${pageDoorLabel(task)} — folder deleted`}");
  expect(bare).toContain("blockedActionLabel(TASK_ACTION_LABELS.delete, blocked ? RUN_BLOCKED_HINT : null)");
  expect(bare).toContain("onClick={() => setErasing(true)}");
  expect(shared).toContain("breakout: { picking: breakoutCtl.picking, onToggle: breakoutCtl.toggle }");
  // The ChatMount: the shared head's seat and Breakout report.
  expect(src).toContain("headChipsHost={chipsHost}");
  expect(src).toContain("onBreakoutControl={setBreakoutCtl}");
  expect(src).not.toContain("headerInPeek");
});

test("the Tasks page's panel: the chip seat with the shared head", () => {
  const src = read("TaskPeek.tsx");
  expect(src).toContain("headChipsHost={chipsHost}");
  expect(src).toContain("headStatusHost={statusHost}");
  expect(src).not.toContain("headerInPeek");
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
  const form = between(src, "{...(!chatOpen ? {", "// …AND THE OPEN CHAT'S HEAD");
  expect(form).toContain("head: ({ titleId, close, busy: closing, armed, parts }: ModalHeadContext) => (");
  // The ✕'s two states carry over: gone while busy, armed after a dirty close.
  // …and none in the rail's popover with the pin (`pinNode`).
  expect(form).toContain("lead={closing || pinNode ? null : {");
  expect(form).toContain('label: armed ? "Close and discard changes" : "Close"');
  expect(form).toContain("onPress: close,");
  // The same nodes the chassis' head is given: crumb, name, source chip, pills.
  expect(form).toContain("{parts.titleLead}");
  expect(form).toContain("<h2 id={titleId}>{parts.title}</h2>");
  expect(form).toContain("{parts.titleAside}");
  expect(form).toContain('chips={<div className="modal-head-actions">{parts.headActions}</div>}');
  expect(form).toContain("armed,");
  expect(form).toContain("fitEnabled={false}");
});

test("the chat's own head (ClaudeChat): one shared head wherever the chat draws its own", () => {
  const src = readFileSync(join(import.meta.dir, "../apps/claude/ClaudeChat.tsx"), "utf8");
  expect(src).toContain("const headUnified = !compact && !peek;");
  expect(src).toContain("{unifiedHead ? inLead(unifiedHead) : !compact && !peek && !mergedHeadRow ? inLead(");
  expect(src).toContain("{headUnified ? null : showHead ? (");
});

test("the panel's ⋮ filing moves the panel only while it still shows that task", () => {
  const src = read("TaskPeek.tsx");
  expect(src).toContain("if (getPeekState().key === t.key) advancePast(t.key, lastOrderWithKey.current);");
  expect(src).not.toContain("            advancePast(t.key, lastOrderWithKey.current);\n          }}");
});

test("every rail window names the task's pull request, after the chat's branch tag (2026-11-19)", () => {
  // The Tasks card's chat and the New task card's chat had the branch tag and
  // no PR tag; a task's own card (hover, pinned, favourite) had neither.
  const tasks = read("claude-rail/TasksPeek.tsx");
  expect(between(tasks, "{chat ? (", '<div className="claude-rail-peek-body">')).toContain("chips={task ? <TaskPrChip task={task} /> : null}");
  const form = read("NewJobModal.tsx");
  expect(between(form, "{...(chatOpen ? {\n        head:", "} : {})}")).toContain(
    "chips={liveChat ? <TaskPrChip task={liveChat} /> : null}",
  );
  const card = read("claude-rail/RailPeek.tsx");
  expect(card).toContain(
    "? { chips: <TaskPrChip task={head.merged as Task} />, chipsRef: setChipsHost, statusRef: setStatusHost }",
  );
  // Only the chat the head names fills the head's seats — the card keeps
  // several chats mounted — and only while the card is up; each seat
  // remembers the chat whose head it was made in, so a swap never hands the
  // new chat the old head's seats.
  expect(card).toContain(
    "{...(shownKey && t.key === viewKey && chipsSeat.key === t.key && statusSeat.key === t.key",
  );
  expect(card).toContain("? { headChipsHost: chipsSeat.el, headStatusHost: statusSeat.el }");
  expect(card).toContain("(el: HTMLElement | null) => setChipsSeat(el ? { key: headKeyRef.current, el } : NO_SEAT),");
  // A pane's PR tag is never folded behind "+N" for the branch tag's sake.
  const chat = readFileSync(join(import.meta.dir, "../apps/claude/ClaudeChat.tsx"), "utf8");
  expect(chat).toContain("<TaskPrChip task={head.task} />");
  expect(chat).not.toContain("collapsed=");
});

test("every chat host hands the chat both seats: the chips and the pause word's own", () => {
  for (const f of ["TaskPeek.tsx", "claude-rail/TasksPeek.tsx", "NewJobModal.tsx", "TaskCards.tsx"]) {
    const src = read(f);
    expect(src).toContain("statusRef={setStatusHost}");
    expect(src).toMatch(/headStatusHost(: |=\{)statusHost/);
  }
});

test("the folder seat: hosts with no place of their own take the head's chip; the chat pane over its own folder says none", () => {
  // No `place=` in the four hosts that had none: the head draws the folder
  // chip itself (TaskHead, `place === undefined`).
  for (const f of ["TaskPeek.tsx", "claude-rail/TasksPeek.tsx", "NewJobModal.tsx", "TaskCards.tsx"]) {
    expect(/^\s*place=/m.test(read(f))).toBe(false);
  }
  // The pane states its own — and `null`, never an omission, over the folder
  // it is already scoped to (`mergedHead`), so no chip is drawn there.
  const chat = readFileSync(join(import.meta.dir, "../apps/claude/ClaudeChat.tsx"), "utf8");
  // …and none where it knows its place (`placeTag`): a pane IS its place, so
  // the name is not drawn; where in it rides the head's `where` seat
  // (2026-11-16).
  expect(chat).toContain("? (placeTag || props.mergedHead ? null : <TaskPeekProject task={head.task} />)\n          : null");
  expect(chat).toContain("{...(inChat && whereTag ? { where: whereTag } : {})}");
});

test("the rail card: its pulse's facts ride the head's own slots, its star and archive the slots' other seats", () => {
  const src = read("claude-rail/RailPeek.tsx");
  const shared = between(src, "{head && (", '<div className="claude-rail-peek-body">');
  expect(shared).toContain("? { n: head.count, unread: head.unread }");
  expect(shared).toContain("? { text: head.when, ...(head.whenFull ? { title: head.whenFull } : {}) }");
  expect(shared).not.toContain("pulseEl(");
  // The reply line: a hover preview's only — a pressed card shows none.
  expect(shared).toContain("{...(head.reply && !pinned && !held");
  // TWO LINES (2026-11-16): row 1 reads `folder › file` in every card — the
  // place's name is no longer the favourite window's alone — and every card
  // hands the ⋮ over (2026-11-17: "show the kebab when we show fav and open
  // icon"): the head shows it with the star and the door.
  expect(shared).toContain('place={placeEl(head, "place")}');
  expect(shared).not.toContain("placeName ? placeEl");
  expect(shared).toContain('{...(hasWhereTag(head.scope, head.at) ? { where: placeEl(head, "where") } : {})}');
  expect(shared).toContain("kebab={head.t.session_id && shownKey ? kebabFor(head) : null}");
  expect(shared).not.toContain("(pinned || held) ? kebabFor(head)");
  expect(shared).toContain('reveal="window"');
  expect(shared).toContain("starNode: starButton(head)");
  expect(shared).toContain("statusOverlay: archiveButton(head)");
  // No old head left (2026-11-21), and no arrow beside "Open" (2026-11-16).
  expect(src).not.toContain(") : head && (");
  expect(src).not.toContain("ArrowUpRight");
  expect(read("../styles/claude-rail.css")).not.toContain(".claude-rail-peek-open.has-icon");
});

test("a pinned rail card wears no accent: no edge on the card, no accent ink on the pin (2026-11-17)", () => {
  const css = read("../styles/claude-rail.css");
  // The filled glyph alone says pinned (owner, 2026-11-17: "remove accent
  // border when pinned, and make the pin subtle color fill when pinned
  // instead of accent color"): no rule keys the card's edge or the pin's
  // colour off the pressed pin.
  expect(css).not.toContain(":has(.rail-pin-btn.is-pinned) .claude-rail-peek-card");
  expect(css).not.toContain(".modal-dialog:has(.rail-pin-btn.is-pinned)");
  expect(css).not.toMatch(/\.rail-pin-btn\.is-pinned[^{]*\{[^}]*var\(--accent/);
});

test("a pinned rail window's first row wears the hover row's ground (2026-11-17)", () => {
  // Owner: "for pin, make the first row same color hover task header". The
  // ground is the ring hover row's own token, keyed off the pressed pin, and
  // the row bleeds to the head's edges (the head's 10px side padding).
  const head = read("../styles/task-head.css");
  const tasks = read("../styles/tasks.css");
  const rowRule = tasks.slice(tasks.indexOf(".hint-panel.is-row {"));
  expect(rowRule.slice(0, rowRule.indexOf("}"))).toContain("background: var(--row-bg-hover);");
  expect(head).toMatch(
    /\.task-head:has\(\.rail-pin-btn\.is-pinned\) \.task-head-row \{\s*margin: 0 -10px;\s*padding: 0 10px;\s*background: var\(--row-bg-hover\);\s*\}/,
  );
  expect(head).toMatch(/\n\.task-head \{[^}]*padding: 0 10px;/);
  // The legacy heads (a bot's card) have their own first row.
  expect(head).toMatch(
    /\.claude-rail-peek-head:has\(\.rail-pin-btn\.is-pinned\) > \.claude-rail-peek-row:first-child \{[^}]*background: var\(--row-bg-hover\);/,
  );
  // A one-row head is filled down to its rule (2026-11-17: "header line
  // below black, not full fill?"): the head's 5px foot is the row's.
  expect(head).toMatch(/:only-child \{\s*margin-bottom: -5px;\s*padding-bottom: 5px;\s*\}/);
  expect(read("../styles/claude-rail.css")).toMatch(/\n\.claude-rail-peek-head \{[^}]*padding: 4px 8px 5px 6px;/);
  // Still no accent anywhere on a pinned window.
  expect(head).not.toMatch(/:has\(\.rail-pin-btn\.is-pinned\)[^{]*\{[^}]*var\(--accent/);
});

test("every two-line head has one air all round: 10px bands, pinned or not (2026-11-22)", () => {
  // Owner: "make sure in 1st and 2nd row the padding we have between
  // elements is consistent across top bottom and left and right". Row 1 is
  // 35px (7px on top, 28px buttons round a 22px tag), row 2 33px (4px, the
  // 22px archive seat round the 16px ring, 7px foot).
  const head = read("../styles/task-head.css");
  expect(head).toMatch(/\n\.task-head\.task-head\.is-two-line \.task-head-row \{[^}]*min-height: 35px;\s*padding-top: 7px;/);
  expect(head).toMatch(/\n\.task-head\.task-head\.is-two-line \.task-head-titlerow \{[^}]*min-height: 33px;\s*padding: 4px 4px 7px;/);
  const tagTop = 7 + (28 - 22) / 2;
  const tagBottom = tagTop + 22;
  const ringTop = 35 + 4 + (22 - 16) / 2;
  const ringBottom = ringTop + 16;
  expect(tagTop).toBe(10);
  expect(ringTop - tagBottom).toBe(10);
  expect(35 + 33 - ringBottom).toBe(10);
  // A pin moves the ground's edge, never the chat: the two rows still sum to
  // the same height.
  const pinRow = head.match(/is-two-line:has\(\.rail-pin-btn\.is-pinned\) \.task-head-row \{[^}]*min-height: (\d+)px/);
  const pinTitle = head.match(/is-two-line:has\(\.rail-pin-btn\.is-pinned\) \.task-head-titlerow \{[^}]*min-height: (\d+)px/);
  expect(Number(pinRow?.[1]) + Number(pinTitle?.[1])).toBe(35 + 33);
});

test("a two-line panel host names its own window, so the pane-wide hover never rides on layout geometry (2026-11-21)", () => {
  // TaskHead's `is-hot` trigger falls back to "the first ancestor twice the
  // head's height" when no host says; measured at mount, that box can be the
  // head's own wrapper while a panel is still loading — and the pane's star
  // and Open then never show under the pointer.
  expect(read("TaskPeek.tsx")).toContain('data-task-window=""');
  expect(read("../apps/claude/ClaudeChat.tsx")).toContain('data-task-window=""');
});
