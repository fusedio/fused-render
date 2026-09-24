// The floating job pop-up's own lifecycle: it must stay up for
// `JOB_POPUP_VISIBLE_MS`, then play the same `TOAST_EXIT_MS` collapse
// `lib/notifications` uses before telling its parent it is gone — real
// timers, the same `sleep`/`afterExit` idiom `notifications.test.ts` uses for
// the identical shape of test, rather than mocked ones (this module reads
// `globalThis.setTimeout` directly, and bun:test has no fake-timer harness
// wired up for it).
import { expect, test } from "bun:test";
import { act, create } from "react-test-renderer";
import type { ReactTestRendererJSON } from "react-test-renderer";

import { installDomShim } from "@platform/lib/testDomShim";
import type { Job } from "@platform/lib/jobs";
import { JOB_POPUP_VISIBLE_MS, subscribeJobDismissed } from "@platform/lib/jobs";

// `lib/notifications` imports `router.ts`, which reads `location` at module
// scope — the shim must be installed before that import EVALUATES, not
// merely before this file's own statements run (static imports are
// evaluated before a module's own top-level code, regardless of where the
// `import` keyword sits in the file — see notifications.test.ts's identical
// comment and router.test.ts's own precedent for this pattern).
installDomShim();
const { default: JobPopupCard } = await import("@platform/ui/JobPopupCard");
const { TOAST_EXIT_MS } = await import("@platform/lib/notifications");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const JOB: Job = {
  id: "j1",
  title: "a red fox in snow",
  detail: "",
  model: "",
  kind: "task",
  state: "done",
  done: null,
  total: null,
  total_scope: "phase",
  total_estimated: false,
  unit: "",
  message: "",
  page: "",
  source: "",
  origin: "",
  owner: "page",
  cancellable: true,
  cancel_requested: false,
  started_at: 0,
  updated_at: 0,
  finished_at: 10,
  stalled: false,
  waiting_for: "",
  tier: "transient",
  group: "j1",
};

test("the card stays mounted for JOB_POPUP_VISIBLE_MS, then leaves, then calls onGone", async () => {
  let gone = false;
  let renderer: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(<JobPopupCard job={JOB} onGone={() => (gone = true)} />);
  });

  // Still up, not yet leaving, well before the visible window ends.
  await act(async () => {
    await sleep(Math.min(200, JOB_POPUP_VISIBLE_MS - 100));
  });
  expect(gone).toBe(false);
  let json = renderer!.toJSON() as ReactTestRendererJSON;
  expect(json.props.className).not.toContain("leaving");

  // Past the visible window, but not yet the exit animation — now leaving,
  // still mounted, `onGone` not yet called.
  await act(async () => {
    await sleep(JOB_POPUP_VISIBLE_MS - Math.min(200, JOB_POPUP_VISIBLE_MS - 100) + 40);
  });
  expect(gone).toBe(false);
  json = renderer!.toJSON() as ReactTestRendererJSON;
  expect(json.props.className).toContain("leaving");

  // Past the exit animation too — `onGone` has fired.
  await act(async () => {
    await sleep(TOAST_EXIT_MS + 60);
  });
  expect(gone).toBe(true);
}, JOB_POPUP_VISIBLE_MS + TOAST_EXIT_MS + 2000);

test("clicking the card (opening it) closes it early, through JobRow's own dismiss", async () => {
  // A job with somewhere to go, and a `dismissFn` stub standing in for the
  // real network call so the dismiss resolves deterministically — the same
  // test seam `JobRow.test.tsx` uses on `JobRow` directly, threaded through
  // `JobPopupCard` unchanged.
  let gone = false;
  const job: Job = { ...JOB, page: "/tmp/out.png" };
  let renderer: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(
      <JobPopupCard
        job={job}
        onGone={() => (gone = true)}
        dismissFn={async (id) => ({ dismissed: id })}
      />,
    );
  });

  const root = renderer!.root;
  const clickable = root.findAll(
    (node) => typeof node.props.onClick === "function" && node.props.role === "button",
  );
  expect(clickable.length).toBeGreaterThan(0);
  await act(async () => {
    clickable[0].props.onClick({ preventDefault() {}, stopPropagation() {} });
  });
  // Let the stubbed dismiss promise settle.
  await act(async () => {
    await sleep(20);
  });
  expect(gone).toBe(false); // still in its exit window, not yet fully gone
  const json = renderer!.toJSON() as ReactTestRendererJSON;
  expect(json.props.className).toContain("leaving");
}, 5000);

