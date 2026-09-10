// THE BOOT'S LIFETIME, and the one answer it puts on the wire about the pane.
//
// Everything here is asserted through `/api/run`, because that is where the
// damage of getting it wrong actually lands: a run SPAWNED for a chat nobody is
// looking at any more, a second "Fix with AI" run on the same prompt, or a
// `has_pane: 1` telling the model it can see an app that was never framed.
// The mount is the real component over a stubbed `fetch` — the boot branch is a
// walk over three awaits, and only a real mount/unmount can cross it.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

const { ClaudeChat } = await import("./ClaudeChat");
const { createMemoryParamsStore } = await import("./params/store");
const { resetAgentDirCacheForTests, resolveAgentDir } = await import("./protocol/agent");

/** One `/api/run` call: the script and the action, plus the fields. */
interface RunCall {
  py: string;
  action: string;
  params: Record<string, string>;
}

const runs: RunCall[] = [];
/** Held back so a test can park the boot inside its detection wait. */
let holdPrefs = false;
/** Held back so a test can send while the PANE is still unresolved: the first
 *  stat is `resolveAgentDir`'s (the chat cannot mount without it), the second is
 *  `usePaneState`'s own. */
let holdPaneStat = false;
let stats = 0;

const realFetch = globalThis.fetch;

function jsonRes(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response;
}

/** The three endpoints a booting chat touches. `agent.py` answers the minimum
 *  that lets the run loop finish in one lap, so no test has to wait on a poll. */
function stubFetch(): void {
  (globalThis as { fetch: unknown }).fetch = async (
    input: unknown,
    init?: { body?: unknown },
  ): Promise<Response> => {
    const url = String(typeof input === "string" ? input : (input as { url: string }).url);
    if (url.startsWith("/api/fs/stat")) {
      stats++;
      if (holdPaneStat && stats > 1) return new Promise<Response>(() => {});
      return jsonRes({
        path: "/w/p",
        is_dir: true,
        templates: [{ mode: "claude", path: "/w/p/.claude/template.html" }],
      });
    }
    // THE LANDING'S LONG POLL, and it has to be a LONG poll here. `sessions.ts`
    // re-arms `/api/tasks/changes` the moment one returns, so a stub that
    // answers it immediately is an infinite re-arm inside `act` — which flushes
    // until the queue is empty and therefore never returns at all. A promise
    // that never settles is what the real endpoint does (it holds the request
    // open until something changes), so the landing view can be mounted.
    if (url.startsWith("/api/tasks/changes")) return new Promise<Response>(() => {});
    if (url === "/api/prefs") {
      if (holdPrefs) return new Promise<Response>(() => {});
      return jsonRes({});
    }
    if (url === "/api/run") {
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        py: string;
        params: Record<string, string>;
      };
      const action = String(body.params?.action ?? "");
      runs.push({ py: body.py, action, params: body.params ?? {} });
      if (body.py.endsWith("/app.py")) return jsonRes({ ok: true, result: {} });
      if (action === "defaults") return jsonRes({ ok: true, result: {} });
      if (action === "live_host") return jsonRes({ ok: true, result: { run_id: "" } });
      if (action === "start") return jsonRes({ ok: true, result: { run_id: "r1" } });
      if (action === "poll") {
        return jsonRes({ ok: true, result: { done: true, session_id: "s1", text: "ok" } });
      }
      return jsonRes({ ok: true, result: {} });
    }
    return jsonRes({});
  };
}

beforeEach(() => {
  runs.length = 0;
  holdPrefs = false;
  holdPaneStat = false;
  stats = 0;
  resetAgentDirCacheForTests();
  stubFetch();
});

const mounted: Array<ReturnType<typeof create>> = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

const baseProps = {
  file: "/w/p",
  chatOnly: true,
  compact: false,
  peek: false,
  autoFocus: false,
} as const;

/** Mounts and lets the microtask queue drain a few times, which is all the boot
 *  needs once nothing is held back. */
