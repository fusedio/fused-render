// The notification store's exit path, tiering rules and retention — the
// client-side counterpart to `jobs.test.ts`'s coverage of `effectiveTier`/
// `popupTick`. See SPEC-toasts-become-notifications.md and
// DECISIONS-toasts-become-notifications.md for the reasoning this codifies.
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";
import { createElement } from "react";

import { installDomShim } from "@platform/lib/testDomShim";

// notifications.ts imports router.ts, which reads `location` at module
// scope — the dom shim has to be installed before that import EVALUATES, not
// merely before this file's own statements run (static imports are
// evaluated before a module's own top-level code, regardless of where the
// `import` keyword sits in the file). `await import(...)`, as router.test.ts
// itself does, defers the import past the `installDomShim()` call below.
installDomShim();

import { JOB_POPUP_VISIBLE_MS } from "@platform/lib/jobs";
const {
  TOAST_EXIT_MS,
  _resetNotificationsForTest,
  _setIsEmbedForTest,
  _setIsTopEmbedForTest,
  dismissNotification,
  dismissPopup,
  getPopupNotification,
  getRetainedNotifications,
  notify,
  useRetainedNotifications,
} = await import("@platform/lib/notifications");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

beforeEach(_resetNotificationsForTest);
afterEach(() => {
  _resetNotificationsForTest();
  _setIsTopEmbedForTest(null);
  _setIsEmbedForTest(null);
  delete (globalThis.window as unknown as Record<string, unknown>).top;
});

const popupSnapshot = getPopupNotification;

// ---- tone -> tier default mapping ------------------------------------------

test("tone: error with no explicit tier defaults to attention", () => {
  notify({ title: "Could not save", tone: "error" });
  expect(getRetainedNotifications().map((n) => [n.title, n.tier])).toEqual([
    ["Could not save", "attention"],
  ]);
});

test("tone: info with no explicit tier defaults to transient and is never retained", () => {
  notify({ title: "Path copied", tone: "info" });
  expect(getRetainedNotifications()).toEqual([]);
});

test("neither tone nor tier defaults to transient", () => {
  notify({ title: "just a note" });
  expect(getRetainedNotifications()).toEqual([]);
});

test("an explicit tier wins over the tone default when tone is not error", () => {
  notify({ title: "Freed 1.4 GB", tone: "info", tier: "trail" });
  expect(getRetainedNotifications().map((n) => n.tier)).toEqual(["trail"]);
});

// ---- error promotion override ----------------------------------------------

test("tone: error always promotes to attention, even over an explicit lower tier", () => {
  notify({ title: "failed anyway", tone: "error", tier: "transient" });
  expect(getRetainedNotifications().map((n) => n.tier)).toEqual(["attention"]);
});

// ---- retention by tier ------------------------------------------------------

test("a transient message leaves nothing in the retained list", () => {
  notify({ title: "Duplicated as foo.py", tone: "info" });
  expect(getRetainedNotifications()).toEqual([]);
});

test("an attention message is retained", () => {
  notify({ title: "Could not delete", tone: "error" });
  expect(getRetainedNotifications().map((n) => n.title)).toEqual(["Could not delete"]);
});

test("a trail message is retained", () => {
  notify({ title: "Moved 3 items to Desktop", tier: "trail" });
  expect(getRetainedNotifications().map((n) => n.tier)).toEqual(["trail"]);
});

// ---- popup lifecycle --------------------------------------------------------

test("notify pops a card immediately, which starts leaving after JOB_POPUP_VISIBLE_MS and is gone after TOAST_EXIT_MS more", async () => {
  notify({ title: "hello", tone: "info" });
  expect(popupSnapshot()?.title).toBe("hello");
  expect(popupSnapshot()?.leaving).toBe(false);

  await sleep(JOB_POPUP_VISIBLE_MS + 30);
  expect(popupSnapshot()?.leaving).toBe(true);

  await sleep(TOAST_EXIT_MS + 30);
  expect(popupSnapshot()).toBe(null);
});

test("latest notify wins — a second call replaces the popup instead of queueing", () => {
  notify({ title: "first", tone: "info" });
  notify({ title: "second", tone: "info" });
  expect(popupSnapshot()?.title).toBe("second");
});

test("the popup ✕ (dismissPopup) does not clear the retained row", async () => {
  notify({ title: "Could not save", tone: "error" });
  expect(getRetainedNotifications().length).toBe(1);

  dismissPopup();
  await sleep(TOAST_EXIT_MS + 30);

  expect(popupSnapshot()).toBe(null);
  expect(getRetainedNotifications().length).toBe(1);
});