test("a row click's real dismiss reaches the panel through noteJobDismissed, not just the card's own leaving state", async () => {
  const dismissed: string[] = [];
  const unsubscribe = subscribeJobDismissed((id) => dismissed.push(id));
  const job: Job = { ...JOB, id: "sys:ai-image:reached", page: "/tmp/out.png" };
  let renderer: ReturnType<typeof create>;
  try {
    await act(async () => {
      renderer = create(
        <JobPopupCard
          job={job}
          onGone={() => {}}
          dismissFn={async (id) => ({ dismissed: id })}
        />,
      );
    });

    const root = renderer!.root;
    const clickable = root.findAll(
      (node) => typeof node.props.onClick === "function" && node.props.role === "button",
    );
    await act(async () => {
      clickable[0].props.onClick({ preventDefault() {}, stopPropagation() {} });
    });
    await act(async () => {
      await sleep(20);
    });

    expect(dismissed).toEqual([job.id]);
  } finally {
    unsubscribe();
  }
}, 5000);

// `globalThis`'s real `addEventListener`/`removeEventListener` (Bun's
// `globalThis` is a genuine `EventTarget`, unlike the shim's `window`/
// `document`, which are no-ops — see testDomShim.ts) work for real dispatch,
// but a dispatched `Event`'s `target` is always the dispatching object
// itself, and `target` is otherwise read-only — no way to aim one at an
// arbitrary "inside the card" marker. A tiny in-memory bus standing in for
// `globalThis`'s listener registry for the length of one test, the same
// technique `useTaskId.test.tsx` uses for `window`, lets a test dispatch a
// plain object shaped like a `PointerEvent` (any `target`, and spies in place
// of `preventDefault`/`stopPropagation`) without touching real DOM dispatch.
function liveGlobalEvents(): {
  restore: () => void;
  fire: (type: string, ev: Record<string, unknown>) => void;
  removeCount: (type: string) => number;
} {
  const g = globalThis as unknown as {
    addEventListener: (type: string, fn: (ev: unknown) => void, opts?: unknown) => void;
    removeEventListener: (type: string, fn: (ev: unknown) => void, opts?: unknown) => void;
  };
  const was = { add: g.addEventListener, remove: g.removeEventListener };
  const bus = new Map<string, Set<(ev: unknown) => void>>();
  const removed = new Map<string, number>();
  g.addEventListener = (type, fn) => {
    const set = bus.get(type) ?? new Set();
    set.add(fn);
    bus.set(type, set);
  };
  g.removeEventListener = (type, fn) => {
    bus.get(type)?.delete(fn);
    removed.set(type, (removed.get(type) ?? 0) + 1);
  };
  return {
    restore: () => {
      g.addEventListener = was.add;
      g.removeEventListener = was.remove;
    },
    fire: (type, ev) => {
      for (const fn of [...(bus.get(type) ?? [])]) fn(ev);
    },
    removeCount: (type) => removed.get(type) ?? 0,
  };
}

test("a click outside the card starts its exit animation", async () => {
  const bus = liveGlobalEvents();
  let renderer: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(<JobPopupCard job={JOB} onGone={() => {}} />);
  });

  const preventDefault = () => {
    throw new Error("must never be called — an outside press must reach whatever it hit");
  };
  const stopPropagation = () => {
    throw new Error("must never be called — an outside press must keep bubbling");
  };
  await act(async () => {
    bus.fire("click", { target: {}, preventDefault, stopPropagation });
  });

  const json = renderer!.toJSON() as ReactTestRendererJSON;
  expect(json.props.className).toContain("leaving");
  bus.restore();
});

test("a click inside the card is ignored", async () => {
  const bus = liveGlobalEvents();
  const marker = { id: "inside" };
  const cardNode = { contains: (n: unknown) => n === marker };
  let renderer: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(<JobPopupCard job={JOB} onGone={() => {}} />, {
      createNodeMock: () => cardNode,
    });
  });

  await act(async () => {
    bus.fire("click", { target: marker, preventDefault() {}, stopPropagation() {} });
  });

  const json = renderer!.toJSON() as ReactTestRendererJSON;
  expect(json.props.className).not.toContain("leaving");
  bus.restore();
});