async function mountChat(extra: Record<string, unknown> = {}) {
  const params = createMemoryParamsStore();
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(<ClaudeChat {...baseProps} params={params} {...extra} />);
  });
  mounted.push(r);
  await settle();
  return { r, params };
}

async function settle(ms = 0): Promise<void> {
  await act(async () => {
    await new Promise((done) => setTimeout(done, ms));
  });
}

const started = () => runs.filter((c) => c.action === "start");

/**
 * A HOST'S OWN CONTENT FRAME, stubbed to exactly what the app-state walk reads
 * (see `pane/appState.test.ts` for the same shape and the same admission that
 * this is not a browser). It is what `Preview.tsx` hands the sidebar in the
 * `?_side=claude` layout: a same-origin `/render` document that IS the app.
 */
function hostFrameStub(): () => HTMLIFrameElement | null {
  const body = {
    tagName: "BODY",
    children: [],
    childNodes: [{ nodeType: 3, nodeValue: "the sine app" }],
    textContent: "the sine app",
    hasAttribute: () => false,
  };
  const win = {
    document: { title: "Sine", body },
    location: {
      href: "http://localhost/render?path=%2Fw%2Fp%2Fsine.html&freq=0.3",
      pathname: "/render",
      search: "?path=%2Fw%2Fp%2Fsine.html&freq=0.3",
    },
    console: { error() {}, warn() {} },
    addEventListener() {},
    removeEventListener() {},
  };
  // PR3's annotation target `watch`es the frame it is handed (`load` listener,
  // T:6127), so the stub has to carry a listener surface too.
  const frame = {
    isConnected: true,
    contentWindow: win,
    addEventListener() {},
    removeEventListener() {},
  } as unknown as HTMLIFrameElement;
  return () => frame;
}

test("the HOSTED layout's pane is the host's frame: has_pane 1 and a pushed block (R3-5)", async () => {
  // `chat_only` takes the chat's OWN column away; it does not take the app off
  // the screen. In the `?_side=claude` split the app is the middle column, and
  // the host hands that frame over as `annotateTarget` — which is what T did
  // through `parent.document` (`annFrame` is `annMarkedFrame()` in CHAT_ONLY).
  //
  // Getting this wrong cost three separate things at once, which is why they are
  // asserted together: no `<live-app-state>` block (so the model could not see
  // the app and no receipt was drawn), `has_pane: "0"` (so agent.py wrote an
  // `mcp.json` with no app-state channel and the CLI reported
  // `mcp__fused_approvals__app_state` as not connected), and — because
  // `_pane_file` reads the pane off the LEADING block — a session recorded as a
  // FOLDER chat, which then never appeared in the file's Recent list (R3-1/R3-3).
  await mountChat({ initialAsk: "what can you see?", annotateTarget: hostFrameStub() });
  await settle(20);
  expect(started().length).toBe(1);
  expect(started()[0].params.has_pane).toBe("1");
  const message = started()[0].params.message;
  expect(message).toContain("<live-app-state>");
  // The app's own facts, not this chat's: the title and the url the pane is on,
  // which is the pane the reader is describing when they type.
  expect(message).toContain('"title":"Sine"');
  expect(message).toContain("freq=0.3");
  // …and the user's words are still the user's words.
  expect(message).toContain("what can you see?");
});

test("a hosted mount whose host has marked NOTHING sends has_pane EMPTY (R3-5)", async () => {
  // The sidebar's copy of R2-10's race: the mark rides the frame the host is
  // SHOWING, so it lands when that frame paints. A `"0"` guessed before then
  // would cost the whole session its `app_state` tool with no way back, so the
  // answer is "you decide" and agent.py resolves it off the filesystem.
  await mountChat({ initialAsk: "fix the chart", annotateTarget: () => null });
  await settle(20);
  expect(started().length).toBe(1);
  expect(started()[0].params.has_pane).toBe("");
  // Nothing to describe, so nothing is claimed about it.
  expect(started()[0].params.message).not.toContain("<live-app-state>");
});

