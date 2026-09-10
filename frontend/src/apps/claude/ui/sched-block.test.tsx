// The banner, and the two-press arm behind its one button. The RULES are tested
// next door (`sched/scheduled.test.ts`); this drives the seam — a repeat armed
// by one press and disarmed by a gesture, a one-off cancelled in one, and the
// refusal that survives the poll the same click asks for.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, beforeAll, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";

// --- the module boundary ----------------------------------------------------
type Entry = import("../sched/scheduled").SchedEntry;
type Task = import("../sched/scheduled").SchedTask;
let entries: Entry[] = [];
let tasks: Task[] = [];
let cancelFails = false;
const cancelled: string[] = [];

/** The three endpoint calls, HANDED IN rather than module-mocked: `bun test`
 *  runs every suite in one process, so replacing `@platform/lib/api` here
 *  replaces it for every suite loaded after this one. */
const schedApi = {
  getSchedule: () => Promise.resolve({ entries }),
  getTasks: () => Promise.resolve({ tasks }),
  cancelScheduledMessage: (id: string) => {
    cancelled.push(id);
    if (cancelFails) return Promise.reject(new Error("404"));
    // The STORE is the source of truth and the click's own `tick()` re-reads it,
    // so a cancel that does not actually remove the entry would have the poll
    // put the block straight back — which is exactly what the server does not
    // do, and what the local unblock is measured against.
    entries = entries.filter((e) => e.id !== id && e.template_id !== id);
    return Promise.resolve({ entry: {} });
  },
};

/**
 * A REAL LISTENER REGISTRY on the shim's `document`. The shim's own
 * `addEventListener` is a no-op — right for a renderer that has no document —
 * but the disarm gestures ARE document-level bindings, and a test that cannot
 * fire them can only assert that the state exists, not that the gesture spends
 * it.
 */
/** The shim has no store; the hop writes through a try/catch either way. */
const stored: Record<string, string> = {};
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => stored[k] ?? null,
  setItem: (k: string, v: string) => {
    stored[k] = v;
  },
  removeItem: (k: string) => delete stored[k],
};

const docListeners: Record<string, ((ev: unknown) => void)[]> = {};
const realDoc = {
  add: document.addEventListener,
  remove: document.removeEventListener,
};
/** INSTALLED AND REMOVED AROUND THIS FILE. `bun test` runs every suite in ONE
 *  process and this patch is on a GLOBAL: left in place it swallows the
 *  listeners of every suite that loads after it (the task-number hook's
 *  `tasks-changed` binding, measured). */
beforeAll(() => {
  (document as unknown as Record<string, unknown>).addEventListener = (
    type: string,
    fn: (ev: unknown) => void,
  ) => {
    (docListeners[type] ||= []).push(fn);
  };
  (document as unknown as Record<string, unknown>).removeEventListener = (
    type: string,
    fn: (ev: unknown) => void,
  ) => {
    const list = docListeners[type] || [];
    const at = list.indexOf(fn);
    if (at >= 0) list.splice(at, 1);
  };
});
afterAll(() => {
  (document as unknown as Record<string, unknown>).addEventListener = realDoc.add;
  (document as unknown as Record<string, unknown>).removeEventListener = realDoc.remove;
});
const fire = (type: string, ev: Record<string, unknown>) => {
  for (const fn of [...(docListeners[type] || [])]) fn(ev);
};

const { SchedBlock } = await import("./SchedBlock");
const { useSchedule } = await import("../sched/useSchedule");
type ScheduleState = import("../sched/useSchedule").ScheduleState;
type ChatController = import("../protocol/controller-api").ChatController;

/** Only the four members the hook reaches for. */
function stubController(over: Partial<ChatController> = {}): ChatController {
  return {
    isBusy: () => false,
    addNote: () => {},
    resumeRun: () => Promise.resolve(),
    ...over,
  } as unknown as ChatController;
}