test("a click on a toast action button elsewhere in .notif-host is ignored, not stolen", async () => {
  // The concrete failure this guards: a toast rendered ABOVE this card in the
  // same `.notif-host` column has its own action button, and starting the
  // exit animation on `pointerdown` used to steal that button's click before
  // its own `pointerup`/`click` ever fired. The target here isn't `cardRef`'s
  // own node at all — it's a sibling the card has no reference to — so the
  // only thing that can save it is the `.closest(".notif-host")` check.
  const bus = liveGlobalEvents();
  const cardNode = { contains: () => false };
  let renderer: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(<JobPopupCard job={JOB} onGone={() => {}} />, {
      createNodeMock: () => cardNode,
    });
  });

  const actionButton = { closest: (sel: string) => (sel === ".notif-host" ? {} : null) };
  await act(async () => {
    bus.fire("click", { target: actionButton, preventDefault() {}, stopPropagation() {} });
  });

  const json = renderer!.toJSON() as ReactTestRendererJSON;
  expect(json.props.className).not.toContain("leaving");
  bus.restore();
});

test("the outside-press listener is removed once the card starts leaving, and again on unmount", async () => {
  const bus = liveGlobalEvents();
  let renderer: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(<JobPopupCard job={JOB} onGone={() => {}} />);
  });
  expect(bus.removeCount("click")).toBe(0);

  await act(async () => {
    bus.fire("click", { target: {}, preventDefault() {}, stopPropagation() {} });
  });
  // Leaving now — the listener that got it there tears itself down rather
  // than sitting around watching a card that can no longer be dismissed.
  expect(bus.removeCount("click")).toBe(1);

  await act(async () => {
    renderer!.unmount();
  });
  // Already removed once leaving started — unmounting a card that never
  // started leaving (the visible-window timeout path) must still clean up,
  // so this asserts the count does not grow past what leaving already did,
  // never that unmount fires a second, redundant removal.
  expect(bus.removeCount("click")).toBe(1);
  bus.restore();
});

async function withActiveElement<T>(value: unknown, fn: () => Promise<T>): Promise<T> {
  // A single `try/finally` reset point for every test below that fakes
  // `document.activeElement` — without it, a failed assertion in the body
  // leaves the fake element in place for every later test in this file
  // (bun runs one shared module scope), turning one real failure into a
  // cascade of unrelated ones downstream. `fn` is always async and always
  // `await`ed here before the reset runs — a bare `try { return fn() }
  // finally` would run the reset synchronously, before the awaited body
  // inside `fn` ever executes.
  (document as unknown as { activeElement: unknown }).activeElement = value;
  try {
    return await fn();
  } finally {
    (document as unknown as { activeElement: unknown }).activeElement = null;
  }
}

function fakeIframe(): unknown {
  return new (globalThis as unknown as { HTMLIFrameElement: new () => unknown }).HTMLIFrameElement();
}

/** Poll `toJSON()` until `predicate` matches, or give up after `timeoutMs`.
 *
 * `setLeaving(true)` here runs inside a native (non-React) `blur` listener,
 * outside React's own event system, so the resulting re-render is scheduled
 * rather than applied inline — CI's ubuntu-latest runner has been observed to
 * need more than one flushed tick before it lands (a single extra
 * `act(async () => { await sleep(0); })` was tried and was NOT enough: CI
 * still read back the pre-update tree). A fixed number of ticks is a guess
 * about how slow a shared CI runner can get; polling with a real bound is
 * not — it resolves the instant the update lands, on any machine, and still
 * fails cleanly (never hangs) if the update genuinely never comes. This is
 * the only test in the file that needs it: it is the only one whose
 * assertion depends on a blur-driven update actually taking effect rather
 * than staying absent (see this file's other blur tests, which assert the
 * class does NOT appear — a race that drops an update looks identical to a
 * correct no-op there, so they were never the ones flaking).
 */
async function waitForClassName(
  renderer: ReturnType<typeof create>,
  predicate: (className: string) => boolean,
  timeoutMs = 2000,
): Promise<ReactTestRendererJSON> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const json = renderer!.toJSON() as ReactTestRendererJSON;
    if (predicate(json.props.className ?? "")) return json;
    if (Date.now() >= deadline) return json;
    await act(async () => {
      await sleep(10);
    });
  }
}

