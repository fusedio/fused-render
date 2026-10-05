// WHAT THE MOUNT ACTUALLY RENDERS: the chat in its own box (never an iframe),
// the host's class on that box, the chunk's failure as an error card, and the
// per-mount params store's seed/sync rules.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { lazy, Suspense } from "react";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

const { ChatMount, ChatChunkBoundary, useHostIds } = await import("./ChatMount");
const { createMemoryParamsStore } = await import("./params/store");
// The native branch is a `lazy` chunk, so an `act` that does not AWAIT pins the
// Suspense fallback and nothing else. Resolving the module once, here, makes
// every mount below able to reach the chat itself inside one async `act`.
const { ClaudeChat } = await import("./ClaudeChat");
const { resetFlagsForTests } = await import("./feature-flag");

// NO SERVER FROM THIS FILE. A mounted chat starts reads (prefs, history,
// tasks), and one that settled later would land as a state update outside
// `act`. A fetch that never settles keeps every mount where the test puts it.
// Restored after each test, because `globalThis` is shared with every other
// file in the run.
const realFetch = globalThis.fetch;
beforeEach(() => {
  (globalThis as { fetch: unknown }).fetch = () => new Promise(() => {});
});

const mounted: Array<ReturnType<typeof create>> = [];
/** A mount that lets the `lazy` chunk land — the native branch's own output is
 *  behind one microtask turn, and a synchronous `act` only ever sees the cover. */
async function mountAsync(el: React.ReactElement) {
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(el);
  });
  await act(async () => {
    await new Promise((done) => setTimeout(done, 0));
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetFlagsForTests();
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

type Json = ReactTestRendererJSON;
function nodes(r: ReturnType<typeof create>): Json[] {
  const out: Json[] = [];
  const walk = (n: Json | string | null) => {
    if (!n || typeof n === "string") return;
    out.push(n);
    for (const k of n.children ?? []) walk(k as Json);
  };
  walk(r.toJSON() as Json);
  return out;
}
const classes = (r: ReturnType<typeof create>) =>
  nodes(r).map((n) => String((n.props as Record<string, unknown>)?.className ?? ""));

test("the chat renders in its own box and NO iframe, with the mount class on it", async () => {
  // AWAITED: the chunk resolves inside this `act`, so what is asserted is the
  // chat itself and not the Suspense cover standing in for it.
  const r = await mountAsync(
    <ChatMount
      file="/w/p"
      chatOnly
      mountClassName="preview-frame is-shown"
      paramsSource="url"
    />,
  );
  expect(nodes(r).filter((n) => n.type === "iframe")).toEqual([]);
  // `mountClassName` rides the box.
  expect(classes(r)).toContain("chat-mount preview-frame is-shown");
  // The chat's own root is inside it — i.e. the chunk really landed, and this
  // is not the cover.
  expect(classes(r).some((c) => c.includes("chat-root"))).toBe(true);
  expect(classes(r).some((c) => c.includes("chat-frame-placeholder"))).toBe(false);
});

test("the chunk's cover is the plain skeleton, with no host class", async () => {
  // The Suspense COVER is the one node a test cannot reach — the chunk is
  // already resolved here (see the top-level `await import`), so the fallback
  // never paints. Pinned at the source instead: a host class on it would be a
  // geometry the box does not have (a compact card's cover scaled twice).
  const src = await Bun.file(new URL("./ChatMount.tsx", import.meta.url)).text();
  expect(src).toMatch(/<Suspense fallback=\{<ChatFramePlaceholder \/>\}>/);
});

test("a chunk that fails to load is an error card with a reload, not a blank shell", async () => {
  // The deploy case `__BUILD_VERSION__` exists for: a tab open across a deploy
  // asks for a hashed chunk that is gone. Without a boundary that throw unmounts
  // React to the root and the reader loses the whole shell.
  const Gone = lazy(() => Promise.reject(new Error("chunk 404")));
  const quiet = console.error;
  console.error = () => {};
  let r!: ReturnType<typeof create>;
  try {
    await act(async () => {
      r = create(
        <ChatChunkBoundary>
          <Suspense fallback={<div className="chat-frame-placeholder" />}>
            <Gone />
          </Suspense>
        </ChatChunkBoundary>,
      );
    });
  } finally {
    console.error = quiet;
  }
  mounted.push(r);
  // No frame of any kind, and no skeleton left standing for ever.
  expect(nodes(r).filter((n) => n.type === "iframe")).toEqual([]);
  expect(classes(r).some((c) => c.includes("chat-frame-placeholder"))).toBe(false);
  expect(classes(r)).toContain("chat-mount-failed");
  // It says what happened, verbatim, and offers the one fix.
  const text = JSON.stringify(r.toJSON());
  expect(text).toContain("chunk 404");
  expect(text).toContain("Reload the page");
});

test("a host id that arrives later pushes only its own key", async () => {
  // The bug this is about is EFFECT DEPS: one effect over both ids re-ran its
  // whole body for a session change and re-wrote `run` with it — reviving a run
  // the controller had already ended and cleared, which `resumeRun` then polls
  // for as a dead id.
  const store = createMemoryParamsStore({ session_id: "s1", run: "r1" });
  const wrote: Array<Record<string, string | null>> = [];
  const spy = {
    ...store,
    get: (k: string) => store.get(k),
    getAll: () => store.getAll(),
    onChange: (cb: (all: Record<string, string>) => void) => store.onChange(cb),
    set: (patch: Record<string, string | null>) => {
      wrote.push(patch);
      store.set(patch);
    },
  };
  function Probe({ sessionId, runId }: { sessionId?: string; runId?: string }) {
    useHostIds(spy, sessionId, runId);
    return null;
  }
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Probe sessionId="s1" runId="r1" />);
  });
  mounted.push(r);
  expect(wrote).toEqual([]); // the seed already holds both

  // A fresh session id, the SAME run: only the session moves.
  act(() => r.update(<Probe sessionId="s2" runId="r1" />));
  expect(wrote).toEqual([{ session_id: "s2" }]);
  expect(store.get("run")).toBe("r1");

  // And a run the controller has cleared is NOT resurrected by the next
  // session-id refresh the listing hands over.
  store.set({ run: null });
  act(() => r.update(<Probe sessionId="s3" runId="r1" />));
  expect(wrote[wrote.length - 1]).toEqual({ session_id: "s3" });
  expect(store.get("run")).toBe(undefined);

  // A run id of its own still lands.
  act(() => r.update(<Probe sessionId="s3" runId="r2" />));
  expect(wrote[wrote.length - 1]).toEqual({ run: "r2" });
});