test("dismissPopup(id) is a no-op if the given id is not the CURRENTLY showing popup (finding #5)", async () => {
  // The exact shape the finding describes: a caller from a delayed action
  // (e.g. clicking "Reconnect" on a retained row) captures the id it minted
  // when it first popped, but by the time the click fires an unrelated
  // notify() may have replaced the popup with something else entirely —
  // without an id, `dismissPopup()` closes whatever's showing now, not the
  // one the caller actually means.
  const firstId = notify({ title: "first disconnected", tone: "error" });
  const secondId = notify({ title: "second disconnected", tone: "error" });
  expect(popupSnapshot()?.id).toBe(secondId);

  dismissPopup(firstId);
  // Nothing should have happened — `firstId` no longer names the popup.
  expect(popupSnapshot()?.id).toBe(secondId);
  expect(popupSnapshot()?.leaving).toBe(false);

  dismissPopup(secondId);
  await sleep(TOAST_EXIT_MS + 30);
  expect(popupSnapshot()).toBe(null);
});

test("dismissPopup() with no id still closes whatever popup is currently showing", async () => {
  notify({ title: "Could not save", tone: "error" });
  dismissPopup();
  await sleep(TOAST_EXIT_MS + 30);
  expect(popupSnapshot()).toBe(null);
});

test("dismissNotification removes a retained row and leaves the popup untouched", () => {
  const id = notify({ title: "Could not save", tone: "error" });
  dismissNotification(id);
  expect(getRetainedNotifications()).toEqual([]);
});

// ---- replaceId --------------------------------------------------------------

test("replaceId updates the live popup in place instead of pushing a new one", () => {
  const id = notify({ title: "Still undoing…", tone: "info" });
  const second = notify({ title: "Still undoing…", tone: "info" }, id);
  expect(second).toBe(id);
  expect(popupSnapshot()?.title).toBe("Still undoing…");
});

test("replaceId against a live popup re-arms the exit timer instead of letting the original clock run out underneath it", async () => {
  // A paste's "Copying N of M…" or an undo's "Still undoing…" keeps calling
  // notify(..., id) against the SAME popup while a long-running operation is
  // in flight. If the timer armed by the FIRST call kept counting regardless
  // of content updates, an operation slower than JOB_POPUP_VISIBLE_MS would
  // see its progress card start leaving mid-operation.
  const id = notify({ title: "Copying 1 of 5…", tone: "info" });

  await sleep(JOB_POPUP_VISIBLE_MS - 200);
  expect(popupSnapshot()?.leaving).toBe(false);

  notify({ title: "Copying 2 of 5…", tone: "info" }, id);

  // Past the ORIGINAL timer's deadline — still up, because the update reset it.
  await sleep(300);
  expect(popupSnapshot()?.title).toBe("Copying 2 of 5…");
  expect(popupSnapshot()?.leaving).toBe(false);

  // The re-armed timer still eventually fires on its own, JOB_POPUP_VISIBLE_MS
  // after the UPDATE (not the original notify() call) — checked before
  // TOAST_EXIT_MS has also elapsed, so the card is still present as "leaving".
  await sleep(JOB_POPUP_VISIBLE_MS - 250);
  expect(popupSnapshot()?.leaving).toBe(true);
}, 10_000);

test("replaceId against a live, never-retained popup does not re-arm once it has left", async () => {
  const id = notify({ title: "Still undoing…", tone: "info" });
  await sleep(JOB_POPUP_VISIBLE_MS + TOAST_EXIT_MS + 30);
  expect(popupSnapshot()).toBe(null);

  // Once the popup is gone, a replaceId call against its id is no longer a
  // "live popup" match — it falls through to the fresh-notification path
  // (covered separately below), not a resurrection of the old card.
  const second = notify({ title: "Still undoing…", tone: "info" }, id);
  expect(second).not.toBe(id);
});

test("replaceId against an id that already left starts a fresh notification", async () => {
  const id = notify({ title: "first", tone: "info" });
  await sleep(JOB_POPUP_VISIBLE_MS + TOAST_EXIT_MS + 30);
  expect(popupSnapshot()).toBe(null);

  const second = notify({ title: "second", tone: "info" }, id);
  expect(second).not.toBe(id);
  expect(popupSnapshot()?.title).toBe("second");
});

// ---- IS_TOP_EMBED no-expiry path ---------------------------------------------

test("an attention popup never auto-expires under IS_TOP_EMBED", async () => {
  _setIsTopEmbedForTest(true);
  notify({ title: "Could not save", tone: "error" });

  await sleep(JOB_POPUP_VISIBLE_MS + TOAST_EXIT_MS + 30);
  expect(popupSnapshot()?.title).toBe("Could not save");
  expect(popupSnapshot()?.leaving).toBe(false);
});

