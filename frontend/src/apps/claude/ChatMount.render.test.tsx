// WHAT THE MOUNT ACTUALLY RENDERS. The chat itself has its own suites; this
// pins the box around it — the native mount and nothing else in it, the host
// classes that may and may not ride it, the ids a host hands over late, and the
// two screens a failure inside it falls back to.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { lazy, Suspense } from "react";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

const { ChatMount, ChatChunkBoundary, ChatLoadFailed, isChunkLoadError, useHostIds } =
  await import("./ChatMount");
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
  // is not the Suspense cover. NOT asserted: that no `.chat-frame-placeholder`
  // remains — the loaded chat draws its OWN boot cover (same class) while its
  // first history read is in flight, and whether that read has settled by now
  // depends on what other test files warmed, which is not this test's claim.
  expect(classes(r).some((c) => c.includes("chat-root"))).toBe(true);
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

/** The boundary as the mount wires it, with the card as its fallback — so what
 *  these cases drive is the real pairing and not a hand-built screen. */
function boundary(children: React.ReactNode, onReady?: () => void) {
  return (
    <ChatChunkBoundary
      fallback={(failure) => (
        <ChatLoadFailed
          kind={failure.kind}
          error={failure.error}
          onRetry={failure.reset}
          {...(onReady ? { onReady } : {})}
        />
      )}
    >
      {children}
    </ChatChunkBoundary>
  );
}

/** Mount with `console.error` muted: the boundary logs every catch in full, on
 *  purpose, and a suite that printed it would be unreadable. */
async function mountQuiet(el: React.ReactElement) {
  const quiet = console.error;
  const logged: unknown[][] = [];
  console.error = (...args: unknown[]) => void logged.push(args);
  let r!: ReturnType<typeof create>;
  try {
    await act(async () => {
      r = create(el);
    });
  } finally {
    console.error = quiet;
  }
  mounted.push(r);
  return { r, logged };
}

/** The boundary's OWN log line, apart from React's own "The above error
 *  occurred in" that every caught throw also prints. */
const ours = (logged: unknown[][]) =>
  logged.filter((args) => args[0] === "chat failed to render");

const buttonLabels = (r: ReturnType<typeof create>) =>
  nodes(r)
    .filter((n) => n.type === "button")
    .map((n) => JSON.stringify(n.children));

test("a chunk that fails to load leaves an error card with a way out, not a blank shell", async () => {
  // The deploy case `__BUILD_VERSION__` exists for: a tab open across a deploy
  // asks for a hashed chunk that is gone. Without a boundary that throw unmounts
  // React to the root and the reader loses the whole shell — and with nothing to
  // degrade to, what is owed instead is the fact and the one press that fixes it.
  //
  // The message is a REAL one (Chromium's), not "chunk 404": the two screens are
  // told apart by matching it, so a test that invents its own wording would pass
  // while every real deploy failure took the crash branch.
  const Gone = lazy(() =>
    Promise.reject(
      new TypeError(
        "Failed to fetch dynamically imported module: http://localhost/assets/ClaudeChat-a1b2c3d4.js",
      ),
    ),
  );
  let ready = 0;
  const { r, logged } = await mountQuiet(
    boundary(
      <Suspense fallback={<div className="chat-frame-placeholder" />}>
        <Gone />
      </Suspense>,
      () => ready++,
    ),
  );
  expect(nodes(r).filter((n) => n.type === "iframe")).toEqual([]);
  // The app's own error card, not a look of its own.
  expect(classes(r).some((c) => c.split(" ").includes("trouble-card"))).toBe(true);
  expect(text(r)).toContain("This chat could not load.");
  expect(text(r)).toContain("The app was updated. Reload to continue.");
  // NOT the crash screen's copy, and no verbatim line: the cause is known
  // exactly, and the URL of a chunk means nothing to a reader.
  expect(text(r)).not.toContain("This chat hit an error.");
  expect(classes(r).some((c) => c.split(" ").includes("trouble-error"))).toBe(false);
  // And the ACTION: a button, not a sentence telling the reader to go and do it.
  // ONE of them — `lazy` caches its rejected promise, so a "Try again" here
  // would re-throw the same rejection and be a button that does nothing.
  expect(buttonLabels(r)).toEqual([JSON.stringify(["Reload"])]);
  // And it completes the host's swap (Bugbot on #1149): a content pane that
  // holds the previous frame until `onReady` would otherwise keep this card
  // at opacity 0 — Reload hidden — until its own timeout gave up.
  expect(ready).toBe(1);
  // The whole thing reaches a console ONCE, whatever the card chose to show
  // (React logs its own "The above error occurred in" beside it; that is React's).
  expect(ours(logged).length).toBe(1);
});

