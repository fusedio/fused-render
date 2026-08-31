// The visibility rules for a status-bar section, tested at the hook rather
// than through a dock. VISIBILITY IS ALL THIS HOOK DOES since D588 deleted
// `hasNew` — each chip draws one circle off its own list, so there is no
// "something new" state left here to test.
//
// Rather than through a dock: the (former) `ModelsDock`'s own tests used to cover only its pure view (the
// stateful half needs `useAiRuntime`), and these rules are where the two
// defects that actually shipped on this branch lived.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useAutoExpandOnNew, type AutoExpandOptions } from "./autoExpand";

/** What the caller would compute as `open`, plus the raw flags, recorded on
 *  every render so a test can assert the sequence rather than just the end. */
interface Seen {
  open: boolean;
  autoOpen: boolean;
  autoClose: boolean;
}

function harness(opts: AutoExpandOptions = {}) {
  const seen: Seen[] = [];
  function Section({
    ids,
    collapsed,
    alsoDrawn,
  }: {
    ids: string[];
    collapsed: boolean;
    /** The panel's OTHER row source, when the test is about one (finding 1). */
    alsoDrawn?: string[];
  }) {
    const { autoOpen, autoClose } = useAutoExpandOnNew(ids, collapsed, true, {
      ...opts,
      ...(alsoDrawn ? { alsoDrawn } : {}),
    });
    // The exact rule every dock uses.
    seen.push({
      open: autoClose ? false : !collapsed || autoOpen,
      autoOpen,
      autoClose,
    });
    return null;
  }
  let renderer!: ReactTestRenderer;
  const render = (ids: string[], collapsed: boolean, alsoDrawn?: string[]) => {
    const el = <Section ids={ids} collapsed={collapsed} alsoDrawn={alsoDrawn} />;
    act(() => {
      if (renderer) renderer.update(el);
      else renderer = create(el);
    });
  };
  return { seen, render, last: () => seen[seen.length - 1], unmount: () => act(() => renderer.unmount()) };
}

// ---------------------------------------------------------------- the default

test("an arrival into a collapsed section opens it", () => {
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

// `neverOpen` (the flag the two now-deleted standalone Models/Engines chips
// passed here, D587: "the models popover should never auto open. that is
// user only") was deleted along with them — nothing calls this hook wanting
// an announce-suppressed-but-occupying source any more; the status-bar merge
// folded both into Activity's `alsoDrawn`, which the tests below already
// cover (occupancy without announcing, and a genuine drain-close).

// ------------------------------------------------- alsoDrawn (a second source)

// CODE REVIEW 2026-08-28, FINDING 1. The drain gate means "the panel is
// genuinely empty", but both real panels draw rows from TWO sources and the
// hook could only see one of them — so the first source emptying force-closed a
// panel that was still full. `alsoDrawn` is the other source's identities:
// occupancy only, never an announcement. `neverClose` (which used to be how the
// failures half was papered over) is deleted, because this is what its caller
// actually wanted.
test("a drain of `ids` does NOT close a panel `alsoDrawn` is still filling", () => {
  const h = harness();
  h.render(["a"], false, ["x"]); // seeds, open by preference, both sources full
  h.render([], false, ["x"]); // the FIRST source drains; the other still draws
  expect(h.last().autoClose).toBe(false);
  expect(h.last().open).toBe(true);
  h.unmount();
});

// The mirror: the transition still has to fire, and it has to fire on whichever
// source empties LAST. Tracking the seen set over the union is what buys this —
// a hook that tracked only `ids` would have shrunk `prev` to empty on the tick
// above and then had nothing left to notice.
test("the panel closes once BOTH sources have drained, whichever went last", () => {
  const h = harness();
  h.render(["a"], false, ["x"]); // seeds, open, both full
  h.render([], false, ["x"]); // ids drain — no close (above)
  expect(h.last().open).toBe(true);
  h.render([], false, []); // and now the other source goes too
  expect(h.last().autoClose).toBe(true);
  expect(h.last().open).toBe(false);
  h.unmount();
});

// D586's promise, now STRUCTURAL rather than a flag: a failure reaching
// Notifications fills the circle and holds the panel, but must never throw a
// panel over the page the user is looking at.
test("an arrival in `alsoDrawn` never opens the panel", () => {
  const h = harness();
  h.render([], true, ["x"]); // seeds, collapsed
  h.render([], true, ["x", "y"]); // a second failure lands
  expect(h.last().autoOpen).toBe(false);
  expect(h.last().open).toBe(false);
  h.unmount();
});

// An arrival among `ids` is still news even while `alsoDrawn` is occupied —
// widening the occupancy set must not narrow the announce set by putting the
// other source's rows into the same "already seen" bucket.
test("an arrival in `ids` still opens the panel while `alsoDrawn` is full", () => {
  const h = harness();
  h.render(["a"], true, ["x"]); // seeds, collapsed
  h.render(["a", "b"], true, ["x"]);
  expect(h.last().autoOpen).toBe(true);
  expect(h.last().open).toBe(true);
  h.unmount();
});
