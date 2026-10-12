// useModelConflictNotify's own contract (SPEC-model-slot-conflict.md §4):
// one toast per conflict episode, with the two-action mapping this file's
// own header explains (`Keep <the other model>` / `Stop both`), driven off
// `aiRuntime.ts`'s real store via its `publishAiRuntime` escape hatch — the
// same approach `useTaskStatusNotify.test.ts` takes with `publishTasks`.
import { afterAll, beforeEach, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const realFetch = globalThis.fetch;
// `resolveModelConflict` (api.ts) is what a toast action calls — never
// actually clicked in these tests, but a never-settling stub keeps any
// stray call from reaching a real network during the test run, matching
// useTaskStatusNotify.test.ts's own rationale for its fetch stub.
globalThis.fetch = (() => new Promise<Response>(() => {})) as unknown as typeof fetch;

const { useModelConflictNotify } = await import("@shell/useModelConflictNotify");
const { publishAiRuntime } = await import("@apps/ai_models/lib/aiRuntime");
const { _resetNotificationsForTest, getPopupNotification } = await import("@platform/lib/notifications");
import type { AiConflict, AiRuntime } from "@platform/lib/api";

afterAll(() => {
  globalThis.fetch = realFetch;
});

beforeEach(() => {
  _resetNotificationsForTest();
});

function runtime(conflicts: AiConflict[]): AiRuntime {
  return {
    runners: [],
    loaded: [],
    downloading: [],
    conflicts,
    totalResidentBytes: null,
    memoryCeilingBytes: null,
  };
}

const conflict = (over: Partial<AiConflict> = {}): AiConflict => ({
  capability: "text-generation",
  models: ["org/a", "org/b"],
  resident: "org/b",
  since: 0,
  held: false,
  ...over,
});

function mountHook(): { unmount: () => void } {
  let renderer!: ReactTestRenderer;
  const Probe = (): null => {
    useModelConflictNotify();
    return null;
  };
  act(() => {
    renderer = create(createElement(Probe));
  });
  return {
    unmount: () => {
      act(() => {
        renderer.unmount();
      });
    },
  };
}

async function publish(conflicts: AiConflict[]): Promise<void> {
  await act(async () => {
    publishAiRuntime(runtime(conflicts));
    await Promise.resolve();
  });
}

/** Mounts the hook AND flushes away whatever `aiRuntime.ts`'s module-level
 *  `current` was left holding by an earlier test in this same process
 *  (`aiRuntime.ts`, like `tasksPulse.ts`, is one module-level store for the
 *  whole run — see useTaskStatusNotify.test.ts's own comment on exactly this
 *  constraint). Publishing `[]` right after mount marks nothing as notified
 *  (an empty list has nothing to notify about) while clearing any leaked
 *  conflict out of the hook's own "seen" bookkeeping, so each test starts
 *  from a genuinely fresh standoff. */
async function mountClean(): Promise<{ unmount: () => void }> {
  const mounted = mountHook();
  await publish([]);
  _resetNotificationsForTest();
  return mounted;
}

test("useModelConflictNotify guards its own effect against IS_EMBED before it can raise anything", () => {
  const src = readFileSync(join(import.meta.dir, "useModelConflictNotify.ts"), "utf8");
  expect(src).toContain('import { IS_EMBED, IS_TOP_EMBED } from "@platform/lib/router";');
  expect(src.indexOf("if (IS_EMBED && !IS_TOP_EMBED) return;")).toBeGreaterThan(-1);
});

test("a freshly detected conflict raises exactly one toast, with Keep <other> and Stop both", async () => {
  const mounted = await mountClean();
  await publish([conflict()]);

  const popup = getPopupNotification();
  expect(popup).not.toBeNull();
  // repoName() trims the owner, same as every other model-id display on this
  // bar — the title names both contenders by that short form.
  expect(popup?.title).toContain("a and b");
  expect(popup?.title).toContain("text generation");
  // resident is org/b, so the toast's own "keep the other one" offers org/a.
  expect(popup?.action?.label).toBe("Keep a");
  expect(popup?.extraAction?.label).toBe("Stop both");

  mounted.unmount();
});

test("the same standing conflict does not raise a second toast on a later poll", async () => {
  const mounted = await mountClean();
  await publish([conflict()]);
  _resetNotificationsForTest(); // clears the retained/popup state, NOT the hook's own "seen" map
  await publish([conflict()]); // same capability, still standing — no new toast
  expect(getPopupNotification()).toBeNull();
  mounted.unmount();
});

test("a conflict entering already-held (a pre-emptive resolve) raises no toast", async () => {
  const mounted = await mountClean();
  await publish([conflict({ held: true, resident: "org/a" })]);
  expect(getPopupNotification()).toBeNull();
  mounted.unmount();
});

test("a conflict clearing and later recurring on the same capability raises a fresh toast", async () => {
  const mounted = await mountClean();
  await publish([conflict()]);
  expect(getPopupNotification()).not.toBeNull();

  _resetNotificationsForTest();
  await publish([]); // resolved/cleared
  expect(getPopupNotification()).toBeNull();

  await publish([conflict({ models: ["org/c", "org/d"], resident: "org/d" })]);
  const popup = getPopupNotification();
  expect(popup).not.toBeNull();
  expect(popup?.action?.label).toBe("Keep c");

  mounted.unmount();
});