test("a host frame we cannot READ is not a pane (Bugbot #1061)", async () => {
  // The host's mark says "this frame is the content the reader is looking at",
  // not "its document is yours to read": the canvases workbench frames a
  // cross-origin document. Counting it made the first send claim `has_pane: 1`,
  // which puts `app_state` on the session's `--allowed-tools` FOR THE WHOLE
  // SESSION with no way back, while `blockForSend` could only ever answer "" —
  // the model told it could see an app it cannot. Unreadable answers the same as
  // absent: empty, and agent.py decides off the filesystem.
  const crossOrigin = () =>
    ({
      isConnected: true,
      // PR3's annotation target `watch`es whatever frame it is handed, so the
      // stub carries a listener surface like the readable one above — the point
      // of this case is the UNREADABLE document, not a missing element API.
      addEventListener() {},
      removeEventListener() {},
      get contentWindow(): never {
        throw new Error("Blocked a frame with origin … from accessing a cross-origin frame.");
      },
      get contentDocument(): never {
        throw new Error("Blocked a frame with origin … from accessing a cross-origin frame.");
      },
    }) as unknown as HTMLIFrameElement;
  await mountChat({ initialAsk: "what can you see?", annotateTarget: crossOrigin });
  await settle(20);
  expect(started().length).toBe(1);
  expect(started()[0].params.has_pane).toBe("");
  expect(started()[0].params.message).not.toContain("<live-app-state>");
});

test("a chat-only mount's first send says has_pane: 0", async () => {
  // CHAT_ONLY has no pane of ours whatever the target turns out to be
  // (`decidePane` answers `kind: "none"` for every kind there), so the status is
  // "none" from the first render and never spends a round-trip in "resolving".
  // A host that offers no frame either — a cards tile, a peek — is the case this
  // pins: there really is nothing to see, and saying so is what keeps the
  // `app_state` tool out of a roster that could only time out.
  // Pinned with the pane's own stat held: the ask leaves with nothing answered.
  holdPaneStat = true;
  await mountChat({ initialAsk: "fix the chart" });
  await settle(20);
  expect(started().length).toBe(1);
  expect(started()[0].params.has_pane).toBe("0");
  expect(started()[0].params.message).toContain("fix the chart");
});

test("an UNRESOLVED pane sends has_pane EMPTY and lets agent.py decide", async () => {
  // The split layout, whose target may well end up with a pane — but has not
  // yet. Neither answer is honest here, and the dishonest one is expensive:
  // `has_pane` is what agent.py builds the session's MCP roster off, ONCE, at
  // spawn, and nothing can repair it afterwards — so a `"0"` guessed while the
  // pane's stat was in flight took `mcp__fused_approvals__app_state` away for
  // the whole session and the CLI reported the tool as unreachable (R2-10).
  // The empty string is agent.py's own "you decide": `main` reads it as no
  // opinion and answers with `_has_pane(file)`, off the filesystem, unraced.
  // `"1"` is still never guessed — the pane may genuinely not exist.
  holdPaneStat = true;
  await mountChat({ chatOnly: false, initialAsk: "fix the chart" });
  await settle(20);
  expect(started().length).toBe(1);
  expect(started()[0].params.has_pane).toBe("");
});

test("an unmount mid-boot sends NOTHING: no run is spawned for a dead mount", async () => {
  // Parked inside the 1.5 s detection wait — the exact window "Fix with AI"
  // spends before its automatic send.
  holdPrefs = true;
  const { r } = await mountChat({ initialAsk: "fix the chart" });
  expect(started()).toEqual([]);
  act(() => r.unmount());
  mounted.length = 0;
  // Past the wait's own bound: without the cancel the ask sends here, on a
  // controller that has been disposed.
  await settle(1700);
  expect(started()).toEqual([]);
  expect(runs.some((c) => c.action === "live_host")).toBe(false);
});

