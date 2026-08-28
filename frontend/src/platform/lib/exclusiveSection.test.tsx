// D582: one status-bar panel open at a time. The cases worth pinning are the
// ones that are NOT obvious — a tie inside a single commit, and a later request
// beating an earlier one — because both were previously at the mercy of
// effect-execution order.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useExclusiveSection, type SectionKey } from "./exclusiveSection";

/** A stand-in for one bar section: declares whether it wants to be open and
 *  records every time the arbiter closes it. */
function Section({
  sectionKey,
  wantOpen,
  onForceClose,
}: {
  sectionKey: SectionKey;
  wantOpen: boolean;
  onForceClose: () => void;
}) {
  useExclusiveSection(sectionKey, wantOpen, onForceClose);
  return null;
}

/** Mount inside `act` — react-test-renderer only flushes passive effects
 *  there, and the arbitration lives in one, so a bare `create()` would
 *  register nothing at all. */
function mount(element: React.ReactElement): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(element);
  });
  return renderer;
}

/** Advance past the current commit so the next request gets a higher tick
 *  rather than tying with what already happened. */
async function nextTick() {
  await act(async () => {
    await Promise.resolve();
  });
}

// The RELOAD case: two persisted preferences both say open. Models is a
// legitimate winner here — D587 forbids Models auto-OPENING, not being open,
// and a saved preference is the user's own choice.
test("two sections wanting open in the SAME commit resolve to Models, deterministically", () => {
  const closed: SectionKey[] = [];
  const renderer = mount(
    <>
      <Section sectionKey="models" wantOpen onForceClose={() => closed.push("models")} />
      <Section sectionKey="jobs" wantOpen onForceClose={() => closed.push("jobs")} />
    </>,
  );
  // Same commit means the same tick, so this is a genuine tie and SECTION_ORDER
  // breaks it — Models first. The loser is force-closed; the winner is not
  // touched at all, because the arbiter only ever closes.
  expect(closed).toEqual(["jobs"]);
  act(() => renderer.unmount());
});

test("a reload where all three preferences say open still leaves only Models", () => {
  const closed: SectionKey[] = [];
  const renderer = mount(
    <>
      <Section sectionKey="models" wantOpen onForceClose={() => closed.push("models")} />
      <Section sectionKey="jobs" wantOpen onForceClose={() => closed.push("jobs")} />
      <Section
        sectionKey="notifications"
        wantOpen
        onForceClose={() => closed.push("notifications")}
      />
    </>,
  );
  expect(closed).not.toContain("models");
  expect(new Set(closed)).toEqual(new Set(["jobs", "notifications"]));
  act(() => renderer.unmount());
});

test("a LATER request beats whatever was already open, whichever section it is", async () => {
  const closed: SectionKey[] = [];
  function Bar({ jobsOpen }: { jobsOpen: boolean }) {
    return (
      <>
        <Section sectionKey="models" wantOpen onForceClose={() => closed.push("models")} />
        <Section sectionKey="jobs" wantOpen={jobsOpen} onForceClose={() => closed.push("jobs")} />
      </>
    );
  }
  const renderer = mount(<Bar jobsOpen={false} />);
  expect(closed).toHaveLength(0);

  await nextTick();
  act(() => renderer.update(<Bar jobsOpen />));

  // Recency wins: Models closes even though it is first in SECTION_ORDER,
  // because SECTION_ORDER only ever breaks ties.
  expect(closed).toEqual(["models"]);
  act(() => renderer.unmount());
});

test("a section that merely STAYS open cannot keep out-bidding a sibling", async () => {
  const closed: SectionKey[] = [];
  function Bar({ n, jobsOpen }: { n: number; jobsOpen: boolean }) {
    return (
      <>
        <Section sectionKey="models" wantOpen onForceClose={() => closed.push("models")} />
        <Section sectionKey="jobs" wantOpen={jobsOpen} onForceClose={() => closed.push("jobs")} />
        {/* Re-renders without changing either section's want. */}
        <span>{n}</span>
      </>
    );
  }
  const renderer = mount(<Bar n={0} jobsOpen={false} />);
  // Several commits pass with Models simply remaining open. A tick is stamped
  // only on the false -> true edge, so none of these re-stamps it.
  for (const n of [1, 2, 3]) {
    await nextTick();
    act(() => renderer.update(<Bar n={n} jobsOpen={false} />));
  }
  await nextTick();
  act(() => renderer.update(<Bar n={4} jobsOpen />));

  expect(closed).toEqual(["models"]);
  act(() => renderer.unmount());
});

// The AUTO-OPEN case, narrowed by D587: Models has no auto-open path any more
// (`autoExpand.ts`'s `neverOpen`), so a same-tick auto-open contest is only
// ever Jobs vs Notifications, and Jobs is the first ELIGIBLE section rather
// than the first section outright. Pinned separately from the reload test
// above because the two now resolve to different winners for different
// reasons, and one test asserting "Models wins ties" would hide that.
test("a Jobs vs Notifications tie resolves to Jobs — Models cannot enter this contest", () => {
  const closed: SectionKey[] = [];
  const renderer = mount(
    <>
      <Section sectionKey="jobs" wantOpen onForceClose={() => closed.push("jobs")} />
      <Section
        sectionKey="notifications"
        wantOpen
        onForceClose={() => closed.push("notifications")}
      />
    </>,
  );
  expect(closed).toEqual(["notifications"]);
  act(() => renderer.unmount());
});