test("an iframe taking focus (a press inside an app page) starts the exit animation", async () => {
  // A press inside an app page's iframe never dispatches anything this
  // document can see — no `click` ever reaches the outside-press listener
  // above. It DOES blur whatever had focus here first, though, and hands
  // focus to the iframe itself, which is exactly what this test fakes: no
  // iframe focused yet when the card mounts, then a blur that hands focus
  // to one for the first time.

  // TEMPORARY DIAGNOSTICS — remove once CI's real cause is identified.
  const DIAG_HTMLIFrameElement = (
    globalThis as unknown as { HTMLIFrameElement: { name: string } }
  ).HTMLIFrameElement;
  console.log("DIAG globalThis.HTMLIFrameElement.name=", DIAG_HTMLIFrameElement?.name);
  console.log("DIAG globalThis.dispatchEvent is native=", globalThis.dispatchEvent.toString().includes("[native code]"));
  console.log(
    "DIAG document identity tag (pre-mount)=",
    (document as unknown as { __diagTag?: string }).__diagTag,
  );
  (document as unknown as { __diagTag?: string }).__diagTag ??= `doc-${Math.random().toString(36).slice(2)}`;
  console.log(
    "DIAG document.activeElement (pre-mount)=",
    document.activeElement,
    "ctor=",
    (document.activeElement as { constructor?: { name?: string } } | null)?.constructor?.name,
  );
  console.log(
    "DIAG Object.getOwnPropertyDescriptor(document,'activeElement') (pre-mount)=",
    JSON.stringify(
      Object.getOwnPropertyDescriptor(document, "activeElement"),
      (_k, v) => (typeof v === "function" ? "[function]" : v),
    ),
  );

  let diagBlurAdds = 0;
  let diagBlurRemoves = 0;
  const diagOrigAdd = globalThis.addEventListener.bind(globalThis);
  const diagOrigRemove = globalThis.removeEventListener.bind(globalThis);
  (globalThis as unknown as { addEventListener: typeof globalThis.addEventListener }).addEventListener = (
    type: string,
    fn: EventListenerOrEventListenerObject,
    opts?: boolean | AddEventListenerOptions,
  ) => {
    if (type === "blur") diagBlurAdds++;
    return diagOrigAdd(type, fn, opts);
  };
  (globalThis as unknown as { removeEventListener: typeof globalThis.removeEventListener }).removeEventListener = (
    type: string,
    fn: EventListenerOrEventListenerObject,
    opts?: boolean | EventListenerOptions,
  ) => {
    if (type === "blur") diagBlurRemoves++;
    return diagOrigRemove(type, fn, opts);
  };

  let renderer: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(<JobPopupCard job={JOB} onGone={() => {}} />);
  });

  (globalThis as unknown as { addEventListener: typeof globalThis.addEventListener }).addEventListener = diagOrigAdd;
  (globalThis as unknown as { removeEventListener: typeof globalThis.removeEventListener }).removeEventListener =
    diagOrigRemove;
  console.log("DIAG blur listeners added/removed during mount=", diagBlurAdds, diagBlurRemoves);
  console.log(
    "DIAG document.activeElement (post-mount, pre-withActiveElement)=",
    document.activeElement,
    "ctor=",
    (document.activeElement as { constructor?: { name?: string } } | null)?.constructor?.name,
    "instanceof HTMLIFrameElement=",
    document.activeElement instanceof DIAG_HTMLIFrameElement,
  );

  await withActiveElement(fakeIframe(), async () => {
    const fake = fakeIframe();
    console.log(
      "DIAG fakeIframe() instanceof globalThis.HTMLIFrameElement=",
      fake instanceof DIAG_HTMLIFrameElement,
      "fake ctor=",
      (fake as { constructor?: { name?: string } }).constructor?.name,
    );
    console.log(
      "DIAG document.activeElement (inside withActiveElement, before blur)=",
      document.activeElement,
      "ctor=",
      (document.activeElement as { constructor?: { name?: string } } | null)?.constructor?.name,
      "instanceof HTMLIFrameElement=",
      document.activeElement instanceof DIAG_HTMLIFrameElement,
      "document.__diagTag=",
      (document as unknown as { __diagTag?: string }).__diagTag,
    );

    await act(async () => {
      globalThis.dispatchEvent(new Event("blur"));
    });

    console.log(
      "DIAG document.activeElement (right after blur dispatch, before poll)=",
      document.activeElement,
      "ctor=",
      (document.activeElement as { constructor?: { name?: string } } | null)?.constructor?.name,
      "instanceof HTMLIFrameElement=",
      document.activeElement instanceof DIAG_HTMLIFrameElement,
    );
    const preJson = renderer!.toJSON() as ReactTestRendererJSON;
    console.log("DIAG className right after blur dispatch=", preJson.props.className);

    const json = await waitForClassName(renderer!, (c) => c.includes("leaving"));
    console.log("DIAG className after full poll window=", json.props.className);
    console.log(
      "DIAG document.activeElement (after full poll window)=",
      document.activeElement,
      "instanceof HTMLIFrameElement=",
      document.activeElement instanceof DIAG_HTMLIFrameElement,
    );
    expect(json.props.className).toContain("leaving");
  });

  await act(async () => {
    renderer!.unmount();
  });
});

