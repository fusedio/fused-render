// WHEN the pull channel answers, which is the whole of this hook's job: an
// `app_state` tool call is BLOCKED until something answers it, so a row this
// effect never re-runs for is a run hung until the tool's own timeout (minutes).
//
// `T` has no effect graph — `answerAppState` is called straight from the 400 ms
// poll, so every unanswered row gets a go on every tick (T:15758-15869). These
// tests pin the two ways the React port lost that.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

import type { AppStateRow } from "../protocol/types";
import type { AppStateWatcher } from "./appState";

const { useAppStateResponder, APP_STATE_NULL_POLLS } = await import("./useAppStateResponder");

type Snapshot = ReturnType<AppStateWatcher["snapshot"]>;

/** Enough of the watcher for the responder: it reads `snapshot()` and, once the
 *  waiting is over, `pull()`. */
function fakeWatcher(snapshot: Snapshot): AppStateWatcher {
  return {
    snapshot: () => snapshot,
    pull: () => ({ unreadable: true }) as unknown as ReturnType<AppStateWatcher["pull"]>,
  } as unknown as AppStateWatcher;
}

const row = (id: string, pollsSeen: number): AppStateRow => ({
  id,
  reason: "checking the pane",
  created_at: 0,
  pollsSeen,
  waitedOut: pollsSeen > APP_STATE_NULL_POLLS,
});

interface Harness {
  rows: AppStateRow[];
  watcher: AppStateWatcher | null;
}

function mount(initial: Harness, answered: string[], notes: string[] = []) {
  function Probe(props: Harness) {
    useAppStateResponder({
      rows: props.rows,
      watcher: props.watcher,
      answerAppState: async (id) => {
        answered.push(id);
      },
      onNote: (t) => notes.push(t),
    });
    return null;
  }
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Probe {...initial} />);
  });
  return {
    poll(next: Harness) {
      act(() => {
        r.update(<Probe {...next} />);
      });
    },
    unmount() {
      act(() => r.unmount());
    },
  };
}

test("a pane that mounts AFTER the request answers it on the readiness flip", async () => {
  const answered: string[] = [];
  const rows = [row("req-1", 1)];
  // No pane yet: the row is left unclaimed rather than answered from nothing.
  const h = mount({ rows, watcher: null }, answered);
  await act(async () => {});
  expect(answered).toEqual([]);
  // The pane mounts. Nothing about the ROWS changed — same id, same poll count —
  // so keying the effect on the ids alone left this request unanswered until the
  // model happened to ask again.
  h.poll({ rows, watcher: fakeWatcher({ title: "app" } as unknown as Snapshot) });
  await act(async () => {});
  expect(answered).toEqual(["req-1"]);
  h.unmount();
});

test("a null snapshot waits APP_STATE_NULL_POLLS polls, then answers with the sentence", async () => {
  const answered: string[] = [];
  const notes: string[] = [];
  // Pane present but with nothing to report — the common case right after the
  // model edits something and the pane reloads.
  const blind = fakeWatcher(null);
  const h = mount({ rows: [row("req-1", 1)], watcher: blind }, answered, notes);
  await act(async () => {});
  expect(answered).toEqual([]);
  // Each poll re-stamps `pollsSeen`, which is what makes the count advance: with
  // the old key the effect never ran twice and this fall-through was dead code.
  for (let seen = 2; seen <= APP_STATE_NULL_POLLS; seen++) {
    h.poll({ rows: [row("req-1", seen)], watcher: blind });
    await act(async () => {});
    expect(answered).toEqual([]);
  }
  h.poll({ rows: [row("req-1", APP_STATE_NULL_POLLS + 1)], watcher: blind });
  await act(async () => {});
  expect(answered).toEqual(["req-1"]);
  // And the one-line note, once per REQUEST however many attempts it took.
  expect(notes).toEqual(["read app state — checking the pane"]);
  h.unmount();
});

test("a request already answered is not answered again, however many polls replay it", async () => {
  const answered: string[] = [];
  const pane = fakeWatcher({ title: "app" } as unknown as Snapshot);
  const h = mount({ rows: [row("req-1", 1)], watcher: pane }, answered);
  await act(async () => {});
  for (let seen = 2; seen < 5; seen++) {
    h.poll({ rows: [row("req-1", seen)], watcher: pane });
    await act(async () => {});
  }
  expect(answered).toEqual(["req-1"]);
  h.unmount();
});
