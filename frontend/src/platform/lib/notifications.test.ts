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
  labelForSource,
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

test("an explicit tier: attention wins over the tone: info default", () => {
  notify({ title: "worth flagging even though it succeeded", tone: "info", tier: "attention" });
  expect(getRetainedNotifications().map((n) => n.tier)).toEqual(["attention"]);
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

// ---- retention narrowing: error OR actionable, nothing else (user: "don't
// keep this in the list. just show popup. anything non actionable or error
// doesn't belong in the list") ------------------------------------------------

test("a tone: info message with an action is retained even though it is not an error", () => {
  notify({ title: "Export ready", tone: "info", action: { label: "Open", onClick: () => {} } });
  expect(getRetainedNotifications().map((n) => n.title)).toEqual(["Export ready"]);
});

test("a tone: info message with a page is retained even though it is not an error", () => {
  notify({ title: "Export ready", tone: "info", page: "/tasks/42" });
  expect(getRetainedNotifications().map((n) => n.title)).toEqual(["Export ready"]);
});

test("a tone: info message with neither an action nor a page is never retained (Undid/Redid the delete no longer belongs in the list)", () => {
  notify({ title: "Undid the delete.", tone: "info" });
  notify({ title: "Freed 1.4 GB — deleted foo", tone: "info" });
  notify({ title: "Moved 3 items to Desktop", tone: "info" });
  expect(getRetainedNotifications()).toEqual([]);
});

test("tier: silent is never retained even when the message carries an action", () => {
  notify({
    title: "quiet but actionable",
    tone: "info",
    tier: "silent",
    action: { label: "Open", onClick: () => {} },
  });
  expect(getRetainedNotifications()).toEqual([]);
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

// ---- replaceId against a RETAINED (not live) entry -------------------------

test(
  "replaceId against a retained (not live) entry clears a stale timer instead of letting it fire against the new popup (finding #6)",
  async () => {
    _setIsTopEmbedForTest(true);

    // A: an attention message under IS_TOP_EMBED — pops, retained, and (per
    // the no-expiry rule) never gets an exit timer armed for it.
    const idA = notify({ title: "first attention", tone: "error" });

    // B: an ordinary transient popup that DOES get a real exit timer, and
    // replaces A as the live popup — A is now retained-but-not-live.
    notify({ title: "just passing through", tone: "info" });
    expect(popupSnapshot()?.title).toBe("just passing through");

    // Update A via replaceId. A is not the live popup, so this hits the
    // RETAINED branch (not the live-popup branch above) — it re-pops A as
    // the live popup again, still attention under IS_TOP_EMBED, so its own
    // rule says this must never auto-expire either.
    notify({ title: "first attention (updated)", tone: "error" }, idA);
    expect(popupSnapshot()?.title).toBe("first attention (updated)");

    // B's own timer, if left running (the bug: nothing clears/re-evaluates
    // it when the retained branch takes over), fires around now and marks
    // whatever `popup` currently IS — no longer B, but A's updated content —
    // as leaving, then removes it entirely: a silent violation of "never
    // auto-expires under IS_TOP_EMBED".
    await sleep(JOB_POPUP_VISIBLE_MS + TOAST_EXIT_MS + 30);
    expect(popupSnapshot()?.title).toBe("first attention (updated)");
    expect(popupSnapshot()?.leaving).toBe(false);
  },
  10_000,
);

test("replaceId against a retained entry that resolves to a non-retained tier removes it from the retained list (finding #7b)", () => {
  const id = notify({ title: "was attention", tone: "error" });
  expect(getRetainedNotifications().map((n) => n.id)).toEqual([id]);

  // Not the live popup any more, so this hits the retained branch — and the
  // new content resolves to "transient" (no tone/tier at all), which must
  // never sit in the retained list regardless of what it is replacing.
  notify({ title: "just passing through", tone: "info" });
  notify({ title: "no longer worth keeping" }, id);

  expect(getRetainedNotifications().map((n) => n.id)).toEqual([]);
});

test("replaceId against the LIVE popup also updates the matching retained entry, if one exists (finding #7a)", () => {
  const id = notify({ title: "original", tone: "error" });
  expect(getRetainedNotifications().map((n) => n.title)).toEqual(["original"]);

  // Still the live popup, so this hits the FIRST (live-popup) branch — the
  // retained row for the same id must reflect the update too, not keep
  // showing stale content the popup itself has moved past.
  notify({ title: "updated", tone: "error" }, id);

  expect(getRetainedNotifications().map((n) => n.title)).toEqual(["updated"]);
});

// ---- IS_TOP_EMBED no-expiry path ---------------------------------------------

test("an attention popup never auto-expires under IS_TOP_EMBED", async () => {
  _setIsTopEmbedForTest(true);
  notify({ title: "Could not save", tone: "error" });

  await sleep(JOB_POPUP_VISIBLE_MS + TOAST_EXIT_MS + 30);
  expect(popupSnapshot()?.title).toBe("Could not save");
  expect(popupSnapshot()?.leaving).toBe(false);
});

test("a retained-but-not-attention (actionable) popup still auto-expires normally under IS_TOP_EMBED — only attention never expires", async () => {
  _setIsTopEmbedForTest(true);
  notify({ title: "Export ready", tone: "info", page: "/tasks/42" });

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

// DEFECT (2026-09-18 fix, live repro): "fused-render / File system change
// detection vs indexing / Finished" showed up as THREE byte-identical
// retained rows in the shell's Notifications panel, and "fused-share /
// Files / Finished" as two — each one a separate document (a sub-document
// watching the same task) forwarding the exact same finished-task notice
// through `_fusedIngestNotification`. `notify()` already collapses a fresh
// call into an already-retained row sharing its `messageFamily` (see its own
// "GROUPING/UPDATION" comment); the ingest receiver skipped that lookup
// entirely and appended straight onto `retained`, so anything reaching the
// shell via forwarding — rather than a local `notify()` call — stacked
// duplicates forever. Two ingests sharing a family must collapse into one
// retained row with `count` incremented, exactly like two local `notify()`
// calls do.
test("two ingested messages sharing a family collapse into one retained row with count 2 (2026-09-18 fix)", () => {
  const ingest = (globalThis as unknown as {
    _fusedIngestNotification: (input: unknown) => number;
  })._fusedIngestNotification;
  let id1 = -1;
  let id2 = -1;
  act(() => {
    id1 = ingest({
      title: "File system change detection vs indexing",
      detail: "Finished",
      tone: "info",
      origin: "fused-render",
      page: "/tasks/1",
    });
    id2 = ingest({
      title: "File system change detection vs indexing",
      detail: "Finished",
      tone: "info",
      origin: "fused-render",
      page: "/tasks/1",
    });
  });
  expect(id2).toBe(id1);
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(1);
  expect(retained[0]?.count).toBe(2);
});

// In the ordinary (non-nested-embed) case the receiving document IS the
// top-level shell, where `forwardToShell`'s own
// `!effectiveIsEmbed() || effectiveIsTopEmbed()` guard already returns
// `undefined` — an ingested message must not be forwarded again in that
// (the common) case, whatever internal path (`notify()` or otherwise) the
// receiver uses to collapse it.
test("an ingested message is not re-forwarded by a top-level (non-embed) receiver", () => {
  const reForwardCalls: unknown[] = [];
  (globalThis.window as unknown as Record<string, unknown>).top = {
    _fusedIngestNotification: (input: unknown) => {
      reForwardCalls.push(input);
      return 999;
    },
  };

  const ingest = (globalThis as unknown as {
    _fusedIngestNotification: (input: unknown) => number;
  })._fusedIngestNotification;
  act(() => {
    ingest({ title: "pane error", tone: "error" });
  });

  expect(reForwardCalls).toEqual([]);
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

// ---- source suppression (SPEC-quiet-notifications.md §2a) -----------------
//
// `presence.ts`'s `currentPresencePage()` reads off the shared, module-cached
// `location` global — shared with every OTHER `bun test` file in this run
// (`testDomShim.ts`'s own header comment) — so this suite pins `pathname`
// explicitly rather than trusting whatever the shim's default or another
// file's own navigation left it at.
const savedLocation = { pathname: location.pathname, search: location.search };
beforeEach(() => {
  (location as unknown as { pathname: string }).pathname = "/";
  (location as unknown as { search: string }).search = "";
});
afterEach(() => {
  (location as unknown as { pathname: string }).pathname = savedLocation.pathname;
  (location as unknown as { search: string }).search = savedLocation.search;
});

test("a plain success with source matching the current page is suppressed entirely", () => {
  const id = notify({ title: "Installed", tone: "info", source: "/" });
  expect(getPopupNotification()).toBeNull();
  expect(getRetainedNotifications()).toEqual([]);
  expect(id).toBe(-1);
});

test("the same message with a source that does NOT match the current page still pops", () => {
  notify({ title: "Installed", tone: "info", source: "/claude-config" });
  expect(popupSnapshot()?.title).toBe("Installed");
});

test("an error with a matching source is never suppressed", () => {
  notify({ title: "Install failed", tone: "error", source: "/" });
  expect(popupSnapshot()?.title).toBe("Install failed");
  expect(getRetainedNotifications().map((n) => n.title)).toEqual(["Install failed"]);
});

test("an actionable message (carries a page) with a matching source is never suppressed", () => {
  notify({ title: "Ready", tone: "info", source: "/", page: "/claude-config" });
  expect(popupSnapshot()?.title).toBe("Ready");
  expect(getRetainedNotifications().map((n) => n.title)).toEqual(["Ready"]);
});

test("no source at all is never suppressed (opt-in only, never defaulted)", () => {
  notify({ title: "Path copied", tone: "info" });
  expect(popupSnapshot()?.title).toBe("Path copied");
});

// ---- CHANGE 1 (SPEC-quiet-notifications.md follow-up): every notification
// names who raised it ---------------------------------------------------

test("labelForSource: an fs path labels as its own basename, extension stripped", () => {
  expect(labelForSource("/Users/me/Projects/my-app")).toBe("my-app");
  expect(labelForSource("/Users/me/Projects/my-app/")).toBe("my-app");
  expect(labelForSource("/Users/me/Projects/report.pdf")).toBe("report");
});

test("labelForSource: a non-path string passes through verbatim", () => {
  expect(labelForSource("Playground")).toBe("Playground");
});

test("labelForSource: no source, or one resolving to nothing, is the empty string — never a placeholder", () => {
  expect(labelForSource(undefined)).toBe("");
  expect(labelForSource("")).toBe("");
  expect(labelForSource("   ")).toBe("");
});

test("a message with a source carries the label on the stored row", () => {
  notify({ title: "Could not save", tone: "error", source: "/Users/me/Projects/my-app" });
  expect(getRetainedNotifications()[0]?.origin).toBe("my-app");
});

test("a message with no source carries no origin at all", () => {
  notify({ title: "Could not save", tone: "error" });
  expect(getRetainedNotifications()[0]?.origin).toBeUndefined();
});

// `origin` (2026-09-17 fix): a caption INDEPENDENT of suppression — a caller
// wants "who made this" without also opting into "suppress when its page is
// open" (a task-status-notify.ts caller says exactly why: a FINISHED task's
// chat being open no longer means "already knows"). See notifications.ts's
// own `NotificationInput.origin` doc comment for the full regression story
// this closes.
test("an explicit `origin` sets the caption without opting into suppression", () => {
  notify({ title: "Transcripto YouTube transcriber", tone: "info", page: "/tasks", origin: "Transcripto" });
  expect(getRetainedNotifications()[0]?.origin).toBe("Transcripto");
  expect(getRetainedNotifications()[0]?.page).toBe("/tasks");
});

test("`origin` wins over a `source`-derived label when both are given", () => {
  notify({ title: "x", tone: "error", source: "/Users/me/Projects/my-app", origin: "Custom Label" });
  expect(getRetainedNotifications()[0]?.origin).toBe("Custom Label");
});

test("`origin` alone (no `source`) never suppresses, even when its own text names a focused page", () => {
  // isFocusedHere is keyed on `source`, not `origin` — an `origin`-only
  // caller has nothing `isSuppressed` can match against, so this must always
  // pop regardless of what document/page is focused.
  const id = notify({ title: "Finished", tone: "info", page: "/somewhere", origin: "Somewhere" });
  expect(id).not.toBe(-1);
  expect(getPopupNotification()?.title).toBe("Finished");
});

// ---- family grouping/collapse (user: "better notification grouping/
// updation for same source") — two genuine repeat finishes of the same
// client-raised task collapse into ONE retained row that updates in place,
// mirroring the "N of M done" shape `jobs.ts`'s own `groupJobs` already gives
// server-side job families, rather than stacking N byte-identical rows. ----

test("two notify() calls with the same page collapse into one retained row with count 2", () => {
  notify({ title: "Transcripto YouTube transcriber finished", tone: "info", page: "/tasks/1" });
  notify({ title: "Transcripto YouTube transcriber finished", tone: "info", page: "/tasks/1" });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(1);
  expect(retained[0]?.count).toBe(2);
});

test("a third repeat within the burst window updates the SAME row again, not a third one", () => {
  const id1 = notify({ title: "Transcripto YouTube transcriber finished", tone: "info", page: "/tasks/1" });
  notify({ title: "Transcripto YouTube transcriber finished", tone: "info", page: "/tasks/1" });
  const id3 = notify({ title: "Transcripto YouTube transcriber finished", tone: "info", page: "/tasks/1" });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(1);
  expect(retained[0]?.count).toBe(3);
  expect(id3).toBe(id1);
});

test("two notify() calls sharing a title but no page collapse by title family", () => {
  // `tone: "error"` (attention-tier) is always retained regardless of
  // action/page (`isRetained`) — the simplest way to get a retained, no-page
  // row to exercise the title-only branch of `messageFamily`.
  notify({ title: "Backup failed", tone: "error" });
  notify({ title: "Backup failed", tone: "error" });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(1);
  expect(retained[0]?.count).toBe(2);
});

test("different families (different page) never collapse into each other", () => {
  notify({ title: "Transcripto YouTube transcriber finished", tone: "info", page: "/tasks/1" });
  notify({ title: "Other task finished", tone: "info", page: "/tasks/2" });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(2);
  expect(retained.every((n) => n.count === 1)).toBe(true);
});

// DEFECT 2 (2026-09-17 fix): the collapse used to only fire within
// `GROUP_GAP_MS` (2 minutes) of the existing row's own `updatedAt` — too
// short for the user's actual case (two runs of a Claude task, routinely
// finishing far more than two minutes apart). A repeat must collapse into an
// already-retained, still-undismissed row no matter how long ago it was
// raised.
test("a repeat collapses into an existing retained row even long after the old 2-minute burst window", () => {
  const realNow = Date.now;
  try {
    let now = 1_000_000;
    Date.now = () => now;
    const id1 = notify({
      title: "Transcripto YouTube transcriber finished",
      tone: "info",
      page: "/tasks/1",
    });
    now += 60 * 60 * 1000; // an hour later — routine for two separate task runs
    const id2 = notify({
      title: "Transcripto YouTube transcriber finished",
      tone: "info",
      page: "/tasks/1",
    });
    const retained = getRetainedNotifications();
    expect(retained).toHaveLength(1);
    expect(retained[0]?.count).toBe(2);
    expect(id2).toBe(id1);
  } finally {
    Date.now = realNow;
  }
});

// DEFECT 2, continued: once the earlier row has been DISMISSED, the family
// no longer names anything to collapse into — the next repeat starts a fresh
// row, exactly as it did before this fix (this branch was never asked to
// make a dismissed row un-dismissable).
test("a repeat after the earlier row was dismissed starts a fresh row, not a collapse", () => {
  notify({ title: "Transcripto YouTube transcriber finished", tone: "info", page: "/tasks/1" });
  const before = getRetainedNotifications();
  expect(before).toHaveLength(1);
  dismissNotification(before[0]!.id);
  expect(getRetainedNotifications()).toHaveLength(0);

  notify({ title: "Transcripto YouTube transcriber finished", tone: "info", page: "/tasks/1" });
  const after = getRetainedNotifications();
  expect(after).toHaveLength(1);
  expect(after[0]?.count).toBe(1);
});

// DEFECT 3 (2026-09-17 fix): `messageFamily` used to key ONLY on `page`,
// which collides whenever two DIFFERENT tasks fall back to the same
// per-folder/global destination (`taskDestination`'s
// `taskHref ?? folderHref ?? "/tasks"`) — the collapsed row is rebuilt from
// the NEW input, so the older task's title silently vanished with no trace.
// Title is now folded into the family key alongside `page`, so two different
// tasks sharing a folder href must NOT collapse into one row, even though the
// user's own case (same task, same title, same page) still does (covered by
// the "same page" test above).
test("two different tasks sharing the same folder-fallback page do not collapse into each other", () => {
  notify({ title: "Transcript task finished", tone: "info", page: "/explorer/proj" });
  notify({ title: "Cleanup task finished", tone: "info", page: "/explorer/proj" });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(2);
  expect(retained.every((n) => n.count === 1)).toBe(true);
  expect(retained.map((n) => n.title)).toEqual([
    "Transcript task finished",
    "Cleanup task finished",
  ]);
});

// DEFECT (2026-09-17 fix, live repro): the finished-task family used to key
// on `page`, which is `taskDestination(task)` -> `taskHref` and embeds that
// run's own PER-RUN `session_id` — two separate runs of the identical task
// therefore got two different `page` values and never collapsed. This is
// the user's own "i ran it twice, I just want them grouped" case: same
// caption (`origin`), same title, genuinely different `page` (a different
// run's session url). They must collapse into one row, and that row must
// point at the NEWER run's page — the collapse rebuilds from the latest
// input, and a click on the grouped row should land on the run the user
// just finished, not the stale earlier one.
test("two finished-task notices with the same caption and title but different (per-run) pages collapse into one row pointing at the newer page", () => {
  notify({
    title: "Reply with exactly one word: APPLE",
    detail: "Finished",
    tone: "info",
    origin: "Transcripto",
    page: "/explorer/view/Transcripto?session=run-1",
  });
  notify({
    title: "Reply with exactly one word: APPLE",
    detail: "Finished",
    tone: "info",
    origin: "Transcripto",
    page: "/explorer/view/Transcripto?session=run-2",
  });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(1);
  expect(retained[0]?.count).toBe(2);
  expect(retained[0]?.page).toBe("/explorer/view/Transcripto?session=run-2");
});

// Same caption, different titles — must NOT collapse just because they share
// a source. The title stays part of the family identity.
test("same caption but different titles stay as two separate rows", () => {
  notify({ title: "Task A finished", tone: "info", origin: "Transcripto", page: "/explorer/view/a" });
  notify({ title: "Task B finished", tone: "info", origin: "Transcripto", page: "/explorer/view/b" });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(2);
  expect(retained.every((n) => n.count === 1)).toBe(true);
});

// ---- familyKey opt-in (2026-09-18 fix, user: "these 2 fused-render
// notifications should have been grouped together as count") -----------------
//
// Two finished tasks in the same folder share a caption but usually have
// DIFFERENT titles ("hi", "New session"), so the ordinary caption+title
// family above never collapses them — that's the bug this field fixes. A
// caller opts in with `familyKey`; `messageFamily` prefers it outright over
// caption/page/title when present.

test("two notices with the same familyKey collapse into one row with count 2, showing the newer title and page (familyKey opt-in)", () => {
  notify({
    title: "hi",
    detail: "Finished",
    tone: "info",
    origin: "fused-render",
    page: "/explorer/view/fused-render?session=run-1",
    familyKey: "task-finished:fused-render",
  });
  notify({
    title: "New session",
    detail: "Finished",
    tone: "info",
    origin: "fused-render",
    page: "/explorer/view/fused-render?session=run-2",
    familyKey: "task-finished:fused-render",
  });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(1);
  expect(retained[0]?.count).toBe(2);
  expect(retained[0]?.title).toBe("New session");
  expect(retained[0]?.page).toBe("/explorer/view/fused-render?session=run-2");
});

// A finished-task notice (familyKey set) and an unrelated non-task notice
// (no familyKey) sharing the SAME caption must NOT collapse just because
// they're in the same folder — familyKey is scoped to the shape that opts
// in, not a loosening of everyone's default caption+title identity.
test("a familyKey notice and an unrelated non-familyKey notice sharing a caption do not collapse", () => {
  notify({
    title: "New session",
    detail: "Finished",
    tone: "info",
    origin: "fused-render",
    page: "/explorer/view/fused-render?session=run-1",
    familyKey: "task-finished:fused-render",
  });
  notify({
    title: "Something went wrong",
    tone: "error",
    origin: "fused-render",
    page: "/explorer/view/fused-render",
  });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(2);
  expect(retained.every((n) => n.count === 1)).toBe(true);
});

// Same title, different captions — two different sources doing the same
// kind of work must not be conflated into one row.
test("same title but different captions stay as two separate rows", () => {
  notify({ title: "Reply with exactly one word: APPLE", tone: "info", origin: "Transcripto", page: "/a" });
  notify({ title: "Reply with exactly one word: APPLE", tone: "info", origin: "OtherApp", page: "/b" });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(2);
  expect(retained.every((n) => n.count === 1)).toBe(true);
});

test("a suppressed replaceId call clears whatever that id was still showing", () => {
  const id = notify({ title: "Installing…", tone: "info", source: "/claude-config" });
  expect(popupSnapshot()?.title).toBe("Installing…");
  // The source comes into focus mid-flight (a repeat call updating the same
  // popup) — the still-showing card must not be left stale.
  const result = notify({ title: "Installing…", tone: "info", source: "/" }, id);
  expect(result).toBe(id);
  // Started its exit animation (same "leaving", not an instant vanish, every
  // other dismiss in this store uses) rather than being left to sit forever.
  expect(getPopupNotification()?.leaving).toBe(true);
});

// ---- F1 (2026-09-18 fix, code review round): ingest retains/collapses
// WITHOUT popping ------------------------------------------------------------
//
// The pane that raised this message already popped its OWN card, in its own
// corner (App.tsx's `!IS_EMBED` guard means only a pane's own document runs
// the local `notify()` call that pops it). If the shell's ingest handler also
// called `notify()`, the shell would pop a SECOND, identical card for the
// same event, and "latest wins" would let it silently evict — and cancel the
// exit timer of — whatever card the shell itself happened to be showing.

test("an ingested message is retained and collapses but never pops a card in the receiving document (F1)", () => {
  const ingest = (globalThis as unknown as { _fusedIngestNotification: (input: unknown) => number })
    ._fusedIngestNotification;

  expect(getPopupNotification()).toBeNull();
  const id = ingest({ title: "pane error", tone: "error" });

  // Retained (and collapse-eligible), exactly like a local notify() call.
  expect(getRetainedNotifications().map((n) => n.title)).toEqual(["pane error"]);
  expect(typeof id).toBe("number");
  // But NOT popped — that is the whole point of F1.
  expect(getPopupNotification()).toBeNull();
});

test("an ingested message does not evict a popup the receiving document is already showing (F1)", () => {
  const ingest = (globalThis as unknown as { _fusedIngestNotification: (input: unknown) => number })
    ._fusedIngestNotification;

  notify({ title: "local notice", tone: "error" });
  expect(getPopupNotification()?.title).toBe("local notice");

  ingest({ title: "forwarded notice", tone: "error" });

  // The shell's own popup is untouched — the forwarded message only landed
  // in the retained list, it never became "latest wins" popup content.
  expect(getPopupNotification()?.title).toBe("local notice");
  expect(getRetainedNotifications().map((n) => n.title).sort()).toEqual([
    "forwarded notice",
    "local notice",
  ]);
});

test("two ingested messages sharing a family still collapse into one retained row even though neither ever pops (F1 + existing collapse contract)", () => {
  const ingest = (globalThis as unknown as { _fusedIngestNotification: (input: unknown) => number })
    ._fusedIngestNotification;
  const id1 = ingest({ title: "Task finished", tone: "info", origin: "fused-render", page: "/tasks/1" });
  const id2 = ingest({ title: "Task finished", tone: "info", origin: "fused-render", page: "/tasks/1" });
  expect(id2).toBe(id1);
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(1);
  expect(retained[0]?.count).toBe(2);
  expect(getPopupNotification()).toBeNull();
});

// Ingest path collapses by familyKey the same way a local notify() call does
// — retainAndCollapse() is the shared code both go through, so this is
// mostly a contract check that the ingest boundary doesn't strip the field.
test("two ingested finished-task notices with the same familyKey but different titles collapse into one row (familyKey opt-in via ingest)", () => {
  const ingest = (globalThis as unknown as { _fusedIngestNotification: (input: unknown) => number })
    ._fusedIngestNotification;
  ingest({
    title: "hi",
    tone: "info",
    origin: "fused-render",
    page: "/tasks/run-1",
    familyKey: "task-finished:fused-render",
  });
  ingest({
    title: "New session",
    tone: "info",
    origin: "fused-render",
    page: "/tasks/run-2",
    familyKey: "task-finished:fused-render",
  });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(1);
  expect(retained[0]?.count).toBe(2);
  expect(retained[0]?.title).toBe("New session");
});

// ---- F2 (2026-09-18 fix, code review round): no runaway self-forward ------
//
// `IS_TOP_EMBED` (router.ts) is `IS_EMBED && window === window.top &&
// !IS_PREVIEW && !IS_SNAPSHOT` — so a TOP-LEVEL window loaded at an embed URL
// with `_preview=1`/`snapshot=1` is `IS_EMBED` but NOT `IS_TOP_EMBED`, and
// `forwardToShell`'s `!effectiveIsEmbed() || effectiveIsTopEmbed()` guard does
// not fire for it. `window.top` for that document IS the document itself, so
// without a separate structural guard: notify() -> forwardToShell() -> its
// OWN `_fusedIngestNotification` -> notify() -> forwardToShell() -> ...
// forever.

test("forwardToShell refuses to forward to itself when window.top === window, even when IS_EMBED but not IS_TOP_EMBED (F2)", () => {
  _setIsEmbedForTest(true);
  _setIsTopEmbedForTest(false); // models a top-level window at an embed URL with _preview=1/snapshot=1
  (globalThis.window as unknown as Record<string, unknown>).top = globalThis.window;

  let ingestCalls = 0;
  const realIngest = (globalThis as unknown as Record<string, unknown>)
    ._fusedIngestNotification as (input: unknown) => number;
  (globalThis as unknown as Record<string, unknown>)._fusedIngestNotification = (input: unknown) => {
    ingestCalls++;
    return realIngest(input);
  };

  try {
    expect(() => notify({ title: "self", tone: "error" })).not.toThrow();
    // Must never call its own ingest handler — there is nothing above this
    // document to forward to, whatever IS_EMBED/IS_TOP_EMBED say.
    expect(ingestCalls).toBe(0);
    // The message still lands locally, exactly as an ordinary top-level
    // notify would — the guard only suppresses the (self-)forward, not the
    // notice.
    expect(getPopupNotification()?.title).toBe("self");
  } finally {
    (globalThis as unknown as Record<string, unknown>)._fusedIngestNotification = realIngest;
  }
});
