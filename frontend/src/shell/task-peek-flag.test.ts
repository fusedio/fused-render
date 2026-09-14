// THE FLAG'S CONTRACT: with `task_peek_enabled` off, the Tasks page is the page
// it was before any of this existed.
//
// That is a claim about ABSENCE, and absence is the one thing a feature's own
// tests never check — every other suite here exercises the peek with the peek
// on. What follows is the other side: the store refuses to open, and no marking
// of the feature's reaches the markup or the stylesheet's live selectors.
//
// Two kinds of check, deliberately:
//
//   * BEHAVIOURAL, for the store — `openPeek` answers false, which is what
//     sends every one of the four views back to `navigateUrl` (performOpen);
//   * SOURCE, for the markup — every attribute and class the feature adds is
//     written behind a `peekOn` guard, and every stylesheet rule that changes
//     the toolbar's layout is scoped to the `data-fit` that only exists while
//     the feature is on. A render test would need four mounted views and the
//     whole task feed; the guards are one line each and reading them is what a
//     reviewer would do anyway.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { beforeEach, describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SHELL = new URL(".", import.meta.url).pathname;
const read = (rel: string) => readFileSync(join(SHELL, rel), "utf8");
const VIEWS = read("ScheduleTaskViews.tsx");
const CARDS = read("TaskCards.tsx");
const CALENDAR = read("ScheduleCalendar.tsx");
const PAGE = read("Scheduled.tsx");
const APP = read("App.tsx");
const FIT = read("row-fit.ts");
const FLAG = read("task-peek-flag.ts");
const STORE = read("task-peek-store.ts");
const SCHEDULE_CSS = read("../styles/schedule.css");
const PEEK_CSS = read("../styles/task-peek.css");
const TASKS_CSS = read("../styles/tasks.css");
const CARDS_CSS = read("../styles/task-cards.css");

/** One rule, as its selector list and its declarations together — the shape the
 *  assertions below want, since several of them are about WHICH surfaces share
 *  one declaration block. Comments are stripped first, so prose that names a
 *  selector cannot be mistaken for the rule that states it. */
function block(css: string, selector: string): string {
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
  for (const [whole, list] of bare.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (list.split(",").some((one) => one.trim() === selector)) return whole;
  }
  throw new Error(`no rule whose selector list holds exactly "${selector}"`);
}
const SIDEBAR = read("../platform/ui/sidebar/SidebarFrame.tsx");

const store = await import("./task-peek-store");

beforeEach(() => {
  store.resetPeekStoreForTests();
});

describe("the store, with the feature off", () => {
  it("refuses to open — which is what sends every view back to navigating", () => {
    // `performOpen` reads exactly this answer: false means "do what you always
    // did", and what it always did is `navigateUrl(intent.href)`.
    expect(store.peekHostReady()).toBe(false);
    expect(store.openPeek("sess-1")).toBe(false);
    expect(store.getPeekState().key).toBeNull();
    expect(store.getPeekState().host).toBe(false);
  });

  it("ignores a `?peek=` in the URL entirely", () => {
    // A link someone shared, or a stale tab: with the feature off the param
    // names nothing and nothing adopts it.
    store.syncPeekFromUrl("?view=list&peek=sess-1");
    expect(store.getPeekState().key).toBeNull();
  });

  it("has nothing to settle, so it cannot rewrite the URL either", () => {
    store.settlePeek([{ key: "sess-1", task_id: "TASK-001" }]);
    expect(store.getPeekState().key).toBeNull();
  });

  it("…and the page turning the host ON is the only thing that changes that", () => {
    store.setPeekHost(true);
    expect(store.getPeekState().host).toBe(true);
    expect(store.openPeek("sess-1")).toBe(true);
  });
});

describe("the flag module", () => {
  it("is the native chat's idiom, down to the tri-state", () => {
    expect(FLAG).toContain("export function useTaskPeekFlag(): boolean | null");
    expect(FLAG).toContain("export function useTaskPeekEnabled(): boolean");
    expect(FLAG).toContain("export function publishTaskPeekEnabled");
    expect(FLAG).toContain("export function resetTaskPeekFlagForTests");
    // One shared read, a generation guard, and a bounded retry — the three
    // things that make two mounts cost one GET.
    expect(FLAG).toContain("let reading: Promise<void> | null = null;");
    expect(FLAG).toContain("let generation = 0;");
    expect(FLAG).toContain(".catch(() => getPrefs())");
  });

  it("reads the pref strictly: only a stored true is on", () => {
    // A server that predates the switch sends no `task_peek` at all, and that
    // must read as off — the pref's own default and today's behaviour.
    expect(FLAG).toContain("p.task_peek?.enabled === true");
  });

  it("settles a failed read on OFF rather than leaving it unknown", () => {
    // `null` sticking would be a page that never decides which behaviour it has.
    expect(FLAG).toContain("if (generation === departed) set(false);");
  });
});