function mount(over: { navLocked?: boolean; onNavigate?(url: string): void } = {}) {
  let api: ScheduleState | null = null;
  /** The 15 s poll, captured instead of waited out: `poll()` below is one tick
   *  of the real watcher, which is what re-reads the store. */
  const ticks: (() => void)[] = [];
  const Harness = () => {
    const sched = useSchedule({
      controller: stubController(),
      file: "/proj",
      sessionId: "s1",
      inChat: true,
      setRunParam: () => {},
      api: schedApi,
      timers: {
        setInterval: (fn) => {
          ticks.push(fn);
          return ticks.length;
        },
        clearInterval: () => {},
      },
      ...over,
    });
    api = sched;
    return createElement(SchedBlock, {
      blockers: sched.blockers,
      rec: sched.rec,
      armed: sched.armed,
      refused: sched.refused,
      stopping: sched.stopping,
      tick: sched.tick,
      onStop: sched.onStop,
      onRow: sched.onRow,
      cardRef: sched.cardRef,
    });
  };
  let tree: ReactTestRenderer;
  act(() => {
    tree = create(createElement(Harness));
  });
  return {
    tree: tree!,
    poll: () => {
      for (const fn of ticks) fn();
    },
    get api() {
      return api!;
    },
  };
}

const flush = async () => {
  for (let i = 0; i < 8; i++) await act(async () => {});
};

const texts = (tree: ReactTestRenderer, cls: string): string[] =>
  tree.root
    .findAll((n) => typeof n.type === "string" && String(n.props.className || "") === cls, {
      deep: true,
    })
    .map((n) => n.children.filter((c) => typeof c === "string").join(""));

const acts = (tree: ReactTestRenderer) =>
  tree.root.find(
    (n) => typeof n.type === "string" && n.props.className === "sb-acts",
  ).children[0] as unknown as { props: Record<string, unknown> };

test("NOTHING BLOCKING IS NOTHING DRAWN — no empty strip above an open composer", async () => {
  entries = [];
  tasks = [];
  const m = mount();
  await flush();
  expect(m.tree.toJSON()).toBeNull();
  expect(m.api.blocked).toBe(false);
  expect(m.api.reason).toBe("");
});

test("a one-off draws the reason, the row and one press's worth of button", async () => {
  entries = [
    { id: "e1", state: "pending", session_id: "s1", due: "2099-01-01T09:00:00Z" },
  ];
  tasks = [{ key: "s1", task_id: "TASK-007", title: "Nightly tidy", status: "upcoming" }];
  const m = mount();
  await flush();
  expect(texts(m.tree, "sb-when")).toEqual([
    "Blocked — a scheduled message runs in this chat.",
  ]);
  // The listing's own name and number, and the ring's default state.
  expect(texts(m.tree, "sb-id")).toEqual(["TASK-007"]);
  expect(texts(m.tree, "sb-name")).toEqual(["Nightly tidy"]);
  expect(texts(m.tree, "sb-meta")[0].startsWith("Upcoming · ")).toBe(true);
  const stop = acts(m.tree);
  expect(stop.props.children).toBe("Cancel this message");
  // ONE PRESS for a one-off: cancelling loses one message the user can schedule
  // again, and a confirm on that would be ceremony.
  await act(async () => {
    (stop.props.onClick as () => void)();
  });
  await flush();
  expect(cancelled).toContain("e1");
  // Applied LOCALLY so the box opens on the click rather than 15 s later.
  expect(m.api.blocked).toBe(false);
});

test("A REPEAT TAKES TWO PRESSES, and the second one names the loss", async () => {
  cancelled.length = 0;
  entries = [
    {
      id: "occ",
      state: "pending",
      session_id: "s1",
      template_id: "tmpl",
      due: "2099-01-01T09:00:00Z",
      message: "run the report",
    },
  ];
  tasks = [];
  const m = mount();
  await flush();
  // No listing row: the message titles itself, and the number's cell is empty
  // rather than holding a gap open.
  expect(texts(m.tree, "sb-name")).toEqual(["run the report"]);
  expect(texts(m.tree, "sb-id")).toEqual([""]);
  let stop = acts(m.tree);
  expect(stop.props.children).toBe("Stop the repeat");
  expect(stop.props.className).toBeUndefined();
  await act(async () => {
    (stop.props.onClick as () => void)();
  });
  // ARMED: the label becomes the question, in place, because nothing on this
  // page can put those runs back.
  stop = acts(m.tree);
  expect(stop.props.children).toBe("Cancel every future run");
  expect(stop.props.className).toBe("armed");
  expect(cancelled).toEqual([]);
  await act(async () => {
    (stop.props.onClick as () => void)();
  });
  await flush();
  // The TEMPLATE, not the occurrence: cancelling the occurrence moves the block.
  expect(cancelled).toEqual(["tmpl"]);
  expect(m.api.blocked).toBe(false);
});

