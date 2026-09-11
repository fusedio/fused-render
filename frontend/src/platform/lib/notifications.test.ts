// The notification store's exit path, tiering rules and retention — the
// client-side counterpart to `jobs.test.ts`'s coverage of `effectiveTier`/
// `popupTick`. See SPEC-toasts-become-notifications.md and
// DECISIONS-toasts-become-notifications.md for the reasoning this codifies.
import { afterEach, beforeEach, expect, test } from "bun:test";

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
  _setIsTopEmbedForTest,
  dismissNotification,
  dismissPopup,
  getPopupNotification,
  getRetainedNotifications,
  notify,
} = await import("@platform/lib/notifications");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

beforeEach(_resetNotificationsForTest);
afterEach(() => {
  _resetNotificationsForTest();
  _setIsTopEmbedForTest(null);
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