test("the ask is spent ONCE — a host's own re-render cannot send it twice", async () => {
  // `markReady` used to be keyed on the `onReady` prop, so a host passing an
  // inline arrow re-ran the boot effect on every render of its own. With the
  // boot now cancelled on cleanup that would restart the detection wait (and,
  // without the spend latch, send the ask again).
  const params = createMemoryParamsStore();
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(
      <ClaudeChat {...baseProps} params={params} initialAsk="fix it" onReady={() => {}} />,
    );
  });
  mounted.push(r);
  await settle(20);
  expect(started().length).toBe(1);
  for (let i = 0; i < 3; i++) {
    await act(async () => {
      r.update(
        <ClaudeChat {...baseProps} params={params} initialAsk="fix it" onReady={() => {}} />,
      );
    });
    await settle(5);
  }
  await settle(1700);
  expect(started().length).toBe(1);
});

// ── THE ANNOTATION STRIP FOLLOWS THE TARGET, NOT THE LAYOUT ─────────────────
//
// T's `annPollTarget` sets `hidden` on Screenshot/Comment/Annotate off ONE fact
// — is there an `annFrame` — and `annFrame` is the pane iframe in the split
// layout or, in CHAT_ONLY, the host's marked frame (`annMarkedFrame`, T:6113).
// So the only state that hides the group is "nothing to act on". Measured on
// `:1777` for `?_side=claude` on a file: `#anncta` 312x26, all three buttons
// `hidden:false`; on a FOLDER listing in the same layout, all three `hidden`
// and the group collapsed. These pin both ends of that.

/** Every element carrying `cls`, by className, wherever it is in the tree. */
function byClass(r: ReturnType<typeof create>, cls: string) {
  return r.root.findAll(
    (n) => typeof n.type === "string" && String(n.props.className ?? "").split(/\s+/).includes(cls),
    { deep: true },
  );
}

test("a HOSTED chat shows the strip — landing included — because the host has a target", async () => {
  // The bug: `stripShown` read `!chatOnly`, a question about OUR layout, so the
  // sidebar lost the whole row on both views while the app sat on screen in the
  // middle column with its mark on it.
  const { r } = await mountChat({ annotateTarget: hostFrameStub() });
  await settle(20);
  // The landing, not the transcript: nothing has been sent.
  expect(byClass(r, "c-home").length).toBe(1);
  expect(byClass(r, "c-anntools").length).toBe(1);
  expect(byClass(r, "c-anncta").length).toBe(1);
  const seats = byClass(r, "c-anncta")[0].findAllByType("button");
  expect(seats.length).toBe(3);
  // ALL THREE ARE LIVE (PR3). When this landed, Comment and Annotate were seats
  // waiting for their handlers and the assertion was that they arrived disabled
  // rather than absent — the row must not grow two buttons under the reader's
  // hand. PR3 handed them those handlers, and a hosted mount whose host has
  // marked its frame is exactly the case they act on, so the row is three
  // working controls; "absent beats dead" now decides the whole row at once
  // (`capable`), which the next test pins.
  expect(seats[0].props.disabled).toBe(false);
  expect(seats[1].props.disabled).toBe(false);
  expect(seats[2].props.disabled).toBe(false);
});

test("the strip ROW stands even with nothing to photograph — it carries the ⋮", async () => {
  // T's `#anntools` is static markup and holds `← Chats` and `#kebab` as well as
  // the three seats, so a folder listing keeps the row and loses only the
  // buttons (T:526 `body.nopane #kebab { margin-left: auto }` is that state).
  // Native used to drop the whole row, which took the menu with it (P2-1) —
  // and PR3's own copy of these two tests pinned the old behaviour until the
  // re-stack (2026-09-10).
  const { r } = await mountChat({ annotateTarget: () => null });
  await settle(20);
  expect(byClass(r, "c-anntools").length).toBe(1);
  expect(byClass(r, "c-anncta").length).toBe(0);
  expect(byClass(r, "c-kebab").length).toBe(1);
});

test("the seats are absent on a chat-only mount with no host getter at all", async () => {
  // A cards tile and the peek modal pass none: there is genuinely nothing to
  // photograph, and "absent beats dead" (T:238-241).
  const { r } = await mountChat();
  await settle(20);
  expect(byClass(r, "c-anncta").length).toBe(0);
});