test("a trail popup still auto-expires normally under IS_TOP_EMBED", async () => {
  _setIsTopEmbedForTest(true);
  notify({ title: "Moved 3 items", tier: "trail" });

  await sleep(JOB_POPUP_VISIBLE_MS + TOAST_EXIT_MS + 30);
  expect(popupSnapshot()).toBe(null);
});

test("IS_TOP_EMBED's no-expiry rule is not in effect elsewhere", async () => {
  _setIsTopEmbedForTest(false);
  notify({ title: "Could not save", tone: "error" });

  await sleep(JOB_POPUP_VISIBLE_MS + TOAST_EXIT_MS + 30);
  expect(popupSnapshot()).toBe(null);
});

// ---- pane -> shell forwarding (code review #1104, findings 1/2/8) ---------
//
// A pane forwards its retained rows to the shell through a same-origin
// global (`_fusedIngestNotification`/`_fusedDismissNotification`, installed
// on `globalThis` by this module's own `installIngest()`), not postMessage
// — see this module's header comment. These tests exercise the RECEIVING
// end directly (what `installIngest()` wires up on `globalThis` in every
// document, including the shell's) and, for the dismiss-forwarding case,
// the SENDING end (`forwardToShell`/`forwardDismissToShell`), which needs
// `IS_EMBED`/`IS_TOP_EMBED` overridden to look like a pane — see
// `_setIsEmbedForTest`.

function RetainedProbe({
  onRender,
}: {
  onRender: (titles: string[]) => void;
}) {
  const items = useRetainedNotifications();
  onRender(items.map((n) => n.title));
  return null;
}

test("a message ingested via the pane->shell global refreshes the useSyncExternalStore snapshot (finding #1)", () => {
  const renders: string[][] = [];
  let renderer: ReturnType<typeof create> | null = null;
  act(() => {
    renderer = create(
      createElement(RetainedProbe, { onRender: (titles: string[]) => renders.push(titles) }),
    );
  });
  expect(renders).toEqual([[]]);

  act(() => {
    (globalThis as unknown as { _fusedIngestNotification: (input: unknown) => number })
      ._fusedIngestNotification({ title: "pane error", tone: "error" });
  });

  // useSyncExternalStore only re-renders when getSnapshot()'s OWN reference
  // changes across an emit() — a handler that mutates `retained` and calls
  // emit() but never refreshSnapshot() leaves the old snapshot object in
  // place and this second render never happens (the exact bug: "the pane
  // forwards, the shell silently never renders it").
  expect(renders).toEqual([[], ["pane error"]]);

  // Unmount: otherwise this component stays subscribed (module-level
  // `listeners` Set) for the rest of the file, and every later test's
  // notify()/dismissNotification() calls (not wrapped in act(), since they
  // don't concern this probe) would each print a spurious act() warning.
  act(() => {
    renderer?.unmount();
  });
});

test("a forwarded message is minted a fresh id in the RECEIVING document's own sequence, not reused from the sender (finding #2)", () => {
  const localId = notify({ title: "local attention", tone: "error" });

  // Simulate a SEPARATE pane's own module-local `nextId` sequence, which also
  // starts at 1 in every document — the exact collision the reviewer
  // describes: two documents each mint id 1 for their own first message, and
  // the old `forwardToShell` shipped that id verbatim.
  let ingestedId: number = -1;
  act(() => {
    ingestedId = (
      globalThis as unknown as { _fusedIngestNotification: (input: unknown) => number }
    )._fusedIngestNotification({ id: localId, title: "pane error", tone: "error" });
  });

  expect(ingestedId).not.toBe(localId);
  const ids = getRetainedNotifications().map((n) => n.id);
  expect(new Set(ids).size).toBe(ids.length);
});

test("dismissNotification in a pane forwards to the shell's own (independently-minted) copy, not just the pane's invisible one (finding #8)", () => {
  _setIsEmbedForTest(true);
  _setIsTopEmbedForTest(false);

  const ingestCalls: unknown[] = [];
  const dismissCalls: number[] = [];
  const fakeTop = {
    _fusedIngestNotification: (input: unknown) => {
      ingestCalls.push(input);
      return 999; // the shell's own minted id — deliberately not the pane's local id
    },
    _fusedDismissNotification: (id: number) => {
      dismissCalls.push(id);
    },
  };
  (globalThis.window as unknown as Record<string, unknown>).top = fakeTop;

  const localId = notify({ title: "registry error", tone: "error" });
  expect(ingestCalls.length).toBe(1);
  expect(localId).not.toBe(999);

  dismissNotification(localId);

  // The row that is actually visible lives in the SHELL's retained list,
  // under the id the shell minted for it (999) — not the pane's own local
  // id, which names only the pane's own invisible copy.
  expect(dismissCalls).toEqual([999]);
});