async function armed() {
  cancelled.length = 0;
  entries = [
    { id: "occ", state: "pending", session_id: "s1", repeats: "0 9 * * *", message: "x" },
  ];
  tasks = [];
  const m = mount();
  await flush();
  await act(async () => {
    (acts(m.tree).props.onClick as () => void)();
  });
  expect(m.api.armed).toBe(true);
  return m;
}

test("ESCAPE DISARMS, and CONSUMES the key so no other claimant sees it", async () => {
  const m = await armed();
  let stopped = false;
  let prevented = false;
  await act(async () => {
    fire("keydown", {
      key: "Escape",
      stopPropagation: () => {
        stopped = true;
      },
      preventDefault: () => {
        prevented = true;
      },
    });
  });
  expect(m.api.armed).toBe(false);
  // The document-level Escape binding has claimants of its own, and backing out
  // of a half-pressed confirm must not also trip one of them.
  expect(stopped).toBe(true);
  expect(prevented).toBe(true);
  // Disarming is always the safe direction to be wrong in: the cost of a lost
  // arm is one more press, the cost of a stale one is every future run.
  expect(cancelled).toEqual([]);
});

test("A PRESS OUTSIDE DISARMS; a press inside the card does not", async () => {
  const m = await armed();
  const inside = { nodeType: 1 };
  const outside = { nodeType: 1 };
  // `contains` is the CARD's own answer; the shim gives the ref no element, so
  // the card states which node is its own.
  (m.api.cardRef as { current: unknown }).current = {
    contains: (n: unknown) => n === inside,
  };
  await act(async () => {
    fire("pointerdown", { target: inside });
  });
  expect(m.api.armed).toBe(true);
  await act(async () => {
    fire("pointerdown", { target: outside });
  });
  expect(m.api.armed).toBe(false);
  expect(cancelled).toEqual([]);
});

test("A REFUSED CANCEL keeps the box shut and says why, keyed to the entry", async () => {
  cancelled.length = 0;
  cancelFails = true;
  entries = [{ id: "e1", state: "pending", session_id: "s1" }];
  tasks = [];
  const m = mount();
  await flush();
  await act(async () => {
    (acts(m.tree).props.onClick as () => void)();
  });
  await flush();
  cancelFails = false;
  // The entry is away, the composer stays shut, and that is the true state.
  expect(m.api.blocked).toBe(true);
  expect(m.api.refused).toBe(true);
  expect(texts(m.tree, "sb-note")).toEqual(["Still scheduled — it may already be running."]);
});

test("the row's hop remembers the calendar and lands on /tasks", async () => {
  entries = [{ id: "e1", state: "pending", session_id: "s1" }];
  tasks = [];
  const hops: string[] = [];
  const m = mount({ onNavigate: (u) => hops.push(u) });
  await flush();
  const row = m.tree.root.find(
    (n) => typeof n.type === "string" && n.props.className === "sb-row",
  );
  await act(async () => {
    (row.props.onClick as () => void)();
  });
  expect(hops).toEqual(["/tasks"]);
  expect(localStorage.getItem("fused-render:scheduled-view")).toBe("calendar");
});

test("N MORE AFTER IT: the soonest is named, the rest counted", async () => {
  entries = [
    { id: "a", state: "pending", session_id: "s1", due: "2099-01-01T09:00:00Z" },
    { id: "b", state: "pending", session_id: "s1", due: "2099-01-02T09:00:00Z" },
    { id: "c", state: "pending", session_id: "s1", due: "2099-01-03T09:00:00Z" },
  ];
  tasks = [];
  const m = mount();
  await flush();
  expect(texts(m.tree, "sb-when")).toEqual([
    "Blocked — a scheduled message runs in this chat. 2 more after it.",
  ]);
});