describe("the markup adds nothing when the feature is off", () => {
  it("stamps the walk's attribute only behind the guard, in all four views", () => {
    for (const src of [VIEWS, CARDS, CALENDAR]) {
      // Every spread of the attribute is conditional, and none is left bare.
      expect(src).toContain("{...(peekOn ? { [PEEK_ITEM_ATTR]:");
      expect(src).not.toContain("{...{ [PEEK_ITEM_ATTR]:");
    }
    // The Board's is in the same file as the List's — two call sites, both
    // guarded, which is why the file is asserted to hold no unguarded spread.
    expect(VIEWS).not.toContain("{...{ [PEEK_ITEM_ATTR]: task.key }}");
  });

  it("draws no halo — the open key is read, and then spent only when the host is up", () => {
    for (const src of [VIEWS, CARDS, CALENDAR]) {
      // The FLAG IS SPENT ON THE VALUE, never on the call. `host` starts false
      // and flips true in a layout effect, so a hook behind that condition is a
      // hook that appears between two renders — which React throws on. Every
      // one of these reads the key unconditionally and gates what it does with
      // it (Bugbot, PR #1133).
      expect(src).toMatch(/=\s*usePeekedKey\(\);/);
      expect(src).not.toMatch(/\?\s*usePeekedKey\(\)/);
      expect(src).toMatch(/peekOn \? (peekedKey|openKey) : null/);
    }
  });

  it("draws no quick-open door", () => {
    expect(VIEWS).toContain("{peekOn && page && !openDraft && (");
    expect(VIEWS).toContain("{peekOn && page && !isDraftTask(task) && (");
    // …and the card's hover strip is not drawn FOR one either.
    expect(VIEWS).toContain("{((peekOn && page) || file || folderMissing");
  });

  it("writes no fit attributes on the list or the toolbar", () => {
    expect(VIEWS).toContain('{...(peekOn ? { "data-fit": fit } : {})}');
    expect(PAGE).toContain('{...(peekOn ? { "data-fit": toolbar.level } : {})}');
  });

  it("attaches no observers at all — the hooks return before they measure", () => {
    // Not merely "writes no attribute": an off page must not be paying for a
    // ResizeObserver and a MutationObserver per list either.
    expect(FIT).toContain("if (!enabled) return;");
    expect(FIT).toContain("if (!enabled || !el) return;");
    expect(FIT).toContain("return enabled ? level : 0;");
    expect(FIT).toContain("return [enabled ? level : 0, setEl];");
    // …and the toolbar's wrapper still answers the stable OFF verdict, so a
    // disabled page does not get a fresh object every render either.
    expect(FIT).toContain("(enabled ? { level } : OFF_FIT)");
  });

  it("mounts no panel: the host is the flag AND the page", () => {
    expect(PAGE).toContain("const peekable = !scope && peekOn;");
    expect(PAGE).toContain("if (!peekable) return page;");
    // Which also means no param-boundary claim from this page: the claim lives
    // in TaskPeek, and TaskPeek is inside the branch above.
    expect(read("TaskPeek.tsx")).toContain("useParamBoundary(nativeChat === false && !!src)");
  });

  it("keeps the nav epoch's ignore list empty", () => {
    // On main a traversal is judged on the whole URL; off, it still is.
    expect(APP).toContain("const NO_PAGE_PARAMS: readonly string[] = [];");
    expect(APP).toContain("useNavEpoch(taskPeekOn ? PAGE_PARAMS : NO_PAGE_PARAMS)");
  });

  it("leaves the sidebar's collapse exactly as it was", () => {
    expect(SIDEBAR).toContain("const collapsing = tuckOnCollapse && sidebarCollapsed");
    expect(SIDEBAR).toContain("tuckOnCollapse?: boolean;");
    expect(read("GlobalSidebar.tsx")).toContain("tuckOnCollapse={taskPeekOn}");
  });
});