test("a plain window blur with no iframe focused does not hide the card", async () => {
  // Alt-tabbing away, opening devtools, a native file picker: all of these
  // blur the window too, but none of them hand focus to an iframe — the
  // narrow condition is what keeps this card up through all of them.
  let renderer: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(<JobPopupCard job={JOB} onGone={() => {}} />);
  });

  await act(async () => {
    globalThis.dispatchEvent(new Event("blur"));
  });

  const json = renderer!.toJSON() as ReactTestRendererJSON;
  expect(json.props.className).not.toContain("leaving");

  await act(async () => {
    renderer!.unmount();
  });
});

test("a window blur while an iframe ALREADY held focus does not hide the card", async () => {
  // The bug this guards: `document.activeElement instanceof
  // HTMLIFrameElement` is true for as long as an iframe holds focus, not
  // only in the instant focus moves to it. A user who clicked into an app
  // page BEFORE this card popped up, then alt-tabs or opens devtools, fires
  // a plain window blur with `activeElement` still the iframe from
  // earlier — that must not read as "focus just moved to an iframe".
  await withActiveElement(fakeIframe(), async () => {
    let renderer: ReturnType<typeof create>;
    await act(async () => {
      renderer = create(<JobPopupCard job={JOB} onGone={() => {}} />);
    });

    await act(async () => {
      globalThis.dispatchEvent(new Event("blur"));
    });

    const json = renderer!.toJSON() as ReactTestRendererJSON;
    expect(json.props.className).not.toContain("leaving");

    await act(async () => {
      renderer!.unmount();
    });
  });
});

function findAll(node: ReactTestRendererJSON | null, className: string): ReactTestRendererJSON[] {
  if (node === null || typeof node === "string") return [];
  const hits: ReactTestRendererJSON[] = [];
  if (typeof node.props?.className === "string" && node.props.className.split(" ").includes(className)) {
    hits.push(node);
  }
  for (const child of node.children ?? []) {
    if (typeof child !== "string") hits.push(...findAll(child, className));
  }
  return hits;
}

test("the ✕ only closes the card — it never calls the real, server-side dismiss", async () => {
  let dismissCalls = 0;
  const dismissed: string[] = [];
  const unsubscribe = subscribeJobDismissed((id) => dismissed.push(id));
  const job: Job = { ...JOB, page: "/tmp/out.png", tier: "trail" };
  let renderer: ReturnType<typeof create>;
  try {
    await act(async () => {
      renderer = create(
        <JobPopupCard
          job={job}
          onGone={() => {}}
          dismissFn={async (id) => {
            dismissCalls++;
            return { dismissed: id };
          }}
        />,
      );
    });

    const before = renderer!.toJSON() as ReactTestRendererJSON;
    const x = findAll(before, "dl-x")[0];
    expect(x).toBeDefined();
    act(() => {
      (x.props as { onClick: () => void }).onClick();
    });

    // The card starts leaving on its own — no network dismiss behind it, and
    // no wait needed for one to settle.
    expect(dismissCalls).toBe(0);
    // Nor does the panel hear about a dismissal that never happened.
    expect(dismissed).toEqual([]);
    const after = renderer!.toJSON() as ReactTestRendererJSON;
    expect(after.props.className).toContain("leaving");
  } finally {
    unsubscribe();
  }
});
