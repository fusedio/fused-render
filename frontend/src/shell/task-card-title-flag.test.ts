// THE FLAG'S CONTRACT (design.md §10 + §A): with `task_card_last_message` off,
// a task card is byte-for-byte the card the Cards wall has always drawn — and
// the head's hover wash is gone for everyone, flag or no flag (§8).
//
// Two kinds of check, the peek flag's own pair (task-peek-flag.test.ts):
//
//   * BEHAVIOURAL, for the decision — `cardTitleLine` is pure, so the one rule
//     the flag gates (which line the title row shows, and whether the id leads)
//     can be exercised without mounting a wall of live chats;
//   * SOURCE, for the markup and the stylesheet — every mark the experiment
//     adds is written behind the flag's value, and the selected fill it hands
//     the hover's wash to is a class the card wears, not a `:hover`.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Task } from "@platform/lib/api";

const SHELL = new URL(".", import.meta.url).pathname;
const read = (rel: string) => readFileSync(join(SHELL, rel), "utf8");
const FLAG = read("task-card-title-flag.ts");
const CARDS = read("TaskCards.tsx");
const CARDS_CSS = read("../styles/task-cards.css");
const VIEWS = read("ScheduleTaskViews.tsx");
const TASKS_CSS = read("../styles/tasks.css");

const { cardTitleLine } = await import("./task-card-title-flag");

function task(fields: Partial<Task>): Task {
  return { title: "Fix login redirect", ...fields } as Task;
}

describe("the line a card is titled by", () => {
  it("is the task's title with the experiment off, whatever was said", () => {
    const shown = cardTitleLine(
      task({ last_message: { role: "assistant", text: "Ran the migration", at: 2 } }),
      false,
    );
    expect(shown).toEqual({ text: "Fix login redirect", said: false });
  });

  it("is the newest message with it on — whoever said it", () => {
    for (const role of ["user", "assistant"] as const) {
      expect(cardTitleLine(task({ last_message: { role, text: "Ran it", at: 2 } }), true))
        .toEqual({ text: "Ran it", said: true });
    }
  });

  it("falls back to the title when nothing has been said yet", () => {
    // A task that has never run, and a server that predates the field: both
    // must draw the card the wall has always drawn rather than a blank line.
    expect(cardTitleLine(task({ last_message: null }), true))
      .toEqual({ text: "Fix login redirect", said: false });
    expect(cardTitleLine(task({}), true))
      .toEqual({ text: "Fix login redirect", said: false });
    // …including a message the server cut down to nothing.
    expect(cardTitleLine(task({ last_message: { role: "user", text: " ", at: 2 } }), true))
      .toEqual({ text: "Fix login redirect", said: false });
  });

  it("takes ONE line of a message, the way it takes one line of a title", () => {
    expect(cardTitleLine(
      task({ last_message: { role: "assistant", text: "\n  Ran it\nthen stopped", at: 2 } }),
      true,
    )).toEqual({ text: "Ran it", said: true });
  });

  it("says \"\" for a task with neither, so the card words its own blank", () => {
    expect(cardTitleLine(task({ title: "" }), true)).toEqual({ text: "", said: false });
  });
});

describe("the flag module", () => {
  it("is the peek flag's idiom, down to the tri-state", () => {
    expect(FLAG).toContain("export function useTaskCardTitleFlag(): boolean | null");
    expect(FLAG).toContain("export function useTaskCardTitleMode(): boolean");
    expect(FLAG).toContain("export function publishTaskCardTitleMode");
    expect(FLAG).toContain("export function resetTaskCardTitleFlagForTests");
    // One shared read, a generation guard, and a bounded retry — the three
    // things that make two mounts cost one GET.
    expect(FLAG).toContain("let reading: Promise<void> | null = null;");
    expect(FLAG).toContain("let generation = 0;");
    expect(FLAG).toContain(".catch(() => getPrefs())");
  });

  it("reads the pref strictly: only a stored true is on", () => {
    expect(FLAG).toContain("p.task_cards?.last_message === true");
  });

  it("settles a failed read on OFF rather than leaving it unknown", () => {
    expect(FLAG).toContain("if (generation === departed) set(false);");
  });
});

describe("the card adds nothing when the experiment is off", () => {
  it("spends the flag on the VALUE, once for the whole wall", () => {
    // Not per card and not inside a condition: `useTaskCardTitleMode` is a
    // hook, and a hook that appears between two renders is what React throws
    // on (the peek's own note, PR #1133).
    expect(CARDS).toContain("const titleMode = useTaskCardTitleMode();");
    expect(CARDS).not.toMatch(/\?\s*useTaskCardTitleMode\(\)/);
  });

  it("asks one function for the line, so `off` cannot mean two things", () => {
    expect(CARDS).toContain("const line = cardTitleLine(task, titleMode);");
    expect(CARDS).toContain('const title = line.text || "(untitled)";');
  });

  it("lifts the id only when the line is a message", () => {
    expect(CARDS).toContain('(line.said ? " task-card-id--lead" : "")');
    expect(CARDS).toContain("data-hint={line.said ? task.last_message?.text : task.title}");
  });
});

