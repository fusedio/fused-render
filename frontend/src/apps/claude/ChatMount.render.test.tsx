// WHAT THE MOUNT ACTUALLY RENDERS. The chat itself has its own suites; this
// pins the box around it — the native mount and nothing else in it, the host
// classes that may and may not ride it, the ids a host hands over late, and the
// screen a `lazy` chunk that will not load falls back to.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { lazy, Suspense } from "react";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

const { ChatMount, ChatChunkBoundary, ChatLoadFailed, useHostIds } = await import("./ChatMount");
const { createMemoryParamsStore } = await import("./params/store");
// The chat is a `lazy` chunk, so an `act` that does not AWAIT pins the Suspense
// fallback and nothing else. Resolving the module once, here, makes every mount
// below able to reach the chat itself inside one async `act`.
const { ClaudeChat } = await import("./ClaudeChat");
const { resetChatPrefsForTests } = await import("./chat-prefs");

// NO PREFS GET FROM THIS FILE: a real one settling later lands as a state
// update outside `act`. A fetch that never settles leaves the defaults where
// they are. Restored after each test, because `globalThis` is shared with every
// other file in the run.
const realFetch = globalThis.fetch;
beforeEach(() => {
  (globalThis as { fetch: unknown }).fetch = () => new Promise(() => {});
});

const mounted: Array<ReturnType<typeof create>> = [];
/** A mount that lets the `lazy` chunk land — the chat's own output is behind
 *  one microtask turn, and a synchronous `act` only ever sees the cover. */
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
  resetChatPrefsForTests();
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
const text = (r: ReturnType<typeof create>) => JSON.stringify(r.toJSON());

test("the mount is the native box and NO iframe, with the mount class on it", async () => {
  // AWAITED: the chunk resolves inside this `act`, so what is asserted is the
  // chat itself and not the Suspense cover standing in for it.
  const r = await mountAsync(
    <ChatMount file="/w/p" chatOnly mountClassName="preview-frame is-shown" paramsSource="url" />,
  );
  expect(nodes(r).filter((n) => n.type === "iframe")).toEqual([]);
  expect(classes(r)).toContain("chat-mount preview-frame is-shown");
  // The chat's own root is inside it — i.e. the chunk really landed, and this
  // is not the cover.
  expect(classes(r).some((c) => c.includes("chat-root"))).toBe(true);
  expect(classes(r).some((c) => c.includes("chat-frame-placeholder"))).toBe(false);
});

test("the chunk's cover never wears a host's FRAME geometry class", async () => {
  const r = await mountAsync(<ChatMount file="/w/p" compact paramsSource="memory" />);
  // No frame geometry anywhere in the output: `.task-card-frame` lays out at
  // 133.33% and draws at `scale(0.75)`, and `.chat-mount` is not a frame.
  expect(classes(r).some((c) => c.split(" ").includes("task-card-frame"))).toBe(false);
  // The Suspense COVER is the one node this cannot reach — the chunk is already
  // resolved in a test (see the top-level `await import`), so the fallback never
  // paints and react-test-renderer puts no instance in the tree for it. Pinned
  // at the source instead, because the regression is a one-word one (handing it
  // a host class again) and it would be invisible: a compact card's cover
  // scaled twice, popping when the real chat lands.
  const src = await Bun.file(new URL("./ChatMount.tsx", import.meta.url)).text();
  expect(src).toMatch(/<Suspense fallback=\{placeholderFor\(\)\}>/);
});

test("a chunk that fails to load leaves an error card with a way out, not a blank shell", async () => {
  // The deploy case `__BUILD_VERSION__` exists for: a tab open across a deploy
  // asks for a hashed chunk that is gone. Without a boundary that throw unmounts
  // React to the root and the reader loses the whole shell — and with nothing to
  // degrade to, what is owed instead is the fact and the one press that fixes it.
  const Gone = lazy(() => Promise.reject(new Error("chunk 404")));
  const quiet = console.error;
  console.error = () => {};
  let r!: ReturnType<typeof create>;
  try {
    await act(async () => {
      r = create(
        <ChatChunkBoundary fallback={<ChatLoadFailed />}>
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
  expect(nodes(r).filter((n) => n.type === "iframe")).toEqual([]);
  // The app's own error card, not a look of its own.
  expect(classes(r).some((c) => c.split(" ").includes("trouble-card"))).toBe(true);
  expect(text(r)).toContain("This chat could not load.");
  expect(text(r)).toContain("The app was updated. Reload to continue.");
  // And the ACTION: a button, not a sentence telling the reader to go and do it.
  const buttons = nodes(r).filter((n) => n.type === "button");
  expect(buttons.length).toBe(1);
  expect(JSON.stringify(buttons[0].children)).toContain("Reload");
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
  const off = await mountAsync(<ChatMount file="/w/p" chatOnly paramsSource="url" />);
  const propsOf = (r: ReturnType<typeof create>) =>
    r.root.findByType(ClaudeChat).props as Record<string, unknown>;
  expect("recap" in propsOf(off)).toBe(false);

  const on = await mountAsync(<ChatMount file="/w/p" chatOnly paramsSource="url" recap />);
  expect(propsOf(on).recap).toBe(true);
});