test("the recap is OPT-IN: absent unless the host asked for it", async () => {
  // The bug this pins: "While you were away" is a ~12s model call fired by
  // window `focus`, which EVERY mounted chat hears. A tasks wall spent seven of
  // them on one return. Default-off is the guarantee — a new embed site cannot
  // inherit the cost by not thinking about it — so what is asserted is the
  // absence of the prop, not merely a falsy one.
  const off = await mountAsync(
    <ChatMount file="/w/p" chatOnly paramsSource="url" />,
  );
  const propsOf = (r: ReturnType<typeof create>) =>
    r.root.findByType(ClaudeChat).props as Record<string, unknown>;
  expect("recap" in propsOf(off)).toBe(false);

  const on = await mountAsync(
    <ChatMount file="/w/p" chatOnly paramsSource="url" recap />,
  );
  expect(propsOf(on).recap).toBe(true);
});

test("a host's run settings SEED the pills and are never re-written under them",
  async () => {
    // THE SIDE PEEK'S BUG (Akshil, 2026-09-18: "I saw the sidebar peek — the
    // values there were different"). The composer ranks `param > detected >
    // pref > constant` (ui/composer-defaults), and `detected` is
    // `agent._defaults`: for a chat with no session id, the GLOBAL Claude
    // preference. Right for a chat somebody opened by hand; wrong for a TASK
    // that was set up with a model of its own, which is what the peek is always
    // showing. With nothing on the param the task's own choice could not win,
    // because it was never in the running.
    //
    // So a host that KNOWS the settings states them, and the existing top of
    // that ranking does the rest.
      const r = await mountAsync(
      <ChatMount
        file="/w/p"
        chatOnly
        peek
       
        paramsSource="memory"
        sessionId="s1"
        model="opus"
        effort="max"
      />,
    );
    const params = (r.root.findByType(ClaudeChat).props as {
      params: { getAll(): Record<string, string>; set(p: Record<string, string | null>): void };
    }).params;
    expect(params.getAll()).toMatchObject({ model: "opus", effort: "max" });

    // AND THEY ARE A SEED, NOT A SYNC — the one way these two differ from
    // `session_id` / `run` / `msg`, which a host may legitimately re-hand.
    //
    // The reader can change the pill, and the pill writes the same param. The
    // hazard is folding these two in beside the ids in `useHostIds`, where the
    // session-id effect re-runs on EVERY listing refresh (the tasks page
    // re-reads every 20-30s and hands a fresh id routinely — the very bug that
    // hook's header records for `run`). A pick made at 0s would be overwritten
    // at 20s by a value the reader had deliberately moved off.
    //
    // So: the reader picks, and then the host re-renders with a NEW SESSION ID,
    // which is the refresh that would trigger it.
    act(() => params.set({ model: "haiku", effort: "low" }));
    act(() => r.update(
      <ChatMount
        file="/w/p"
        chatOnly
        peek
       
        paramsSource="memory"
        sessionId="s2"
        model="opus"
        effort="max"
      />,
    ));
    expect(params.getAll()).toMatchObject({
      model: "haiku", effort: "low", session_id: "s2",
    });
  });

test("a host with no run settings leaves the chat's own detection speaking",
  async () => {
    // "" and absent both mean "this host has no opinion", which is every chat
    // that is not a task. Asserted as the ABSENCE of the key, not a falsy one:
    // an empty `model` param is a value, and `resolveModel`'s `param || detected`
    // would still short-circuit differently from no param at all if it ever
    // stopped being a falsy-or.
      const r = await mountAsync(
      <ChatMount file="/w/p" chatOnly peek paramsSource="memory" />,
    );
    const params = (r.root.findByType(ClaudeChat).props as {
      params: { getAll(): Record<string, string> };
    }).params;
    expect("model" in params.getAll()).toBe(false);
    expect("effort" in params.getAll()).toBe(false);

    const blank = await mountAsync(
      <ChatMount file="/w/p" chatOnly peek paramsSource="memory"
                 model="" effort="" />,
    );
    const blankParams = (blank.root.findByType(ClaudeChat).props as {
      params: { getAll(): Record<string, string> };
    }).params;
    expect("model" in blankParams.getAll()).toBe(false);
  });