describe("the head's hover, and the card the reader last opened (§8)", () => {
  it("draws no hover wash on the head at all", () => {
    // For everyone, flag or no flag: the head already sits a step above the
    // card, and a wall of six lighting up under a passing pointer promised
    // something six times for every time it was true.
    expect(CARDS_CSS).not.toContain(".task-card-head:hover {");
    // …and the pointer and the focus ring, which say the same thing honestly,
    // stay exactly where they were — as does the doors strip the head's hover
    // still reveals, which is a reveal and not a wash.
    expect(CARDS_CSS).toContain(".task-card-head:focus-visible {");
    expect(CARDS_CSS).toContain(".task-card-head:hover .task-card-doors,");
    expect(CARDS_CSS).toContain("cursor: pointer;");
  });

  it("spends that fill on the selected card instead — the List row's mark", () => {
    expect(CARDS_CSS).toContain(".task-card.is-selected .task-card-head {");
    expect(CARDS_CSS).toContain("background: color-mix(in srgb, var(--fg) 5%, var(--row-bg-hover));");
  });

  it("remembers it per tab, and writes it on the press that opens the task", () => {
    // sessionStorage, the List's own idiom for the same claim — and both the
    // head's press and the card's go through `openTask`, so one gesture cannot
    // light a card the other would not.
    expect(CARDS).toContain('const SELECTED_KEY = "tasks.cards.selected";');
    expect(CARDS).toContain("sessionStorage.getItem(SELECTED_KEY)");
    expect(CARDS).toMatch(/const openTask = \(task: Task\) => \{[\s\S]*writeSelectedCard\(key\);/);
    expect(CARDS).toContain('(selected ? " is-selected" : "")');
  });
});

describe("EVERY view is titled by the same rule (Akshil, 2026-09-14)", () => {
  // The flag shipped on the Cards wall alone, and a reader who turned it on and
  // opened the List saw the old title — which reads as a switch that did not
  // take rather than as a switch that is scoped. One pref, one rule, three
  // views (design-principles §1: a lens, not three pages).

  it("spends the flag once per VIEW, never per row or per card", () => {
    // A hook that appeared per row would be a hook count that moves with the
    // filter, which is what React throws on — the Cards wall's own note.
    expect((VIEWS.match(/const titleMode = useTaskCardTitleMode\(\);/g) ?? []).length)
      .toBe(2);
    expect(VIEWS).not.toMatch(/\?\s*useTaskCardTitleMode\(\)/);
    // …and handed down as a value, with OFF as the default, so a caller that
    // predates the flag renders the row this page has always rendered.
    expect(VIEWS).toContain("titleMode = false,");
    expect((VIEWS.match(/titleMode=\{titleMode\}/g) ?? []).length).toBe(2);
  });

  it("asks `cardTitleLine` for the line in the List row and the Board card", () => {
    expect((VIEWS.match(/const line = cardTitleLine\(task, titleMode\);/g) ?? []).length)
      .toBe(2);
    // The blank is worded once per view, as it always was, and off the same
    // value — so a task with neither a message nor a title cannot read three
    // ways.
    expect(VIEWS).toContain('const label = line.text || "(untitled)";');
    expect(VIEWS).toContain('{line.text || "(untitled)"}');
    expect(VIEWS).not.toContain('firstLine(task.title) || "(untitled)"');
  });

  it("lifts the id only when the line is a message, in both views", () => {
    expect((VIEWS.match(/<IdChip id=\{task\.task_id\} kind="task" lead=\{line\.said\} \/>/g) ?? [])
      .length).toBe(2);
    // One chip decides what "leading" looks like, so the row and the card
    // cannot lift it by different amounts.
    expect(VIEWS).toContain('(lead ? " tasks-id--lead" : "")');
    // …and it is the CARDS wall's lift, to the character: weight and colour
    // only, at the same size.
    const lead = TASKS_CSS.slice(TASKS_CSS.indexOf(".tasks-id--lead {"));
    expect(lead).toContain("font-weight: 600;");
    expect(lead).toContain("color: var(--fg);");
    expect(CARDS_CSS.slice(CARDS_CSS.indexOf(".task-card-id--lead {")))
      .toContain("font-weight: 600;");
  });

  it("captions whatever the line is one line OF", () => {
    // The point of the caption is the text the one-line clamp is hiding, so a
    // row showing a message has to caption the message and not the title.
    expect(VIEWS).toContain(
      "data-hint={line.said ? task.last_message?.text : task.title}");
    expect(VIEWS).toMatch(
      /data-hint=\{lockedDraft\s*\?\s*"Finish the draft to run it\."\s*:\s*\(line\.said \? task\.last_message\?\.text : task\.title\)\}/);
  });

  it("says so in the pref's own words — it is not a Cards setting", () => {
    const PREFS = read("Preferences.tsx");
    expect(PREFS).toContain("<h2>Tasks: show last message as the title</h2>");
    // The KEY is untouched: the pref is the same pref, only its label was
    // scoped wrong (`task_card_last_message`, shell/prefs.py).
    expect(PREFS).toContain("prefs.task_cards?.last_message ?? false");
  });
});
