// useTaskStatusNotify's §5 wiring: narrator-only diffing of the existing
// task-status poll, driving task-status-notify.ts's decision table. Follows
// scheduleEvents.test.ts's own pattern (real presence/notifications modules,
// fetch stubbed directly — mock.module is process-wide and the wrong tool
// here, per appdoctor-lib.test.ts's header), but drives `tasksPulse.ts`'s
// real store via its own `publishTasks` escape hatch (the same one the Tasks
// page uses to hand the shared pulse a known-fresh answer) rather than
// waiting out its poll timers.
import { beforeEach, describe, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const presenceStore = new Map<string, string>();
(globalThis as unknown as { localStorage: Storage }).localStorage = {
  getItem: (k: string) => (presenceStore.has(k) ? (presenceStore.get(k) as string) : null),
  setItem: (k: string, v: string) => {
    presenceStore.set(k, v);
  },
  removeItem: (k: string) => {
    presenceStore.delete(k);
  },
  clear: () => presenceStore.clear(),
  key: () => null,
  length: 0,
} as Storage;

// The pulse store's own poll would otherwise fire against a real endpoint —
// answer the pulse read with an empty pull-your-own-answers response, and
// anything else (the listing feed's read/long-poll) with a promise that never
// resolves (tasksPulse.lane.test.tsx's own pattern for keeping that feed out
// of the way of a test that only cares about the pulse rows).
globalThis.fetch = ((url: string) =>
  String(url).includes("/api/tasks/pulse")
    ? Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks: [] }) } as unknown as Response)
    : new Promise<Response>(() => {})) as unknown as typeof fetch;

const { useTaskStatusNotify } = await import("@shell/useTaskStatusNotify");
const { publishTasks } = await import("@shell/tasksPulse");
const { _resetNotificationsForTest, getPopupNotification, getRetainedNotifications } =
  await import("@platform/lib/notifications");
import type { TaskPulseTask } from "@platform/lib/api";

const PRESENCE_KEY = "fused-render:presence";

/** Plants a top-level entry sorting BEFORE this document's own minted
 *  windowId — making isNarrator() false here, same as scheduleEvents.test.ts. */
function plantForeignNarrator(): void {
  const raw = presenceStore.get(PRESENCE_KEY);
  const map = raw ? JSON.parse(raw) : {};
  map["a-foreign"] = { page: "", focused: true, ts: Date.now(), topLevel: true };
  presenceStore.set(PRESENCE_KEY, JSON.stringify(map));
}

function task(over: Partial<TaskPulseTask> = {}): TaskPulseTask {
  return {
    key: "t1",
    status: "in_progress",
    unread: 0,
    last_active: 0,
    happened_at: 0,
    project: "/proj",
    task_id: "T097",
    title: "update the changelog",
    target: "/proj",
    session_id: "",
    ...over,
  } as TaskPulseTask;
}

function mountHook(): { unmount: () => void } {
  let renderer!: ReactTestRenderer;
  const Probe = (): null => {
    useTaskStatusNotify();
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

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

async function publish(rows: TaskPulseTask[]): Promise<void> {
  await act(async () => {
    publishTasks(rows);
    await Promise.resolve();
  });
}

beforeEach(() => {
  presenceStore.clear();
  _resetNotificationsForTest();
});

describe("useTaskStatusNotify", () => {
  test("a task's first sighting never raises a notification", async () => {
    const h = mountHook();
    await flush();
    await publish([task({ status: "needs_attention" })]);
    expect(getPopupNotification()).toBeNull();
    expect(getRetainedNotifications()).toEqual([]);
    h.unmount();
  });

  test("in_progress -> needs_attention pops a plain, non-retained alert", async () => {
    const h = mountHook();
    await flush();
    await publish([task({ status: "in_progress" })]);
    await publish([task({ status: "needs_attention" })]);

    const popup = getPopupNotification();
    expect(popup?.title).toContain("needs your input");
    expect(popup?.tone).toBeUndefined();
    expect(getRetainedNotifications()).toEqual([]); // attentionRows owns retention, not this
    h.unmount();
  });

  test("in_progress -> blocked pops and retains a never-suppressed failure", async () => {
    const h = mountHook();
    await flush();
    await publish([task({ status: "in_progress" })]);
    await publish([task({ status: "blocked" })]);

    const retained = getRetainedNotifications();
    expect(retained.length).toBe(1);
    expect(retained[0].tone).toBe("error");
    expect(retained[0].page).toBeDefined();
    h.unmount();
  });

  // REVERSED 2026-09-16 (user: "the user does want to open the app along
  // with claude template to go back") — see task-status-notify.ts's own
  // header comment. `page` retains the row.
  //
  // SECOND REVERSAL, 2026-09-17: no `source` any more, so this popup is never
  // presence-suppressed, and there is no "Recent" section left to land in —
  // the row is an ordinary retained row like any other.
  test("in_progress -> done pops a never-suppressed notice that is retained and clickable", async () => {
    const h = mountHook();
    await flush();
    await publish([task({ status: "in_progress" })]);
    await publish([task({ status: "done" })]);

    // THIRD REVERSAL, 2026-09-17 (code review round): "finished" moved out
    // of the title into `detail`, and the caption (`origin`) is restored —
    // see task-status-notify.ts's own header comment on the regression this
    // closes (dropping `source` above also silently deleted the caption,
    // since nothing else fed it).
    const popup = getPopupNotification();
    expect(popup?.title).not.toContain("finished");
    expect(popup?.detail).toBe("Finished");
    expect(popup?.origin).toBeTruthy();
    expect(popup?.tone).toBe("info");

    const retained = getRetainedNotifications();
    expect(retained.length).toBe(1);
    expect(retained[0].page).toBeDefined();
    h.unmount();
  });

  test("a non-narrator window never raises anything", async () => {
    plantForeignNarrator();
    const h = mountHook();
    await flush();
    await publish([task({ status: "in_progress" })]);
    await publish([task({ status: "blocked" })]);

    expect(getPopupNotification()).toBeNull();
    expect(getRetainedNotifications()).toEqual([]);
    h.unmount();
  });

  // Finding 9 (code review 2026-09-16): a non-narrator window must keep
  // tracking each task's column silently so that the moment it BECOMES the
  // narrator (the old one's tab closed), a transition landing on that very
  // tick is recognized correctly rather than looking like a first sighting.
  test("a transition landing on the narrator-handoff tick is still notified, not dropped", async () => {
    plantForeignNarrator();
    const h = mountHook();
    await flush();
    // While non-narrator: in_progress -> blocked happens silently. The old,
    // buggy code never touched `prev` here at all.
    await publish([task({ status: "in_progress" })]);
    await publish([task({ status: "blocked" })]);
    expect(getPopupNotification()).toBeNull();
    expect(getRetainedNotifications()).toEqual([]);

    // The foreign narrator's tab closes — this window is elected narrator.
    presenceStore.delete(PRESENCE_KEY);

    // The very next tick both hands this window the narrator role AND
    // carries a real transition (blocked -> needs_attention). Without the
    // fix, `prev` was never seeded with "blocked" while this window was
    // non-narrator, so this reads as a first sighting of the task and
    // notifies nothing.
    await publish([task({ status: "needs_attention" })]);

    const popup = getPopupNotification();
    expect(popup?.title).toContain("needs your input");
    h.unmount();
  });
});
