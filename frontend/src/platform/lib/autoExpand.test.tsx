// The visibility rules for a status-bar section, tested at the hook rather
// than through a dock: `ModelsDock`'s own tests cover only its pure view (the
// stateful half needs `useAiRuntime`), and these rules are where the two
// defects that actually shipped on this branch lived.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useAutoExpandOnNew, type AutoExpandOptions } from "./autoExpand";

/** What the caller would compute as `open`, plus the raw flags, recorded on
 *  every render so a test can assert the sequence rather than just the end. */
interface Seen {
  open: boolean;
  hasNew: boolean;
  autoOpen: boolean;
  autoClose: boolean;
}

function harness(opts: AutoExpandOptions = {}) {
  const seen: Seen[] = [];
  function Section({ ids, collapsed }: { ids: string[]; collapsed: boolean }) {
    const { hasNew, autoOpen, autoClose } = useAutoExpandOnNew(ids, collapsed, true, opts);
    // The exact rule every dock uses.
    seen.push({
      open: autoClose ? false : !collapsed || autoOpen,
      hasNew,
      autoOpen,
      autoClose,
    });
    return null;
  }
  let renderer!: ReactTestRenderer;
  const render = (ids: string[], collapsed: boolean) => {
    act(() => {
      if (renderer) renderer.update(<Section ids={ids} collapsed={collapsed} />);
      else renderer = create(<Section ids={ids} collapsed={collapsed} />);
    });
  };
  return { seen, render, last: () => seen[seen.length - 1], unmount: () => act(() => renderer.unmount()) };
}

// ---------------------------------------------------------------- the default

test("an arrival into a collapsed section opens it and sets no dot beside it", () => {
  const h = harness();
  h.render(["a"], true); // seeds
  h.render(["a", "b"], true);
  expect(h.last().autoOpen).toBe(true);
  expect(h.last().open).toBe(true);
  h.unmount();
});

// D585 FINDING 1, the HIGH one: the arrival branch used to gate on the
// PERSISTED `collapsed`. On a default install there is no stored key, so
// `collapsed === false` — and once a drain had set the `"closed"` override, the
// next arrival matched neither `collapsed` nor anything that clears it, leaving
// the section permanently deaf with no panel AND no dot.
test("a drained section still hears the next arrival — no permanent deafness", () => {
  const h = harness();
  h.render(["a"], false); // seeds, panel open by preference
  h.render([], false); // drains -> auto-closes
  expect(h.last().autoClose).toBe(true);
  expect(h.last().open).toBe(false);

  h.render(["b"], false); // a NEW job arrives into the closed section
  expect(h.last().open).toBe(true);
  expect(h.last().autoClose).toBe(false);
  h.unmount();
});

// ---------------------------------------------------------------- neverOpen (Models)

// D587, user: "the models popover should never auto open. that is user only".
test("neverOpen sets the dot and leaves the panel shut", () => {
  const h = harness({ neverOpen: true });
  h.render(["a"], true); // seeds
  h.render(["a", "b"], true);
  expect(h.last().hasNew).toBe(true);
  expect(h.last().autoOpen).toBe(false);
  expect(h.last().open).toBe(false);
  h.unmount();
});

// KNOCK-ON of finding 1 (coordinator: "verify that specific case rather than
// reasoning about it"). Fixing the deafness must NOT hand Models an auto-open
// path: it has to go from "closed" to "closed with a dot", never to "open".
test("a drained neverOpen section goes to closed-with-a-dot, never to open", () => {
  const h = harness({ neverOpen: true });
  h.render(["a"], false); // seeds, open by preference
  h.render([], false); // drains -> auto-closes (kept: closing is not opening)
  expect(h.last().open).toBe(false);

  h.render(["b"], false); // a new model becomes resident
  expect(h.last().open).toBe(false);
  expect(h.last().autoOpen).toBe(false);
  expect(h.last().hasNew).toBe(true);
  h.unmount();
});

test("neverOpen KEEPS auto-close on drain — D580 was explicitly good", () => {
  const h = harness({ neverOpen: true });
  h.render(["a"], false); // seeds, open
  h.render([], false); // Unload on the last row
  expect(h.last().autoClose).toBe(true);
  expect(h.last().open).toBe(false);
  h.unmount();
});

// ---------------------------------------------------------------- neverClose (failures)

// D586: a failure must touch visibility in NEITHER direction — it may not throw
// a panel over the page, and an emptying error list must not shut a panel the
// repo rows are still filling.
test("neverOpen + neverClose leaves visibility alone in both directions", () => {
  const h = harness({ neverOpen: true, neverClose: true });
  h.render(["a"], false); // seeds, open by preference
  h.render([], false); // the error list drains
  expect(h.last().autoClose).toBe(false);
  expect(h.last().open).toBe(true); // the panel the repo rows are filling stays

  // Collapse FIRST, as its own step: changing the saved preference is the user
  // speaking directly and deliberately clears any standing dot/override, so
  // flipping `collapsed` in the same render as an arrival would wipe the very
  // dot this asserts (real behaviour, not a quirk worth testing around).
  h.render([], true);
  h.render(["b"], true); // a failure arrives while collapsed
  expect(h.last().hasNew).toBe(true);
  expect(h.last().open).toBe(false);
  h.unmount();
});
