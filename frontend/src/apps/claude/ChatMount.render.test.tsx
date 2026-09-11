// WHAT EACH BRANCH ACTUALLY RENDERS. `legacy-src.test.ts` pins the six URLs;
// this pins the element around them — which is the other half of "flag off is
// the legacy iframe exactly as today", and the half a string test cannot see: a
// lost `className`, a lost `frameRef`, legacy winning when the flag says
// native, or either branch rendering before the flag has answered at all.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { lazy, Suspense } from "react";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

const { ChatMount, ChatChunkBoundary, legacyBranch, useHostIds } = await import("./ChatMount");
const { createMemoryParamsStore } = await import("./params/store");
// The native branch is a `lazy` chunk, so an `act` that does not AWAIT pins the
// Suspense fallback and nothing else. Resolving the module once, here, makes
// every mount below able to reach the chat itself inside one async `act`.
await import("./ClaudeChat");
const { publishNativeChatEnabled, resetNativeChatFlagForTests } = await import("./feature-flag");

// NO PREFS GET FROM THIS FILE. Every test publishes the flag directly, but the
// "not read yet" one deliberately leaves `read()` armed — and a real GET that
// settles later now writes `false` (feature-flag.ts's catch), which lands as a
// state update outside `act`. A fetch that never settles keeps the tri-state
// exactly where each test puts it. Restored after each test, because
// `globalThis` is shared with every other file in the run.
const realFetch = globalThis.fetch;
beforeEach(() => {
  (globalThis as { fetch: unknown }).fetch = () => new Promise(() => {});
});

const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement) {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
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
  resetNativeChatFlagForTests();
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

const SRC = "/render?path=%2Fw%2Fp%2Ftemplate.html&_file=%2Fw%2Fp&chat_only=1";

test("the flag not yet read renders NEITHER branch — just the cover", () => {
  resetNativeChatFlagForTests();
  const r = mount(
    <ChatMount
      file="/w/p"
      chatOnly
      legacySrc={SRC}
      className="preview-side-frame"
      paramsSource="url"
    />,
  );
  // No iframe: booting the legacy template would drain the pending "Fix with
  // AI" ask and start a poll for a document about to be thrown away.
  expect(nodes(r).filter((n) => n.type === "iframe")).toEqual([]);
  expect(classes(r).some((c) => c.split(" ").includes("chat-frame"))).toBe(true);
  expect(classes(r).some((c) => c.includes("chat-frame-placeholder"))).toBe(true);
});

test("flag off is the legacy ChatFrame: same src, same class, same frameRef", () => {
  publishNativeChatEnabled(false);
  const frameRef = { current: null as HTMLIFrameElement | null };
  const r = mount(
    <ChatMount
      file="/w/p"
      chatOnly
      legacySrc={SRC}
      className="task-peek-frame"
      title="TASK-1 chat"
      legacyFrameRef={frameRef}
      paramsSource="memory"
    />,
  );
  const frames = nodes(r).filter((n) => n.type === "iframe");
  expect(frames.length).toBe(1);
  const props = frames[0].props as Record<string, unknown>;
  expect(props.src).toBe(SRC);
  expect(props.title).toBe("TASK-1 chat");
  // The frame's own class rides the IFRAME (the card's scaled fit depends on
  // it), beside ChatFrame's own.
  expect(String(props.className).split(" ")).toContain("task-peek-frame");
  // And nothing native: no `.chat-mount` box around it.
  expect(classes(r).some((c) => c.split(" ").includes("chat-mount"))).toBe(false);
});

test("flag off with a `legacy` node hands that node over verbatim", () => {
  publishNativeChatEnabled(false);
  const r = mount(
    <ChatMount
      file="/w/p"
      legacySrc={SRC}
      legacy={<iframe className="pane-frame is-shown" src={SRC} title="given" />}
      paramsSource="url"
    />,
  );
  const frames = nodes(r).filter((n) => n.type === "iframe");
  expect(frames.length).toBe(1);
  expect(String((frames[0].props as Record<string, unknown>).className)).toBe(
    "pane-frame is-shown",
  );
});

test("flag on renders the native box and NO iframe, with the mount class on it", async () => {
  publishNativeChatEnabled(true);
  // AWAITED: the chunk resolves inside this `act`, so what is asserted is the
  // chat itself and not the Suspense cover standing in for it.
  const r = await mountAsync(
    <ChatMount
      file="/w/p"
      chatOnly
      legacySrc={SRC}
      mountClassName="preview-frame is-shown"
      paramsSource="url"
    />,
  );
  expect(nodes(r).filter((n) => n.type === "iframe")).toEqual([]);
  // `mountClassName` rides the native box; `className` (frame geometry) does
  // NOT — stamping both would shrink a compact card twice.
  expect(classes(r)).toContain("chat-mount preview-frame is-shown");
  // The chat's own root is inside it — i.e. the chunk really landed, and this
  // is not the cover.
  expect(classes(r).some((c) => c.includes("chat-root"))).toBe(true);
  expect(classes(r).some((c) => c.includes("chat-frame-placeholder"))).toBe(false);
});

test("the chunk's cover never wears the LEGACY frame's geometry class", async () => {
  publishNativeChatEnabled(true);
  const r = await mountAsync(
    <ChatMount
      file="/w/p"
      compact
      legacySrc={SRC}
      className="task-card-frame"
      paramsSource="memory"
    />,
  );
  // No frame geometry anywhere in the native branch's output: `.task-card-frame`
  // lays out at 133.33% and draws at `scale(0.75)`, and `.chat-mount` is not a
  // frame.
  expect(classes(r).some((c) => c.split(" ").includes("task-card-frame"))).toBe(false);
  // The Suspense COVER is the one node this cannot reach — the chunk is already
  // resolved in a test (see the top-level `await import`), so the fallback never
  // paints and react-test-renderer puts no instance in the tree for it. Pinned
  // at the source instead, because the regression is a one-word one (handing it
  // `props.className` again) and it would be invisible: a compact card's cover
  // scaled twice, popping when the real chat lands.
  const src = await Bun.file(new URL("./ChatMount.tsx", import.meta.url)).text();
  expect(src).toMatch(/<Suspense fallback=\{placeholderFor\(\)\}>/);
});

test("a chunk that fails to load falls back to the legacy frame, not a blank shell", async () => {
  // The deploy case `__BUILD_VERSION__` exists for: a tab open across a deploy
  // asks for a hashed chunk that is gone. Without a boundary that throw unmounts
  // React to the root and the reader loses the whole shell.
  const Gone = lazy(() => Promise.reject(new Error("chunk 404")));
  const props = {
    file: "/w/p",
    chatOnly: true,
    legacySrc: SRC,
    className: "task-card-frame",
    title: "TASK-1 chat",
    paramsSource: "memory" as const,
  };
  const quiet = console.error;
  console.error = () => {};
  let r!: ReturnType<typeof create>;
  try {
    await act(async () => {
      r = create(
        <ChatChunkBoundary fallback={legacyBranch(props)}>
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
  const frames = nodes(r).filter((n) => n.type === "iframe");
  expect(frames.length).toBe(1);
  const framed = frames[0].props as Record<string, unknown>;
  expect(framed.src).toBe(SRC);
  // …and the SAME node the flag-off branch renders, geometry class included.
  expect(String(framed.className).split(" ")).toContain("task-card-frame");
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
