// D582: one status-bar panel open at a time. The cases worth pinning are the
// ones that are NOT obvious — a tie inside a single commit, and a later request
// beating an earlier one — because both were previously at the mercy of
// effect-execution order.
//
// THREE SECTIONS NOW (Models, Activity, Notifications) — `SECTION_ORDER` is
// `["models", "activity", "notifications"]`. Models can never auto-open (its
// own `useAutoExpandOnNew` call never feeds anything into `ids`), so the only
// tie this arbiter can ever see is still Activity vs Notifications, which is
// the one case these tests pin.
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

test("two sections wanting open in the SAME commit resolve to Activity, deterministically", () => {
  const closed: SectionKey[] = [];
  const renderer = mount(
    <>
      <Section sectionKey="activity" wantOpen onForceClose={() => closed.push("activity")} />
      <Section
        sectionKey="notifications"
        wantOpen
        onForceClose={() => closed.push("notifications")}
      />
    </>,
  );
  // Same commit means the same tick, so this is a genuine tie and SECTION_ORDER
  // breaks it — Activity first. The loser is force-closed; the winner is not
  // touched at all, because the arbiter only ever closes.
  expect(closed).toEqual(["notifications"]);
  act(() => renderer.unmount());
});

test("a LATER request beats whatever was already open, whichever section it is", async () => {
  const closed: SectionKey[] = [];
  function Bar({ notificationsOpen }: { notificationsOpen: boolean }) {
    return (
      <>
        <Section sectionKey="activity" wantOpen onForceClose={() => closed.push("activity")} />
        <Section
          sectionKey="notifications"
          wantOpen={notificationsOpen}
          onForceClose={() => closed.push("notifications")}
        />
      </>
    );
  }
  const renderer = mount(<Bar notificationsOpen={false} />);
  expect(closed).toHaveLength(0);

  await nextTick();
  act(() => renderer.update(<Bar notificationsOpen />));

  // Recency wins: Activity closes even though it is first in SECTION_ORDER,
  // because SECTION_ORDER only ever breaks ties.
  expect(closed).toEqual(["activity"]);
  act(() => renderer.unmount());
});

test("a section that merely STAYS open cannot keep out-bidding a sibling", async () => {
  const closed: SectionKey[] = [];
  function Bar({ n, notificationsOpen }: { n: number; notificationsOpen: boolean }) {
    return (
      <>
        <Section sectionKey="activity" wantOpen onForceClose={() => closed.push("activity")} />
        <Section
          sectionKey="notifications"
          wantOpen={notificationsOpen}
          onForceClose={() => closed.push("notifications")}
        />
        {/* Re-renders without changing either section's want. */}
        <span>{n}</span>
      </>
    );
  }
  const renderer = mount(<Bar n={0} notificationsOpen={false} />);
  // Several commits pass with Activity simply remaining open. A tick is
  // stamped only on the false -> true edge, so none of these re-stamps it.
  for (const n of [1, 2, 3]) {
    await nextTick();
    act(() => renderer.update(<Bar n={n} notificationsOpen={false} />));
  }
  await nextTick();
  act(() => renderer.update(<Bar n={4} notificationsOpen />));

  expect(closed).toEqual(["activity"]);
  act(() => renderer.unmount());
});

// THE AUTO-OPEN CASE is the SAME contest as the same-commit tie above, now
// that there are only two sections: since D587 (carried into the status-bar
// merge via Activity's own `alsoDrawn` wiring for its engine/model rows), the
// only source that can ever WANT Activity's panel open is a job arrival, so
// the one same-tick auto-open contest left IS Activity vs Notifications —
// already pinned by the first test in this file.