test("a chat that THROWS gets the crash screen, with what happened and a retry", async () => {
  // Bugbot on #1149: this boundary is above the whole chat, so every render
  // throw inside it lands here too — and one screen said "The app was updated.
  // Reload to continue." to all of them. A reader who reloads on that advice
  // gets the same crash from the same build.
  function Boom(): React.ReactElement {
    throw new Error("Cannot read properties of undefined (reading 'turns')");
  }
  let ready = 0;
  const { r, logged } = await mountQuiet(boundary(<Boom />, () => ready++));
  expect(text(r)).toContain("This chat hit an error.");
  expect(text(r)).not.toContain("The app was updated. Reload to continue.");
  // The error VERBATIM, in the app's own `.trouble-error` pre — the reader is
  // owed what actually happened rather than a shrug.
  expect(classes(r).some((c) => c.split(" ").includes("trouble-error"))).toBe(true);
  expect(text(r)).toContain("Cannot read properties of undefined (reading 'turns')");
  // TWO actions, retry first: a render throw is usually about one conversation's
  // state, and the cheap press should be the one in front.
  expect(buttonLabels(r)).toEqual([JSON.stringify(["Try again"]), JSON.stringify(["Reload"])]);
  expect(ready).toBe(1);
  expect(ours(logged).length).toBe(1);
});

test("a very long throw is trimmed to one line so the actions stay in the box", async () => {
  // The card sits in a box that can be a 300px task card; a folded stack trace
  // would push Try again and Reload below the fold. The full object went to the
  // console — that is where a stack belongs.
  const long = "wide\n   ".repeat(200);
  function Boom(): React.ReactElement {
    throw new Error(long);
  }
  const { r } = await mountQuiet(boundary(<Boom />));
  const pre = nodes(r).find((n) =>
    String((n.props as Record<string, unknown>)?.className ?? "")
      .split(" ")
      .includes("trouble-error"),
  );
  const shown = String((pre?.children ?? [])[0]);
  expect(shown.length).toBeLessThanOrEqual(200);
  expect(shown).not.toContain("\n");
  expect(shown.endsWith("\u2026")).toBe(true);
});

test("Try again REMOUNTS the tree — a throw that was about one render is over", async () => {
  // The claim is a remount and not a re-render: the boundary keys its children
  // on the attempt, so the subtree that threw is thrown away with its state.
  // A child that throws only the FIRST time is exactly that shape.
  let renders = 0;
  function OnceBad() {
    renders += 1;
    if (renders === 1) throw new Error("the first paint only");
    return <div className="chat-root" />;
  }
  const { r } = await mountQuiet(boundary(<OnceBad />));
  expect(text(r)).toContain("This chat hit an error.");
  const retry = nodes(r).find(
    (n) => n.type === "button" && JSON.stringify(n.children).includes("Try again"),
  );
  await act(async () => {
    (retry?.props as { onClick: () => void }).onClick();
  });
  // The chat is back, and the card is gone with it.
  expect(classes(r).some((c) => c.split(" ").includes("chat-root"))).toBe(true);
  expect(text(r)).not.toContain("This chat hit an error.");
});

test("the two kinds are told apart by what a failed import ACTUALLY says", async () => {
  // Every engine's own wording for a dynamic import that did not arrive, plus
  // Vite's own for a stylesheet dep (vite 6.4.3's preload helper). These are
  // the strings the classification rests on; inventing one would make the chunk
  // screen unreachable in the browser the message came from.
  for (const message of [
    "Failed to fetch dynamically imported module: http://x/assets/a.js", // Chromium
    "error loading dynamically imported module: http://x/assets/a.js", // Firefox
    "Importing a module script failed.", // WebKit
    "Unable to preload CSS for /assets/chat-9f8e.css", // Vite itself
    "Loading chunk 42 failed.", // webpack's, matched for breadth
    "Loading CSS chunk 7 failed.",
  ]) {
    expect(isChunkLoadError(new TypeError(message))).toBe(true);
  }
  // …and everything else is a CRASH, because the crash screen's advice is safe
  // for a chunk failure while the deploy screen's is a lie about a chat that
  // threw. A value that is not an Error at all included.
  for (const other of [
    new Error("Cannot read properties of undefined (reading 'turns')"),
    new Error(""),
    "a thrown string",
    null,
    undefined,
    { message: "Failed to fetch dynamically imported module: /a.js" },
  ]) {
    expect(isChunkLoadError(other)).toBe(false);
  }
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