test("the mode's lock closes the CALENDAR and nothing else", async () => {
  entries = [];
  tasks = [];
  const m = mount({ navLocked: true });
  await flush();
  // The box stays live — the notes are for this chat — but scheduling LEAVES
  // for the Tasks view, so that door is the one that shuts.
  expect(m.api.blocked).toBe(false);
  expect(m.api.schedDisabled).toBe(true);
  expect(m.api.reason).toBe("finish or discard the notes first");
});

test("THE DUE BOUNDARY IS CROSSED IN PLACE: same entry, new when-text", async () => {
  // T repaints the whole card on every 15 s poll, which is how `.sb-meta`
  // crosses from "09:00 today" to "any moment now" (T:16921-16931). Dedupe the
  // published rows on ID ORDER ALONE and the row shows a stale time for the
  // life of the pendency — the id set never changes, so no repaint ever lands.
  const soon = new Date(Date.now() + 60_000).toISOString();
  entries = [{ id: "e9", state: "pending", session_id: "s1", due: soon }];
  tasks = [{ key: "s1", task_id: "TASK-050", title: "Nightly tidy", status: "upcoming" }];
  const m = mount();
  await flush();
  expect(texts(m.tree, "sb-meta")[0]).not.toBe("Upcoming · any moment now");
  expect(texts(m.tree, "sb-meta")[0].endsWith(" today")).toBe(true);

  // The SAME id, past due now. One poll later the row has to say so.
  entries = [
    {
      id: "e9",
      state: "pending",
      session_id: "s1",
      due: new Date(Date.now() - 60_000).toISOString(),
    },
  ];
  await act(async () => {
    m.poll();
  });
  await flush();
  expect(texts(m.tree, "sb-meta")[0]).toBe("Upcoming · any moment now");
  // ...and the listing row it was labelled from is NOT refetched: the pendency
  // is the same one, which is what the id dedupe is actually for.
  expect(texts(m.tree, "sb-id")).toEqual(["TASK-050"]);
});

test("THE CLOCK ALONE CROSSES IT: nothing about the entry changes", async () => {
  // The half of the boundary the id/`due`/`state` dedupe cannot see. The entry
  // is byte-for-byte the SAME OBJECT on both polls — what moves is the wall
  // clock, and "14:01 today" is only true until 14:01. So the poll publishes
  // the clock it saw (`useSchedule.tick`) beside the rows it deduped, and the
  // cell is read against that rather than against whichever render the entry
  // happened to trigger. Without it the row says "14:01 today" for the whole
  // life of the pendency and never announces the run it is holding the box for.
  const realNow = Date.now;
  try {
    // A FIXED CLOCK, so the 24-hour cell and the "today" gap are the same two
    // strings on every machine and in every timezone.
    const base = new Date(2026, 8, 9, 14, 0, 0).getTime();
    Date.now = () => base;
    const entry = {
      id: "e10",
      state: "pending",
      session_id: "s1",
      due: new Date(base + 60_000).toISOString(),
    };
    entries = [entry];
    tasks = [{ key: "s1", task_id: "TASK-051", title: "Nightly tidy", status: "upcoming" }];
    const m = mount();
    await flush();
    expect(texts(m.tree, "sb-meta")[0]).toBe("Upcoming · 14:01 today");
    const before = m.api.blockers;

    // ONE POLL LATER, past due — and `entries` is untouched: same array, same
    // object, same `due`, same `state`.
    Date.now = () => base + 120_000;
    await act(async () => {
      m.poll();
    });
    await flush();
    expect(texts(m.tree, "sb-meta")[0]).toBe("Upcoming · any moment now");
    // THE DEDUPE IS STILL DOING ITS JOB: the published array was not replaced
    // (that is the identity the render loop was guarded against), and the
    // listing row was not refetched for a pendency that never changed.
    expect(m.api.blockers).toBe(before);
    expect(m.api.blockers[0]).toBe(entry);
    expect(texts(m.tree, "sb-id")).toEqual(["TASK-051"]);
  } finally {
    Date.now = realNow;
  }
});