test("a CONTROLLER REBUILD does not re-send a spent ask (QA #1061)", async () => {
  // THE PATH THE TWO TESTS ABOVE CANNOT REACH. Both re-render the HOST, which
  // by construction cannot change the controller's identity — and the controller
  // is what the boot latch is keyed on (`bootedFor`), deliberately, so a new
  // target gets its own `openSession`/`resumeRun`. Swap `file` for one whose
  // `agentDir` is ALREADY in the resolver cache and both halves fire at once:
  // the controller memo (deps `[agentDir, file, params]`) rebuilds, the boot
  // re-runs with `bootDispatched` re-armed, and a sticky `askRef` took the
  // `if (ask)` branch a second time — `params.set({session_id: null, run: null})`
  // disowning the conversation on screen, and the same "Fix with AI" prompt
  // fired at the OTHER file.
  //
  // The ask is spent on the LATCH now, so the rebuild finds nothing to send.
  resetAgentDirCacheForTests();
  // Both targets pre-resolved, so neither swap spends a stat round-trip that
  // would take `agentDir` back to `undefined` and remount the body.
  await resolveAgentDir("/w/p");
  await resolveAgentDir("/w/p/other.html");
  const params = createMemoryParamsStore();
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(<ClaudeChat {...baseProps} params={params} initialAsk="fix it" />);
  });
  mounted.push(r);
  await settle(20);
  expect(started().length).toBe(1);
  expect(started()[0].params.message).toContain("fix it");
  const sessionAfterAsk = params.get("session_id");

  // The host swaps the target in place — and, like the real hosts, hands the
  // one-shot ask over as `undefined` on every render after the delivery.
  await act(async () => {
    r.update(<ClaudeChat {...baseProps} file="/w/p/other.html" params={params} />);
  });
  await settle(1700);

  // ONE run, still, and it is still the one the reader is looking at.
  expect(started().length).toBe(1);
  expect(params.get("session_id")).toBe(sessionAfterAsk);
  expect(params.get("session_id")).toBeTruthy();
});

/** The rendered TEXT of every element with exactly this class — flattened off
 *  the JSON tree rather than read out of `props.children`, so a sentence React
 *  splits into several nodes still comes back as one string (P3R1-8). */
function troubleText(r: ReturnType<typeof create>, klass: string): string[] {
  const out: string[] = [];
  const text = (n: unknown): string => {
    if (n === null || n === undefined || typeof n === "boolean") return "";
    if (typeof n === "string" || typeof n === "number") return String(n);
    if (Array.isArray(n)) return n.map(text).join("");
    const node = n as { children?: unknown };
    return text(node.children ?? "");
  };
  const walk = (n: unknown): void => {
    if (!n || typeof n !== "object") return;
    if (Array.isArray(n)) {
      for (const k of n) walk(k);
      return;
    }
    const node = n as { props?: { className?: string }; children?: unknown };
    if (String(node.props?.className ?? "") === klass) out.push(text(node.children ?? ""));
    walk(node.children);
  };
  walk(r.toJSON());
  return out;
}

// ---- the cover over the agentDir stat, and its 8 s backstop (P3-13) --------

test("the template lookup shows the SKELETON, not an empty box", async () => {
  // This branch used to return a bare `.chat-root` on the argument that "the
  // host is still holding its own cover over this box (ChatFrame's skeleton)".
  // True flag-OFF, where the host frames a booting document — but flag-on there
  // is no frame: `ChatMount`'s `Suspense` fallback covers the CHUNK LOAD and has
  // already resolved by the time this component runs its own stat. So a first
  // mount drew an empty box on the host background for the length of one
  // `/api/fs/stat`, and a cold wall of six cards drew six empty tiles where
  // legacy drew six skeletons.
  let release!: (r: Response) => void;
  (globalThis as { fetch: unknown }).fetch = async (input: unknown): Promise<Response> => {
    const url = String(typeof input === "string" ? input : (input as { url: string }).url);
    if (url.startsWith("/api/fs/stat")) {
      return new Promise<Response>((res) => {
        release = res;
      });
    }
    if (url.startsWith("/api/tasks/changes")) return new Promise<Response>(() => {});
    return jsonRes({});
  };

  const params = createMemoryParamsStore();
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(<ClaudeChat {...baseProps} params={params} />);
  });
  mounted.push(r);

  // The SAME node `Suspense` shows and `ChatFrame` holds over a booting frame,
  // so the two waits read as one wait rather than a skeleton flashing to an
  // empty box.
  const roots = r.root.findAll(
    (n) => typeof n.type === "string" && String((n.props as { className?: string }).className ?? "").includes("chat-frame"),
  );
  expect(roots.length).toBeGreaterThan(0);
  // A skeleton, not a bare plate: the shimmer bars are what makes it a wait.
  expect(
    r.root.findAll(
      (n) =>
        typeof n.type === "string" &&
        String((n.props as { className?: string }).className ?? "").includes("skel"),
    ).length,
  ).toBeGreaterThan(0);

  await act(async () => {
    release(
      jsonRes({
        path: "/w/p",
        is_dir: true,
        templates: [{ mode: "claude", path: "/w/p/.claude/template.html" }],
      }),
    );
  });
});

