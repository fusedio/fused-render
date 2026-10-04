// THE NOTE COMPOSER'S KEYS, on the node `buildPopNode` actually builds.
//
// Everything about this card is imperative and lives on the node (the reason is
// at the top of `AnnPopover.tsx`: while it is portaled into the app it is
// outside React's root container, where delegated events never fire). So the
// keys cannot be tested through the component — they are tested by BUILDING the
// node over a fake document and dispatching at the textarea's own listener,
// which is the listener the reader's keystroke reaches.
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import { expect, test } from "bun:test";

const { buildPopNode } = await import("./AnnPopover");
const { ANN_DONE_CHORD } = await import("./types");
const { isMac } = await import("@platform/lib/platform");

interface FakeNode {
  tagName: string;
  className: string;
  id: string;
  type: string;
  hidden: boolean;
  rows: number;
  value: string;
  placeholder: string;
  spellcheck: boolean;
  textContent: string;
  style: Record<string, string>;
  children: FakeNode[];
  ownerDocument: unknown;
  handlers: Map<string, ((e: unknown) => void)[]>;
  append(...kids: FakeNode[]): void;
  appendChild(kid: FakeNode): void;
  addEventListener(type: string, fn: (e: unknown) => void): void;
  contains(): boolean;
  focus(): void;
}

/** Enough of a document for the card: `createElement`, and an `activeElement`
 *  for the refocus half of the portaled-keystroke guard. */
function fakeDoc() {
  const doc: Record<string, unknown> = { activeElement: null };
  doc.createElement = (tag: string): FakeNode => {
    const node: FakeNode = {
      tagName: tag.toUpperCase(),
      className: "",
      id: "",
      type: "",
      hidden: false,
      rows: 0,
      value: "",
      placeholder: "",
      spellcheck: false,
      textContent: "",
      style: {},
      children: [],
      ownerDocument: doc,
      handlers: new Map(),
      append(...kids) {
        node.children.push(...kids);
      },
      appendChild(kid) {
        node.children.push(kid);
      },
      addEventListener(type, fn) {
        const list = node.handlers.get(type) ?? [];
        list.push(fn);
        node.handlers.set(type, list);
      },
      contains: () => false,
      focus: () => {},
    };
    return node;
  };
  return doc as unknown as Document;
}

interface Log {
  commits: string[];
  closes: number;
  dels: number;
  dones: number;
}

function build() {
  const log: Log = { commits: [], closes: 0, dels: 0, dones: 0 };
  const doc = fakeDoc();
  const pop = buildPopNode(doc, {
    commit: (text) => log.commits.push(text),
    close: () => void log.closes++,
    del: () => void log.dels++,
    doneRound: () => void log.dones++,
  }) as unknown as FakeNode;
  const ta = pop.children[0]!;
  const hint = pop.children[1]!;
  return { log, pop, ta, hint };
}

/** Spelled for whichever platform the suite runs on — `isMod` is exclusive, so
 *  a hard-coded `metaKey` would pass on a Mac and assert nothing in CI. */
function press(ta: FakeNode, over: Record<string, unknown> = {}) {
  let prevented = 0;
  let stopped = 0;
  const e = {
    key: "Enter",
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    preventDefault: () => void prevented++,
    stopPropagation: () => void stopped++,
    ...over,
  };
  for (const fn of ta.handlers.get("keydown") ?? []) fn(e);
  return { prevented, stopped };
}

const MOD = isMac ? { metaKey: true } : { ctrlKey: true };

test("⌘↩ in the card is ✓ Done, and it does NOT also save", () => {
  const { log, ta } = build();
  ta.value = "make this blue";
  const { prevented, stopped } = press(ta, MOD);
  expect(log.dones).toBe(1);
  // NOT committed here: `done()` is the one writer that commits the open draft
  // and it re-reads this very textarea, so a save on this path would be a
  // second chance for the two to disagree about what an empty card is.
  expect(log.commits).toEqual([]);
  expect(prevented).toBe(1);
  // The bubble is stopped so the document listeners, which answer the same
  // chord, cannot finish the round a second time.
  expect(stopped).toBe(1);
});

test("a bare Enter is still the SAVE, and Shift+Enter is still a newline", () => {
  const { log, ta } = build();
  ta.value = "make this blue";
  press(ta);
  expect(log.commits).toEqual(["make this blue"]);
  expect(log.dones).toBe(0);

  press(ta, { shiftKey: true });
  expect(log.commits).toHaveLength(1);
  expect(log.dones).toBe(0);
});

test("the wrong modifier is neither key", () => {
  const { log, ta } = build();
  // Ctrl+Enter on a Mac, Cmd+Enter off one: `isMod` rejects it, and the save
  // branch below it asks for no modifier at all — so it saves. That is the
  // pre-existing behaviour of every modified Enter and is left alone.
  press(ta, isMac ? { ctrlKey: true } : { metaKey: true });
  expect(log.dones).toBe(0);
});

test("the hint line teaches the chord", () => {
  const { hint } = build();
  expect(hint.textContent).toContain(`${ANN_DONE_CHORD} to finish`);
  expect(hint.textContent).toContain("Enter to save");
  expect(hint.textContent).toContain("Esc to cancel");
});

test("Escape still closes the card and claims the press", () => {
  const { log, ta } = build();
  const { stopped } = press(ta, { key: "Escape" });
  expect(log.closes).toBe(1);
  expect(stopped).toBe(1);
});