test("A HALF-PRESSED STOP DIES WITH ITS ENTRY (T:17091-17097, 17146)", async () => {
  cancelled.length = 0;
  entries = [
    {
      id: "occ-1",
      state: "pending",
      session_id: "s1",
      template_id: "tmpl-1",
      due: "2099-01-01T09:00:00Z",
      message: "run the report",
    },
  ];
  tasks = [];
  const m = mount();
  await flush();
  await act(async () => {
    (acts(m.tree).props.onClick as () => void)();
  });
  await flush();
  // Armed, not spent: a repeat takes two presses.
  expect(m.api.armed).toBe(true);
  expect(acts(m.tree).props.children).toBe("Cancel every future run");
  expect(cancelled).toEqual([]);

  // A DIFFERENT message comes to the front. The arm belonged to the one that
  // left, and a destructive button must never be found armed with no press
  // behind it — so it goes back cold rather than sitting one poll away from
  // spending a repeat nobody pressed against.
  entries = [
    {
      id: "occ-2",
      state: "pending",
      session_id: "s1",
      template_id: "tmpl-2",
      due: "2099-01-02T09:00:00Z",
      message: "a different repeat",
    },
  ];
  await act(async () => {
    m.poll();
  });
  await flush();
  expect(m.api.armed).toBe(false);
  expect(acts(m.tree).props.children).toBe("Stop the repeat");

  // AND THE ARM IS SPENT, not merely hidden. This is the whole hazard: comparing
  // the kept id against the front entry is enough to DISPLAY correctly, so a
  // stale arm shows nothing at all — until the very same entry comes back to
  // the front (the second occurrence ran, this one is soonest again), and then
  // one press spends every future run of a repeat nobody confirmed.
  entries = [
    {
      id: "occ-1",
      state: "pending",
      session_id: "s1",
      template_id: "tmpl-1",
      due: "2099-01-01T09:00:00Z",
      message: "run the report",
    },
  ];
  await act(async () => {
    m.poll();
  });
  await flush();
  expect(m.api.armed).toBe(false);
  expect(acts(m.tree).props.children).toBe("Stop the repeat");
  await act(async () => {
    (acts(m.tree).props.onClick as () => void)();
  });
  await flush();
  // The press ARMS again; it does not cancel.
  expect(cancelled).toEqual([]);
  expect(m.api.armed).toBe(true);

  // And the same on the way OUT: nothing blocking is nothing armed.
  entries = [];
  await act(async () => {
    m.poll();
  });
  await flush();
  expect(m.api.blocked).toBe(false);
  expect(m.api.armed).toBe(false);
});

test("THE BANNER DESCRIBES THE ENTRY, NOT THE TASK — a pending blocker inside a done task", async () => {
  // P4R1-3, measured five times on both stacks: `/api/tasks` reports `done` for
  // a task that holds a finished run AND a future pending message. True of the
  // board, false as a caption over the composer that message is holding shut —
  // the reader saw "Done · 13:26 today" above a dead box. And the name cell read
  // the CONVERSATION's title (the reported dots are literally the task's title,
  // typed) rather than the message that is coming.
  cancelled.length = 0;
  entries = [
    {
      id: "e9",
      state: "pending",
      session_id: "s1",
      due: "2099-01-01T09:00:00Z",
      message: "QA test scheduled message A1",
    },
  ];
  tasks = [
    {
      key: "s1",
      task_id: "TASK-104",
      title: ". . . . . . . . . . . . . . .",
      status: "done",
      failed: false,
    },
  ];
  const m = mount();
  await flush();
  expect(texts(m.tree, "sb-meta")[0].startsWith("Upcoming · ")).toBe(true);
  expect(texts(m.tree, "sb-name")).toEqual(["QA test scheduled message A1"]);
  // The number still comes from the listing — it is the one thing only
  // `/api/tasks` hands out.
  expect(texts(m.tree, "sb-id")).toEqual(["TASK-104"]);
  // ...and the ring wears the ENTRY's state, so the hue and the word agree.
  const ring = m.tree.root.find(
    (n) => typeof n.type === "string" && String(n.props.className || "").startsWith("sb-ring"),
  );
  expect(ring.props.className).toBe("sb-ring sb-ring--upcoming");
});