test("a stat that never settles gets an 8 s backstop to the TroubleView", async () => {
  // Legacy revealed at `CHAT_FRAME_FALLBACK_MS`; without a backstop a stalled
  // server — or a request the browser never answers — left the box covered for
  // ever, with no road to the branch that exists to explain exactly this.
  //
  // The TIMER is asserted rather than waited out: the real duration is 8 s, and
  // a suite that actually sleeps it pays that on every run. The firing is then
  // driven by hand, which also proves the branch it lands on.
  const { CHAT_FRAME_FALLBACK_MS } = await import("@platform/ui/ChatFrame");

  (globalThis as { fetch: unknown }).fetch = async (input: unknown): Promise<Response> => {
    const url = String(typeof input === "string" ? input : (input as { url: string }).url);
    if (url.startsWith("/api/fs/stat")) return new Promise<Response>(() => {});
    if (url.startsWith("/api/tasks/changes")) return new Promise<Response>(() => {});
    return jsonRes({});
  };

  const G = globalThis as Record<string, unknown>;
  const realTimeout = G.setTimeout as typeof setTimeout;
  const armed: Array<{ ms: number; fn: () => void }> = [];
  G.setTimeout = ((fn: () => void, ms?: number) => {
    if (ms === CHAT_FRAME_FALLBACK_MS) {
      armed.push({ ms, fn });
      return 0 as unknown as ReturnType<typeof setTimeout>;
    }
    return realTimeout(fn, ms);
  }) as typeof setTimeout;

  let r!: ReturnType<typeof create>;
  try {
    const params = createMemoryParamsStore();
    await act(async () => {
      r = create(<ClaudeChat {...baseProps} params={params} />);
    });
    mounted.push(r);

    // Exactly one backstop, at legacy's own number — one constant, so the two
    // waits cannot drift apart.
    expect(armed).toHaveLength(1);
    expect(armed[0]!.ms).toBe(8000);
  } finally {
    G.setTimeout = realTimeout;
  }

  const trouble = () =>
    r.root.findAll(
      (n) =>
        typeof n.type === "string" &&
        String((n.props as { className?: string }).className ?? "").includes("trouble-card"),
    );
  // Covered until it fires...
  expect(trouble()).toHaveLength(0);
  // ...and the reader is told, instead of watching a skeleton for ever.
  await act(async () => {
    armed[0]!.fn();
  });
  expect(trouble().length).toBeGreaterThan(0);

  // ── P3R1-8: AND WHAT IT SAYS IS TWO PLAIN SENTENCES ────────────────────
  //
  // The stalled stat is the commonest way into this card, and it used to read
  // "Something went wrong" over a monospace box saying "There is no claude
  // template for this folder." — a title that says nothing, a sentence about an
  // internal thing the reader cannot act on, and a claim that is false here:
  // the folder's template is fine, the request never answered (owner,
  // 2026-09-10: short and human-readable, one sentence of what happened and one
  // of what to do).
  expect(troubleText(r, "trouble-title")).toEqual(["This chat couldn't load."]);
  expect(troubleText(r, "trouble-explain")).toEqual([
    "Reload the page, or check that Fused Render is still running.",
  ]);
  // NO VERBATIM BOX: there are no machine words behind this failure, and a
  // monospace plate reading our own sentence back (or "(no message)") reads as
  // a program having said it.
  expect(
    r.root.findAll(
      (n) =>
        typeof n.type === "string" &&
        String((n.props as { className?: string }).className ?? "").includes("trouble-error"),
    ),
  ).toHaveLength(0);
  // …and nothing in the card names the app's insides.
  const words = JSON.stringify(r.toJSON());
  for (const term of ["agentDir", "template.html", "claude template", "stat", "controller"]) {
    expect(words).not.toContain(term);
  }
});