describe("the peek header", () => {
  const HEAD = read("TaskPeek.tsx");

  it("is the PEEK's own row — no Claude wordmark, no model cluster", () => {
    // It wore `@apps/claude/ui/Topbar` for a day (design.md, Round 3), which
    // bought one row instead of two and cost the row its subject: a ✻ Claude
    // mark and a model/run status are facts about the TOOL.
    // The import is gone (the comment that records why it left is not).
    expect(HEAD).not.toContain('from "@apps/claude/ui/Topbar"');
    expect(HEAD).not.toContain("<ChatTopbar");
  });

  it("orders the row left to right, and that order is the tab order", () => {
    // Controls · who · acts. Read off the source in the order it renders,
    // because the tab order IS the DOM order and there is no tabindex anywhere
    // in this header to say otherwise.
    const head = HEAD.slice(
      HEAD.indexOf('<header className="task-side-peek-head"'),
      HEAD.indexOf("</header>"),
    );
    const at = (needle: string) => {
      const i = head.indexOf(needle);
      expect(i).toBeGreaterThan(-1);
      return i;
    };
    const order = [
      'aria-label="Close the task panel"',
      // Only in cover, and directly after the ×: two acts, two controls
      // (design.md, Polish batch 4, item 11).
      "task-side-peek-resize",
      'aria-label="Previous task"',
      'aria-label="Next task"',
      "<StatusIcon",
      "task-side-peek-id",
      "task-side-peek-title",
      // …and the right cluster reads project · Open · ⋮: the FACT, then the
      // act, then the rest of the acts (design.md, Polish batch 4).
      "task-side-peek-project",
      "task-side-peek-open",
      'aria-label="More actions"',
    ].map(at);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(head).not.toContain("tabIndex");
  });

  it("leads with a plain × in EVERY mode, and adds Resize panel only in cover", () => {
    // It was a `PanelIcon` whose meaning changed with the layout — "hide the
    // right panel", and in cover "show the list" (Polish batch 3, option b).
    // The one control every reader reaches for first was the one they had to
    // decode first, so it is a × now and it always closes (design.md, Polish
    // batch 4, item 11).
    expect(HEAD).toContain('aria-label="Close the task panel"');
    expect(HEAD).toContain('data-hint="Close · Esc"');
    expect(HEAD).not.toContain("<PanelIcon");
    expect(HEAD).not.toContain('from "@platform/ui/PanelIcon"');
    expect(HEAD).not.toContain("layout.cover ? showListBesidePeek() : closePeek()");
    // The way back to the split is a SECOND control beside it, shown only where
    // there is a split to go back to — a control that changes what it does
    // under the reader is worse than two controls.
    expect(HEAD).toContain("{layout.cover && (");
    // …and it says what it does IN WORDS, with no glyph at all (design.md,
    // Polish batch 5): the four inward corner marks it wore were a picture a
    // reader had to be taught, for an act that happens once in a visit.
    expect(HEAD).toContain('className="task-side-peek-resize"');
    expect(HEAD).toContain("Resize panel");
    expect(HEAD).toContain('data-hint="Restore the list beside the panel"');
    expect(HEAD).not.toContain("ICON_RESET_SIZE");
    expect(HEAD).toContain("onClick={() => showListBesidePeek()}");
    // …and with a × in the corner at every width, the ⋮'s own Close row is a
    // second way to do what the corner already does.
    expect(HEAD).not.toContain('items.push({ label: "Close", icon: ICON_CLOSE');
  });

  it('says "↗ Open" in words, in the header and on the row alike', () => {
    // The folder door mark was the page's one picture for this act, and a good
    // one in a MENU. Out on a row of glyph buttons that are otherwise verbs —
    // run, archive, delete — it was the only mark a reader had to be taught,
    // one tooltip at a time (design.md, Polish batch 4).
    //
    // THE MARK LEADS AND THE ARROW POINTS OUT (Polish batch 5): an external-
    // link glyph in front of the word, the way every other icon-and-word
    // control in this app is built. The trailing "→" it replaces meant
    // "forward", which is what the Next chevron three inches away means.
    expect(HEAD).toContain("{ICON_OPEN_EXTERNAL}\n                  Open");
    expect(HEAD).not.toContain("ICON_ARROW_RIGHT");
    expect(VIEWS).not.toContain("ICON_ARROW_RIGHT");
    expect(VIEWS).toContain("const OPEN_DOOR_LABEL = (");
    expect(VIEWS).toContain("{OPEN_DOOR_LABEL}");
    // …and the same words on the row AND on the wall card, which is the whole
    // claim: one act, one wording, three surfaces.
    expect((VIEWS.match(/\{OPEN_DOOR_LABEL\}/g) ?? []).length).toBe(2);
    expect(VIEWS).toContain('className="tasks-act tasks-act--page"');
    expect(VIEWS).toContain('className="tasks-act tasks-card-act tasks-act--page"');
    // …AND THE CARDS WALL, which lives in TaskCards.tsx and draws its own door.
    // It takes the words from the same export rather than typing them again.
    expect(CARDS).toContain("OPEN_DOOR_LABEL,");
    expect(CARDS).toContain("{peekOn ? OPEN_DOOR_LABEL : ICON_FOLDER}");
    expect(VIEWS).toContain("export const OPEN_DOOR_LABEL = (");
    // BOTH of the wall's two doors — the live one and the disabled one a gone
    // folder gets.
    expect((CARDS.match(/\{peekOn \? OPEN_DOOR_LABEL : ICON_FOLDER\}/g) ?? []).length).toBe(2);
    // FLAG OFF, MAIN'S GLYPH. This file's cards also render on the app page's
    // Tasks tab, where there is no peek — the door keeps the folder there, and
    // the text styling is gated on the same `peekOn` that the List row's own
    // door is rendered behind.
    expect(CARDS).toContain('(peekOn ? " task-card-door--page" : "")');
    expect(CARDS).toContain("const ICON_FOLDER = (");
    expect(CARDS_CSS).toContain(".task-card-doors > .task-card-door--page {");
    // …and all three doors wear an OUTLINE at rest (design.md, Polish batch 5).
    // The wall's already did; the other two were bare words in a run of ink,
    // with only a hover wash to say they could be pressed.
    expect(PEEK_CSS).toContain(".task-side-peek-open,\n.task-side-peek-resize {");
    expect(PEEK_CSS.slice(PEEK_CSS.indexOf(".task-side-peek-open,"))).toContain(
      "border: 1px solid var(--border);",
    );
    expect(TASKS_CSS.slice(TASKS_CSS.indexOf(".tasks-act--page,"))).toContain(
      "border-color: var(--border);",
    );
    // The SHAPE survives where a mark is still the right answer — the peek's ⋮,
    // where a menu row is a label with a glyph beside it.
    expect(HEAD).toContain("<svg {...ICON}><path d={ICON_OPEN_FOLDER_PATH} /></svg>");
    expect(HEAD).toContain("icon: ICON_OPEN_DOOR");
    // …and the arrows the folder replaced are still gone from the rows, cards
    // and header.
    expect(VIEWS).not.toContain("M14 4h6v6M20 4l-7 7M10 20H4v-6M4 20l7-7");
    expect(HEAD).not.toContain("M14 4h6v6M20 4l-7 7M10 20H4v-6M4 20l7-7");
  });

  it("walks with CHEVRONS, not arrows", () => {
    expect(HEAD).toContain('<svg {...ICON}><polyline points="18 15 12 9 6 15" /></svg>');
    expect(HEAD).toContain('<svg {...ICON}><polyline points="6 9 12 15 18 9" /></svg>');
  });

  it("names the ARROW KEYS in the chevrons' tooltips", () => {
    // A tooltip names the gesture a reader is most likely to reach for, and on
    // a list with a panel open that is ↑/↓ — not ⌃⇧K/J, which is a chord you
    // have to be told about twice (design.md, Polish batch 5). The chords still
    // work; they are simply not what the button advertises.
    expect(HEAD).toContain('data-hint="Previous task · ↑"');
    expect(HEAD).toContain('data-hint="Next task · ↓"');
    expect(HEAD).not.toContain("⌃⇧K\"");
    expect(HEAD).not.toContain("⌃⇧J\"");
  });

  it("has no ⌘↩ anywhere — the doors are the way out", () => {
    // A chord with no mark on it except the tooltip of the button that does the
    // same thing one press away, while ⌘↩ in the New task modal SUBMITS
    // (design.md, Polish batch 5). Handler, binding and tooltip all gone.
    expect(HEAD).not.toContain("⌘↩");
    expect(HEAD).not.toContain('e.key === "Enter" && (e.metaKey || e.ctrlKey)');
    // …and the act itself is untouched: the header's door and the ⋮ row both
    // still call it.
    expect(HEAD).toContain("const openAsPage = useCallback(");
  });

  it("walks quietly: into view, focused, and without a ring", () => {
    // Three claims, and they are one gesture (design.md, Polish batch 5).
    // `block: "nearest"` does nothing when the item is already on screen, so an
    // ordinary walk down a visible list never jerks the scroller.
    expect(HEAD).toContain('el.scrollIntoView({ block: "nearest" });');
    // Focus follows ONLY from the frame: a chevron is a button a keyboard may
    // want to press again, and stealing focus out of it on the first press is a
    // control that works once.
    expect(HEAD).toContain('active.closest(".tasks-frame")');
    expect(HEAD).toContain("el.focus({ preventScroll: true });");
    // …and the ring the UA would draw on that focus is taken off by a mark
    // React cannot wipe (an ATTRIBUTE: every one of these items has a
    // React-owned className that changes on the very same open).
    expect(STORE).toContain('export const PEEK_WALK_ATTR = "data-peek-walk";');
    expect(HEAD).toContain("el.setAttribute(PEEK_WALK_ATTR, \"\");");
    expect(PEEK_CSS).toContain("[data-peek-walk]:focus,\n[data-peek-walk]:focus-visible {");
  });

  it("paints the preview's gutters with the CHAT's ground, not the page's", () => {
    // Edge to edge the panel showed three colours down one column: header, then
    // 28px of page either side of the app, then the conversation on a lighter
    // surface again (design.md, Polish batch 5).
    expect(PEEK_CSS).toContain("background: var(--peek-chat-bg);");
    // AND THE VALUE IS THE CHAT'S OWN, both themes. It is a literal here
    // because `apps/claude/styles/chat.css` arrives with a `lazy()` import and
    // is not loaded at all for a reader on the legacy iframe chat, so
    // `var(--c-panel)` would resolve to nothing exactly half the time. The two
    // files are pinned to each other here instead.
    const chat = read("../apps/claude/styles/chat.css");
    const panel = (from: number) => /--c-panel: (#[0-9a-f]{6});/.exec(chat.slice(from))?.[1];
    const light = chat.indexOf(':root[data-theme="light"] .chat-root,');
    expect(PEEK_CSS).toContain(`--peek-chat-bg: ${panel(0)};`);
    expect(PEEK_CSS).toContain(`--peek-chat-bg: ${panel(light)};`);
  });

  it("carries a VERTICAL kebab holding exactly the three stated acts", () => {
    // Vertical because it sits at the end of a row rather than in one.
    expect(HEAD).toContain('<circle cx="12" cy="5"');
    for (const label of ["Continue this task in terminal", "Archive task", "Delete task"]) {
      expect(HEAD).toContain(label);
    }
    expect(HEAD).toContain("Unarchive task");
    // The two that left: the first is a control of its own in the header now,
    // the second was a menu row nobody could find for an act the address bar
    // already does.
    expect(HEAD).not.toContain('label: "Open as page"');
    expect(HEAD).not.toContain('label: "Copy link"');
  });

  it("gives every icon a tooltip", () => {
    const head = HEAD.slice(
      HEAD.indexOf('<header className="task-side-peek-head"'),
      HEAD.indexOf("</header>"),
    );
    // One `data-hint` per control — the app's own tooltip contract. Six now:
    // close, Resize panel (cover only), prev, next, Open, kebab. The project name
    // is not in the count any more: it is a label, and its full path is a
    // `title` rather than a hint (design.md, Polish batch 4).
    expect(head.match(/data-hint=/g)?.length).toBe(6);
  });

  it("stays ONE LINE at the pane minimum by folding, in a stated order", () => {
    // The title ellipsises all the way down first; then the project name goes,
    // then the door — and the door reappears in the ⋮, because a hidden control
    // has to be somewhere (design.md, Header + list state v2).
    expect(FIT).toContain('export const PEEK_HEAD_DROPS = [\n  ".task-side-peek-project",\n  ".task-side-peek-open",\n]');
    expect(PEEK_CSS).toContain(
      '.task-side-peek-head[data-fit]:not([data-fit="0"]) .task-side-peek-project',
    );
    expect(PEEK_CSS).toContain('.task-side-peek-head[data-fit="2"] .task-side-peek-open');
    expect(HEAD).toContain("headFit >= PEEK_HEAD_DROPS.length");
    // Nothing else ever folds: the way out of a panel and the way to its
    // actions are what a narrow window must never take away.
    for (const kept of ["task-side-peek-title", "task-side-peek-id", "task-side-peek-kebab"]) {
      expect(PEEK_CSS).not.toContain(`[data-fit] .${kept} {\n  display: none`);
    }
  });

  it("sends a MESSAGE row to the panel, at that turn", () => {
    // The press used to leave the page for Explorer; now the thread the reader
    // has expanded stays on screen beside the conversation, which is the whole
    // point of a peek and the one thing this press could not do (design.md,
    // Polish batch 3). `openPeek` answers false off /tasks, and then it is the
    // navigation it has always been.
    expect(VIEWS).toContain("if (openPeek(task.key, { anchor: m.anchor || null })) {");
    expect(VIEWS).toContain("navigateUrl(to);");
    // The anchor reaches BOTH chats: the legacy template takes it on its URL,
    // the native one as a seeded param.
    expect(read("../apps/claude/legacy-src.ts")).toContain(
      'msgAnchor ? `&msg=${encodeURIComponent(msgAnchor)}` : ""',
    );
    expect(HEAD).toContain("{...(anchor ? { msgAnchor: anchor } : {})}");
    expect(read("../apps/claude/ChatMount.tsx")).toContain(
      'if (msgAnchor && memory.get("msg") !== msgAnchor) memory.set({ msg: msgAnchor });',
    );
  });

  it("makes the project a FACT again, not a second door", () => {
    // It was a folder mark and a name wired to the folder's Explorer page,
    // sitting an inch from a door that also wore a folder and went somewhere
    // else — two pictures of the same thing meaning two different places
    // (Akshil, 2026-09-14 — design.md, Polish batch 4). The header keeps
    // exactly one way out, and it is the one that says its act in words.
    expect(HEAD).not.toContain("const folderPage");
    expect(HEAD).not.toContain('data-hint="Open project folder"');
    expect(HEAD).not.toContain("ICON_FOLDER_MARK");
    // A plain muted span with the full path in its tooltip — and no hover
    // wash, because a label that lights up under the pointer promises a press.
    expect(HEAD).toContain('<span\n                className="task-side-peek-project"');
    expect(HEAD).toContain("title={tildePath(task.project, home)}");
    expect(PEEK_CSS).not.toContain(".task-side-peek-project:hover");
  });

  it("walks with ↑/↓ too — but only where nothing else is entitled to", () => {
    // The chevrons' gesture on the keys a reader actually reaches for
    // (design.md, Polish batch 4). ⌃⇧J/K are untouched and still above it.
    expect(HEAD).toContain("e.ctrlKey && e.shiftKey && !e.metaKey && !e.altKey");
    expect(HEAD).toContain('if ((e.key === "ArrowDown" || e.key === "ArrowUp")');
    // BARE arrows only — a modified one belongs to whatever owns the modifier.
    expect(HEAD).toContain("!e.metaKey && !e.ctrlKey &&\n          !e.altKey && !e.shiftKey");
    // TWO GUARDS: which document the press happened in (the preview and the
    // legacy chat attach this very listener in documents of their own, and in
    // there the arrows are the app's and the conversation's), and what is
    // focused in ours.
    expect(HEAD).toContain("doc === document && arrowShouldWalk(e.target)");
    // …and the refusal is SILENT: an arrow this panel declines has to reach
    // whatever would have had it, page scroll included. INCLUDING AT THE ENDS —
    // `step` answers whether it actually moved, and at the first or last task
    // it does not, so the key is not swallowed for a walk that did nothing.
    expect(HEAD).toContain("if (!step(e.key === \"ArrowDown\" ? 1 : -1)) return false;");
    expect(HEAD).toContain(
      "return false;\n        e.preventDefault();\n        return true;",
    );
    expect(HEAD).toContain("(delta: number): boolean => {");
    expect(HEAD).toContain("if (!next) return false;");
  });

  it("confirms the delete against the task it was ASKED about, not the open one", () => {
    // Bugbot, 78118e0fa. The modal read the live `task`, which is whatever the
    // panel is showing NOW — so anything that swapped the open task while the
    // confirmation was up (a chevron, ⌃⇧J, an arrow key, an archive advancing
    // the panel, a poll) retargeted the confirmation at a task the reader never
    // asked about, under a dialog still spelling out the old one's number.
    //
    // The snapshot IS the open state: one value, so a stale boolean cannot
    // disagree with it.
    expect(HEAD).toContain("const [erasing, setErasing] = useState<Task | null>(null);");
    expect(HEAD).toContain("setErasing(task);"); // captured at the press
    expect(HEAD).toContain("{erasing && (");
    expect(HEAD).toContain("task={erasing}");
    // …and every later mention of the deleted task is the SNAPSHOT — the prop,
    // the toast and the advance alike.
    expect(HEAD).toContain("const erased = erasing;");
    expect(HEAD).toContain("notify({ title: `Deleted ${erased.task_id}`, tone: \"info\" });");
    expect(HEAD).toContain("advancePast(erased.key, eraseOrder.current);");
    expect(HEAD).not.toContain("setErasing(true)");
    // …and the arrows cannot reach the panel from inside the dialog in the
    // first place, which is the other half of the same fix.
    expect(STORE).toContain('.modal-dialog, [role="dialog"], [aria-modal="true"]');
  });

  it("advances rather than closing when the task is filed or deleted", () => {
    // Filing is a sweep. The order is read BEFORE the act, because the poll
    // that follows takes the row away (shell/task-peek-store.ts).
    expect(HEAD).toContain("const visible = order();");
    expect(HEAD).toContain("advancePast(from, visible);");
    // The delete path reads it at the KEBAB PRESS, not in the modal's `onDone`:
    // by then the confirmation has been on screen for as long as the reader
    // took to read it, and a poll in between would have moved the list.
    expect(HEAD).toContain("eraseOrder.current = order();");
    // …against the SNAPSHOT of the task the delete was asked about, not the
    // panel's current one (Bugbot, 78118e0fa — the test above).
    expect(HEAD).toContain("advancePast(erased.key, eraseOrder.current);");
    expect(HEAD).toContain("if (next) openPeek(next);\n    else closePeek();");
  });
});

describe("the one selected style, and the flag that gates it", () => {
  // The restyle of 2026-09-14 (design.md, Header + list state v2) replaces a
  // grey `.is-selected` fill and three neutral hover washes with one accent
  // ring and one accent tint. `is-selected` is the LIST's own memory of where
  // the reader was and exists with the feature off, so a bare
  // `.tasks-row.is-selected` rule is not a peek selector at all — it would ship
  // the whole restyle to a reader who opted out.

  it("reaches nothing with the feature off: every rule names the host", () => {
    // `.tasks-peek-host` is rendered by Scheduled.tsx only when `peekable` —
    // the flag AND the unscoped /tasks route — so with the feature down the
    // wrapper does not exist and not one of these selectors can match.
    expect(PAGE).toContain('<div className="tasks-peek-host">');
    expect(PAGE).toContain("if (!peekable) return page;");
    const rules = PEEK_CSS.split("}")
      .map((chunk) => chunk.slice(chunk.lastIndexOf("*/") + 1).split("{")[0] ?? "")
      .filter((sel) => /is-peeked|is-selected|\.tasks-row:hover|card-head:hover|tv-card:hover/.test(sel));
    expect(rules.length).toBeGreaterThan(4);
    for (const sel of rules) {
      for (const one of sel.split(",")) {
        if (!one.trim()) continue;
        expect(one).toContain(".tasks-peek-host");
      }
    }
  });

  it("leaves main's grey selected row and hover EXACTLY where they were", () => {
    // What a flag-off reader still gets, word for word.
    expect(TASKS_CSS).toContain(".tasks-row:hover {\n  background: var(--row-bg-hover);\n}");
    expect(TASKS_CSS).toContain(
      ".tasks-row.is-selected,\n.tasks-row.is-selected:hover {\n  background: var(--row-bg-hover);\n}",
    );
    expect(TASKS_CSS).toContain(".tasks-row.is-inert:hover {\n  background: transparent;\n}");
    // The Cards wall's head has NO hover fill on either side of the flag
    // (Akshil, 2026-09-14: "when we hover on the heading, we shouldn't change
    // the color"); the fill went to the card last opened instead.
    expect(CARDS_CSS).not.toMatch(/\.task-card-head:hover\s*\{/);
    expect(CARDS_CSS).toContain(".task-card.is-selected .task-card-head {");
    expect(PEEK_CSS).not.toContain(".tasks-peek-host .task-card-head:hover");
    expect(SCHEDULE_CSS).toContain(
      "background: color-mix(in srgb, var(--fg) 6%, var(--tasks-card-bg));",
    );
  });

  it("is ONE look on all three surfaces once the flag is on", () => {
    // A FILL, not a ring (design.md, Polish batch 3). The tokens are the app's
    // own row pair — tokens.css has no `--bg-secondary`/`--bg-tertiary`, and
    // `--row-bg-selected` / `--row-bg-hover` are exactly the secondary and
    // tertiary row surfaces it does have.
    const active = block(PEEK_CSS, ".tasks-peek-host .tasks-row.is-peeked");
    // GREY, NOT ACCENT (design.md, Polish batch 4): `--row-bg-active` is the
    // app's accent wash, and a lime-tinted fill on the open row said "this
    // task is in a state" where it meant "this is the one you are reading".
    expect(active).toContain("background: var(--row-bg-selected)");
    expect(active).not.toContain("--row-bg-active");
    // The same declaration block names every surface, which is the only way
    // "identical" survives the next tuning.
    for (const surface of [
      ".tasks-peek-host .tasks-row.is-peeked",
      ".tasks-peek-host .schedule-tv-card.is-peeked",
      ".tasks-peek-host .task-card.is-peeked",
      // …and the wall card's HEAD, which is painted over the card itself: a
      // fill on the card alone showed nothing, which is the Cards-view miss
      // this batch was asked to fix.
      ".tasks-peek-host .task-card.is-peeked .task-card-head",
    ]) {
      expect(active).toContain(surface);
    }
    const hover = block(PEEK_CSS, ".tasks-peek-host .tasks-row:hover");
    expect(hover).toContain("background: var(--row-bg-hover)");
    expect(hover).toContain(".tasks-peek-host .schedule-tv-board .schedule-tv-card:hover");
    // …BUT NOT THE WALL CARD'S HEAD (Akshil, 2026-09-14: "when we hover on the
    // heading, we shouldn't change the color"). That strip has no hover fill
    // with the flag off, and the flag does not put one back — the card's own
    // press is the whole of its affordance.
    expect(hover).not.toContain(".tasks-peek-host .task-card-head:hover");
    // NO ACCENT RING left on any of the three.
    expect(active).not.toContain("--peek-halo");
    expect(hover).not.toContain("--peek-halo");
  });

  it("fills exactly ONE item — the second highlight is gone", () => {
    // `.is-selected` is the List's own memory of where the reader was, and with
    // a panel open it was a second answer to one question.
    const quiet = block(PEEK_CSS, ".tasks-peek-host .tasks-row.is-selected:not(.is-peeked)");
    expect(quiet).toContain("background: transparent");
    // …and it keeps the ordinary hover, so a row does not go dead under the
    // pointer just because it used to be the one you opened.
    expect(PEEK_CSS).toContain(".tasks-peek-host .tasks-row.is-selected:not(.is-peeked):hover,");
  });
});

describe("the preview's inset, and the page's gutters at width", () => {
  it("gives the preview one header-button of air, and scales to what is left", () => {
    // The app was welded to both walls of the panel while everything around it
    // was inset (design.md, Polish batch 4, item 7). One var, so the preview's
    // edge and the ×'s edge cannot drift apart.
    expect(PEEK_CSS).toContain("--peek-icon-w: 28px;");
    expect(PEEK_CSS).toContain("width: var(--peek-icon-w);");
    expect(PEEK_CSS).toContain("--peek-preview-inset: 38px;");
    expect(PEEK_CSS).toContain("padding-left: var(--peek-preview-inset);");
    expect(PEEK_CSS).toContain("padding-right: var(--peek-preview-inset);");
    // The ARITHMETIC half of the same number — padding on a scroller does not
    // shrink what is inside it, so the frame has to be drawn at the inner
    // width or the gutter simply crops the app.
    expect(read("peek-preview.ts")).toContain("export const PREVIEW_INSET = 38;");
    expect(read("peek-preview.ts")).toContain("const inner = peekWidth - 2 * PREVIEW_INSET;");
  });

  it("drops the page's gutters entirely when the frame is tight", () => {
    // Not the 12px inset of batch 3: an inset on the PAGE is a band down the
    // side of everything, scrollers included. The page gives its gutter up and
    // the 4px that keeps type off the edge goes on the blocks (Polish batch 4).
    expect(PEEK_CSS).toContain(
      '.tasks-frame[data-tight="1"] .schedule-page {\n  padding-left: 0;\n  padding-right: 0;\n}',
    );
    expect(PEEK_CSS).toContain(
      '.tasks-frame[data-tight="1"] .schedule-page > .schedule-header,\n' +
        '.tasks-frame[data-tight="1"] .schedule-page > .prefs-section {\n  padding: 0 4px;\n}',
    );
    // …and the untight gutter the baseline is frozen from is STILL only ever
    // declared on the base rule (Bugbot, PR #1141).
    expect(PEEK_CSS).not.toContain("--tasks-page-gutter:"); // named, never redeclared
    expect(SCHEDULE_CSS).toContain("--tasks-page-gutter: 44px;");
  });
});

describe("the middle pane's floor", () => {
  // SOURCE checks, for the same reason the rest of this file uses them: the
  // floor is one attribute and one custom property written by the page and read
  // by four selectors, and what can go wrong is the two halves drifting apart —
  // a switch nothing listens for, or a rule keyed on an attribute nobody
  // writes. Neither needs a mounted list to catch.
  it("writes the switch and the number onto the frame, and only there", () => {
    expect(PAGE).toContain('data-floored={peek.floored ? "1" : undefined}');
    expect(PAGE).toContain('"--tasks-floor": `${contentFloor}px`');
    // The number is a CONTENT width: the frame's ¾ baseline counts the page's
    // gutters, and the views live inside them.
    expect(PAGE).toContain("peek.floor - peekGutter()");
  });

  it("keeps the seam reachable in cover mode — it is the only way back", () => {
    // Bugbot, PR #1138: hiding it left a covered page with no control that
    // makes the panel narrower, so the reader's only exit was closing the task.
    const block = PEEK_CSS.slice(
      PEEK_CSS.indexOf(".task-side-peek.is-cover .task-side-peek-seam {"),
    ).slice(0, 400);
    expect(block).not.toContain("display: none");
    expect(block).toContain("width: calc(var(--peek-seam-w)");
    // …and the arrows step the DRAGGED width, not the rendered one, which in
    // cover is the whole content area and never gets smaller.
    expect(read("TaskPeek.tsx")).toContain(
      "const from = getPeekState().width ?? currentRoom().peekWidth;",
    );
  });

  it("scrolls each view sideways at the floor instead of reflowing it", () => {
    for (const view of [".tasks-list", ".schedule-cal"]) {
      // Each view is named twice: once as a scroller, once for the content
      // inside it that stops shrinking.
      expect(PEEK_CSS).toContain(`.tasks-frame[data-floored="1"] ${view} > *`);
      const scrollers = PEEK_CSS.slice(
        PEEK_CSS.indexOf('.tasks-frame[data-floored="1"] .tasks-list,'),
      ).slice(0, 160);
      expect(scrollers).toContain(`.tasks-frame[data-floored="1"] ${view}`);
    }
    // THE CARDS WALL IS DELIBERATELY NOT ONE OF THEM (design.md, Polish batch
    // 3): a grid of conversations answers a narrower pane by dropping a column,
    // which a list of rows cannot do. It is pinned closed rather than left
    // unmentioned, because "no rule" and "a rule that says never" read the same
    // in a diff and only one of them survives a refactor.
    expect(PEEK_CSS).toContain('.tasks-frame[data-floored="1"] .task-cards-scroll {\n  overflow-x: hidden;');
    expect(PEEK_CSS).toContain("grid-template-columns: repeat(auto-fill, minmax(min(var(--task-card-min), 100%), 1fr));");
    expect(PEEK_CSS).toContain("overflow-x: auto;");
    expect(PEEK_CSS).toContain("min-width: var(--tasks-floor, 0px);");
  });

  it("leaves the toolbar out of it — the toolbar is exempt at every width", () => {
    // A toolbar that scrolled sideways would put New task somewhere you have to
    // go looking for it (design.md, Widths v2). It folds and hides instead.
    expect(PEEK_CSS).not.toContain('[data-floored="1"] .schedule-toolbar');
  });

  it("never lets the page itself scroll sideways", () => {
    // design-principles §0. The frame's host clips, and every scroller above is
    // a view inside it.
    const host = PEEK_CSS.slice(PEEK_CSS.indexOf(".tasks-peek-host {"));
    expect(host.slice(0, host.indexOf("}"))).toContain("overflow: hidden");
  });
});

describe("the stylesheet changes nothing when the feature is off", () => {
  // Every rule that alters how the toolbar LAYS OUT is scoped to `data-fit`,
  // which shell/row-fit.ts writes only while the feature is on. Unscoped, these
  // changed the page for readers who had opted out: the search floored at 72px
  // where it used to collapse to nothing, the row stopped shrinking, and the
  // filter group's minimum moved.
  for (const rule of [
    ".schedule-toolbar[data-fit] > .schedule-view-seg",
    ".schedule-toolbar[data-fit] {",
    ".schedule-toolbar[data-fit] .schedule-tv-filters {",
    ".schedule-toolbar[data-fit] .schedule-tv-search {",
  ]) {
    it(`scopes \`${rule}\` to the flag's attribute`, () => {
      expect(SCHEDULE_CSS).toContain(rule);
    });
  }

  it("scopes every HIDE rung to a `data-fit` value the flag alone writes", () => {
    // The ladder's second half (design.md, Widths v2) takes controls off the
    // row. Every one of those rules names an explicit `[data-fit="N"]`, so a
    // toolbar with no attribute at all — the flag-off page — matches none of
    // them and keeps all four views, both filters and its search.
    for (const rung of [
      ".schedule-tv-pop-wrap",
      ".schedule-tv-search",
      '.schedule-view-btn[data-view="calendar"]',
      '.schedule-view-btn[data-view="cards"]',
      '.schedule-view-btn[data-view="board"]',
    ]) {
      const at = SCHEDULE_CSS.indexOf(`${rung} {\n  display: none;`);
      if (at < 0) continue;
      const head = SCHEDULE_CSS.slice(Math.max(0, at - 400), at);
      expect(head).toContain('[data-fit="');
    }
  });

  it("leaves the base toolbar, filter group and search as they were", () => {
    const base = (selector: string) => {
      const at = SCHEDULE_CSS.indexOf(`\n${selector} {`);
      expect(at).toBeGreaterThan(-1);
      return SCHEDULE_CSS.slice(at, SCHEDULE_CSS.indexOf("}", at));
    };
    // No wrap, no flex pinning, no overflow clip on the bare selector.
    const toolbar = base(".schedule-toolbar");
    expect(toolbar).not.toContain("flex-wrap");
    expect(toolbar).not.toContain("overflow");
    expect(toolbar).not.toContain("flex:");
    // The filter group keeps the `min-width: 0` it has always had…
    expect(base(".schedule-tv-filters")).toContain("min-width: 0");
    // …and the search its plain 260px width with no floor.
    const search = base(".schedule-tv-search");
    expect(search).toContain("width: 260px");
    expect(search).toContain("min-width: 0");
    expect(search).not.toContain("--fit-natural");
  });

  it("folds no label without the attribute", () => {
    // Every fold rule names `data-fit`; a `.schedule-fit-lbl` in an off page is
    // a span with no rule pointing at it.
    // Per RULE, not per line: the selectors are multi-line, and the attribute
    // is on the first of them.
    const rules = SCHEDULE_CSS.split("}")
      .map((chunk) => chunk.slice(chunk.lastIndexOf("*/") + 1))
      .filter((chunk) => chunk.includes(".schedule-fit-lbl"));
    expect(rules.length).toBeGreaterThan(0);
    for (const rule of rules) {
      expect(rule).toContain("data-fit");
      // AND THE ATTRIBUTE, not just a negation of one of its values:
      // `:not([data-fit="0"])` on its own matches a toolbar that has no
      // attribute at all — the flag-off page — and folded its labels
      // (measured live, 2026-09-13).
      expect(rule).not.toMatch(/\.schedule-toolbar:not\(/);
    }
  });

  it("every `:not([data-fit=…])` in the tasks stylesheets is preceded by a `[data-fit]` presence guard", () => {
    // Same trap, other file: `.tasks-list:not([data-fit="0"]) .tasks-row-time`
    // hid every row's age for readers with the flag OFF (no attribute at all
    // matches the negation). Found in review, 2026-09-13.
    for (const rel of ["../styles/schedule.css", "../styles/tasks.css", "../styles/task-peek.css"]) {
      const css = read(rel).replace(/\/\*[\s\S]*?\*\//g, "");
      const bare = css.match(/[^\]]:not\(\[data-fit/g) ?? [];
      expect({ file: rel, bare }).toEqual({ file: rel, bare: [] });
    }
  });
});