// ---- P3R1-8: the OTHER way into the boot card -----------------------------

test("no target at all gets its own two sentences, not the stalled-load ones", async () => {
  // Two different facts with two different things to do about them, so they are
  // not folded into one sentence: nothing to open a chat ON is the reader's own
  // next move, where a stalled load is the app's.
  const params = createMemoryParamsStore();
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(<ClaudeChat {...baseProps} file={null} params={params} />);
  });
  mounted.push(r);
  expect(troubleText(r, "trouble-title")).toEqual(["There's nothing to open a chat on."]);
  expect(troubleText(r, "trouble-explain")).toEqual([
    "Open a file or a folder first, then start the chat.",
  ]);
  expect(
    r.root.findAll(
      (n) =>
        typeof n.type === "string" &&
        String((n.props as { className?: string }).className ?? "").includes("trouble-error"),
    ),
  ).toHaveLength(0);
});


// ---- the three options the watcher was never handed (P3-19/20/37) ---------

test("the block says APP, the outline goes to a FILE, and its nodes carry a path", async () => {
  // `createAppStateWatcher` has taken all three options since it was written;
  // the call site passed NONE, so every default was quietly in force:
  //
  //   * P3-20 — the block called the user's running app "the preview" (T:5352-
  //     5411);
  //   * P3-37 — the whole outline was INLINED on every send, so the CLI re-read
  //     it on every later turn (the exact cost T:5177-5218 exists to avoid) and
  //     it warned `app-state outline kept inline: no screenshot directory` each
  //     time;
  //   * P3-19 — no outline node carried a `path`, while the block's own preamble
  //     tells the model `path` is the same anchorPath the pins use — so a pin
  //     could not be joined to a node, and D146's single-identifier promise was
  //     broken from the outline side.
  const uploads: Array<{ path: string; body: string }> = [];
  const base = globalThis.fetch;
  (globalThis as { fetch: unknown }).fetch = async (
    input: unknown,
    init?: { body?: unknown },
  ): Promise<Response> => {
    const url = String(typeof input === "string" ? input : (input as { url: string }).url);
    if (url === "/api/run") {
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        py: string;
        params: Record<string, string>;
      };
      // The shots dir the outline is offloaded into — the one answer the boot
      // stub does not give, and the only reason it stayed inline in tests.
      if (String(body.params?.action) === "shots_dir") {
        return jsonRes({ ok: true, result: { dir: "/w/p/.fused/shots" } });
      }
    }
    if (url.startsWith("/api/fs/upload")) {
      uploads.push({ path: url, body: "" });
      return jsonRes({ ok: true });
    }
    return base(input as RequestInfo, init as RequestInit);
  };

  await mountChat({ initialAsk: "what can you see?", annotateTarget: hostFrameStub() });
  await settle(30);

  expect(started().length).toBe(1);
  const message = started()[0].params.message;
  // The NOUN. A hosted target resolved through `app.py` is an app, and the
  // preamble now says so.
  expect(message).toContain("app");
  expect(message).not.toContain("the preview pane the reader is looking at");
  // OFFLOADED: a path instead of the outline, and the preamble that tells the
  // model where to read it.
  expect(message).toContain("dom_path");
  expect(message).toContain("The DOM outline is the JSON file at `dom_path`");
  expect(uploads.length).toBeGreaterThan(0);
});
