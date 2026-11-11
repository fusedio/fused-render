// THE BOOT'S LIFETIME, and the one answer it puts on the wire about the pane.
//
// Everything here is asserted through `/api/claude/agent`, because that is where the
// damage of getting it wrong actually lands: a run SPAWNED for a chat nobody is
// looking at any more, a second "Fix with AI" run on the same prompt, or a
// `has_pane: 1` telling the model it can see an app that was never framed.
// The mount is the real component over a stubbed `fetch` — the boot branch is a
// walk over three awaits, and only a real mount/unmount can cross it.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

const { ClaudeChat, annotationsForTests } = await import("./ClaudeChat");
const { publishBreakoutEnabled, resetFlagsForTests } = await import("./feature-flag");
const { Kebab } = await import("./ui/Kebab");
const { createMemoryParamsStore } = await import("./params/store");
const { resetListingFeedForTests } = await import("@shell/tasksPulse");
const { troubleReport } = await import("@platform/lib/trouble");
const { resetClaudeDefaultsForTests } = await import("@platform/lib/claude-defaults");

/** One `/api/claude/agent` call: the action, plus the fields. */
interface RunCall {
  action: string;
  params: Record<string, string>;
}

const runs: RunCall[] = [];
let historyReads = 0;
let historyTurns: unknown[] = [];
let pollHint: unknown = undefined;
/** Held back so a test can park the boot inside its detection wait: the
 *  agent's `defaults` read never answers. */
let holdDetection = false;
/** Held back so a test can send while the PANE is still unresolved: the only
 *  stat a booting chat makes is `usePaneState`'s own. */
let holdPaneStat = false;
let stats = 0;
/** Held back so a test can watch the landing's ready signal wait on the task
 *  listing the Recent list is drawn from (T:19282-19291). Resolved by
 *  `releaseSessions()`. */
let holdSessions = false;
let releaseSessions: () => void = () => {};
/** Every `/api/schedule` read of this mount — one per watcher tick, which is
 *  what makes an unwanted `scheduleResetForNewTranscript` visible. */
let scheduleReads = 0;

const realFetch = globalThis.fetch;

function jsonRes(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response;
}

/** The endpoints a booting chat touches. The agent answers the minimum
 *  that lets the run loop finish in one lap, so no test has to wait on a poll. */
function stubFetch(): void {
  (globalThis as { fetch: unknown }).fetch = async (
    input: unknown,
    init?: { body?: unknown },
  ): Promise<Response> => {
    const url = String(typeof input === "string" ? input : (input as { url: string }).url);
    if (url.startsWith("/api/fs/stat")) {
      stats++;
      if (holdPaneStat) return new Promise<Response>(() => {});
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
    // THE LANDING'S RECENT LIST, which reads the task listing now rather than
    // agent.py's `sessions` action (.claude-design/design.md §B).
    if (url === "/api/tasks") {
      if (holdSessions) {
        return new Promise<Response>((res) => {
          releaseSessions = () => res(jsonRes({ tasks: [] }));
        });
      }
      return jsonRes({ tasks: [] });
    }
    if (url === "/api/schedule") {
      scheduleReads++;
      return jsonRes({ entries: [] });
    }
    if (url.startsWith("/api/claude-sessions/history")) {
      historyReads++;
      return jsonRes({ turns: historyTurns });
    }
    if (url === "/api/prefs") return jsonRes({});
    if (url === "/api/claude/app-entry") return jsonRes({});
    if (url === "/api/claude/agent") {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, string>;
      const action = String(body.action ?? "");
      runs.push({ action, params: body });
      if (action === "defaults") {
        if (holdDetection) return new Promise<Response>(() => {});
        return jsonRes({});
      }
      if (action === "live_host") return jsonRes({ run_id: "" });
      if (action === "start") return jsonRes({ run_id: "r1" });
      if (action === "poll") return jsonRes({ done: true, session_id: "s1", text: "ok", ...(pollHint === undefined ? {} : { worktree_hint: pollHint }) });
      return jsonRes({});
    }
    return jsonRes({});
  };
}

beforeEach(() => {
  // THE GLOBAL DEFAULTS READ IS SINGLE-FLIGHT and process-wide
  // (platform/lib/claude-defaults `reading`). A chat mounted by an EARLIER file,
  // under a fetch stub that never answered, leaves that read pinned for the rest
  // of the bun run: every chat here then joins it, `defaults.ready` never comes,
  // and an `initialAsk` sits out ASK_DETECTION_TIMEOUT_MS (1.5 s) — so each
  // "sends once" test saw zero starts after its 20 ms settle. That was red on CI
  // only (Linux file order), never locally. Reset before, not only after.
  resetClaudeDefaultsForTests();
  runs.length = 0;
  historyReads = 0;
  historyTurns = [];
  pollHint = undefined;
  holdDetection = false;
  holdPaneStat = false;
  holdSessions = false;
  releaseSessions = () => {};
  stats = 0;
  scheduleReads = 0;
  // THE LISTING FEED, which is module state shared by every suite in the one
  // `bun test` process (`shell/tasksPulse`): the rows a previous mount's
  // `/api/tasks` returned are remembered and REPLAYED synchronously to the next
  // subscriber, so a landing that is supposed to be waiting on a held read would
  // otherwise be handed an answer before it ever asked.
  resetListingFeedForTests();
  stubFetch();
});

const mounted: Array<ReturnType<typeof create>> = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetFlagsForTests();
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
    // `hostFrame` only counts a frame whose document is READABLE (a null
    // `contentDocument` is the cross-origin canvases workbench, #1720); this
    // stub is the same-origin kind, so it has one. `body: null` keeps the
    // annotation layer (which needs a real DOM to inject into) out of a test
    // about the boot; the app-state walk reads `contentWindow.document` above.
    contentDocument: { body: null },
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
  holdDetection = true;
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

test("`noAnnTools` with a URL-carried annmode=1 boots DISARMED, and stays so", async () => {
  // The canvas node pane (`_noann=1`): the chat is pinned to its chat view, so
  // an armed mode would have no toggle to undo it. `bootFromParam` arms AFTER
  // the hook's mount-time disarm (it waits for the target), which is why the
  // armed state itself is watched (CodeRabbit, #1880).
  const params = createMemoryParamsStore();
  params.set({ annmode: "1" });
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(
      <ClaudeChat {...baseProps} params={params} annotateTarget={hostFrameStub()} noAnnTools />,
    );
  });
  mounted.push(r);
  await settle(20);
  const ann = annotationsForTests();
  expect(ann).not.toBeNull();
  expect(ann!.armed).toBe(false);
  expect(ann!.locked).toBe(false);
});

test("the same annmode=1 boot ARMS without `noAnnTools` (the control)", async () => {
  const params = createMemoryParamsStore();
  params.set({ annmode: "1" });
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(<ClaudeChat {...baseProps} params={params} annotateTarget={hostFrameStub()} />);
  });
  mounted.push(r);
  await settle(20);
  expect(annotationsForTests()!.armed).toBe(true);
});

test("a CONTROLLER REBUILD does not re-send a spent ask (QA #1061)", async () => {
  // THE PATH THE TWO TESTS ABOVE CANNOT REACH. Both re-render the HOST, which
  // by construction cannot change the controller's identity — and the controller
  // is what the boot latch is keyed on (`bootedFor`), deliberately, so a new
  // target gets its own `openSession`/`resumeRun`. Swap `file` in place and
  // both halves fire at once: the controller memo (deps `[file, params]`)
  // rebuilds, the boot
  // re-runs with `bootDispatched` re-armed, and a sticky `askRef` took the
  // `if (ask)` branch a second time — `params.set({session_id: null, run: null})`
  // disowning the conversation on screen, and the same "Fix with AI" prompt
  // fired at the OTHER file.
  //
  // The ask is spent on the LATCH now, so the rebuild finds nothing to send.
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

// ---- R1-3: the clipboard does not say what the screen stopped saying ---------

test("a failure with nothing verbatim behind it reports no error section", () => {
  // `troubleReport` printed `Error:` over "(no message)" for a blank error, so
  // "Copy the details" handed on the one string P3R1-8 removed — as the only
  // machine fact in it. A failure with nothing verbatim behind it now reports
  // no error section at all.
  const report = troubleReport({ what: "opening the chat on /a/b.py", error: "" });
  expect(report).not.toContain("(no message)");
  expect(report).not.toContain("Error:");
  // What it does carry is what it actually knows.
  expect(report).toContain("What the app was doing: opening the chat on /a/b.py");
  expect(report).toContain("Help: ");
  // A caller WITH bytes is untouched.
  expect(troubleReport({ what: "x", error: "ENOTFOUND api" })).toContain("Error:\nENOTFOUND api");
});

// ---- P3R1-8: the OTHER way into the boot card -----------------------------

test("no target at all gets its own two sentences", async () => {
  // Nothing to open a chat ON is the reader's own next move.
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
  // AND NO RETRY BUTTON HERE (R1-4): this card's own next move is opening a
  // file, so a button back to the same empty room would be an action the copy
  // never asked for.
  expect(
    r.root
      .findAll((n) => n.type === "button")
      .map((n) => String((n.props as { children?: unknown }).children ?? "")),
  ).not.toContain("Reload the page");
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
    if (url === "/api/claude/agent") {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, string>;
      // The shots dir the outline is offloaded into — the one answer the boot
      // stub does not give, and the only reason it stayed inline in tests.
      if (String(body.action) === "shots_dir") {
        return jsonRes({ dir: "/w/p/.fused/shots" });
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

test("THE SCHEDULE RESET WAITS FOR A TRANSCRIPT REPLACEMENT (T:18000)", async () => {
  // T calls `scheduleResetForNewTranscript()` from `loadHistory`'s non-refresh
  // branch and from nowhere else, and each call re-ticks the poller. Keyed on
  // the session id instead, the reset fires twice for free: once on MOUNT — a
  // second `/api/schedule` racing the one `watcher.start()` already issues — and
  // once when the first poll of a brand-new chat reports its id, MID-RUN, where
  // it re-arms `baselined = false` and the next tick then silently writes off a
  // scheduled run that fired in that window.
  await mountChat({ initialAsk: "go" });
  await settle(20);
  // ONE read: the watcher's own baseline tick. The mount is not a replacement,
  // and neither is the session id the run's first poll just reported.
  expect(started().length).toBe(1);
  expect(scheduleReads).toBe(1);
});

// ── the landing's ready signal waits for its list (P4-14) ───────────────────

test("THE LANDING IS READY WHEN ITS LIST HAS ANSWERED (T:19282-19291)", async () => {
  // T fires `markChatReady()` after `await loadRecent()`, and the host uncovers
  // the pane on that signal — so firing it first shows a landing whose one list
  // is still a skeleton, which is the state the read is about to replace.
  holdSessions = true;
  let ready = 0;
  await mountChat({ onReady: () => ready++ });
  expect(ready).toBe(0);
  releaseSessions();
  await settle();
  expect(ready).toBe(1);
});

// THE SLIM HEADER IN A PEEK (headerInPeek): the Task page side peek and peek modal
// draw the chat worktree seat and session label under the host head. A
// conversation is up once a session id is on the mount.
test("a peek with headerInPeek draws the slim top bar; a bare peek and a compact one do not", async () => {
  const withHead = await mountChat({ peek: true, headerInPeek: true, initialSessionId: "sess-abc12345" });
  await settle(20);
  expect(byClass(withHead.r, "chat-peek-head").length).toBe(1);
  expect(byClass(withHead.r, "c-topbar").length).toBe(1);
  expect(byClass(withHead.r, "c-session").length).toBe(1);
  // Back / picker / kebab stay the host job in a peek.
  expect(byClass(withHead.r, "c-anntools").length).toBe(0);

  const bare = await mountChat({ peek: true, initialSessionId: "sess-abc12345" });
  await settle(20);
  expect(byClass(bare.r, "chat-peek-head").length).toBe(0);
  expect(byClass(bare.r, "c-topbar").length).toBe(0);

  const compact = await mountChat({ compact: true, peek: true, headerInPeek: true, initialSessionId: "sess-abc12345" });
  await settle(20);
  expect(byClass(compact.r, "c-topbar").length).toBe(0);
});

// BREAKOUT DOOR: the top bar carries no Breakout button any more. The ⋮ menu
// holds the item (the Kebab's `breakout` prop) in either layout, wherever the
// gate passes.
const kebabOf = (r: ReturnType<typeof create>) => r.root.findAllByType(Kebab)[0]!;
test("no Breakout button in the bar: the kebab carries the item in the merged head and in the strip", async () => {
  const onBreakout = () => {};
  const merged = await mountChat({ mergedHead: true, onBreakout, initialSessionId: "sess-abc12345" });
  await settle(20);
  // The flag is opt-in: the stubbed prefs read leaves it off until published.
  await act(async () => publishBreakoutEnabled(true));
  await settle(20);
  expect(byClass(merged.r, "c-anntools").length).toBe(0);
  expect(byClass(merged.r, "c-breakout").length).toBe(0);
  expect(kebabOf(merged.r).props.breakout.picking).toBe(false);

  const strip = await mountChat({ onBreakout, initialSessionId: "sess-abc12345" });
  await settle(20);
  expect(byClass(strip.r, "c-anntools").length).toBe(1);
  expect(byClass(strip.r, "c-breakout").length).toBe(0);
  expect(kebabOf(strip.r).props.breakout).toBeDefined();
});

test("the kebab has no Breakout item while the flag is off (default) or without onBreakout", async () => {
  const strip = await mountChat({ onBreakout: () => {}, initialSessionId: "sess-abc12345" });
  await settle(20);
  expect(kebabOf(strip.r).props.breakout).toBeUndefined();
  const none = await mountChat({ initialSessionId: "sess-abc12345" });
  await settle(20);
  await act(async () => publishBreakoutEnabled(true));
  await settle(20);
  expect(kebabOf(none.r).props.breakout).toBeUndefined();
});

// BREAKOUT BOOT: a breakout card is a fresh chat. It restores no history and
// wears the chip for its seeded messages until the first send carries them.
test("a breakout boots empty: no history fetch, a chip for the selected messages above the composer", async () => {
  const context = { block: "<breakout-context>\nThe user opened this side chat from 2 messages of their main chat.\n\nUser: q\n\nAssistant: a\n</breakout-context>", count: 2, title: "Parent" };
  const { r } = await mountChat({ breakout: true, breakoutContext: context });
  await settle(20);
  expect(historyReads).toBe(0);
  expect(byClass(r, "annsum-breakout").length).toBe(1);
});

test("a breakout card opens on the chat, never the folder landing", async () => {
  const { r } = await mountChat({ breakout: true });
  await settle(20);
  expect(byClass(r, "c-home").length).toBe(0);
});

// SEND BACK: the card offers it only once the breakout has stopped, so the
// reported `send` must start a fresh turn on an idle chat. It used to be a
// follow-up and failed "no run to attach this message to".
test("a breakout's reported send starts a new turn when the chat is idle", async () => {
  let reported: { send: (t: string) => void } | null = null;
  await mountChat({ breakout: true, onBreakoutState: (s: { send: (t: string) => void }) => { reported = s; } });
  await settle(20);
  expect(reported).not.toBeNull();
  expect(started()).toHaveLength(0);
  await act(async () => reported!.send("handoff text"));
  await settle(20);
  expect(started()).toHaveLength(1);
  expect(started()[0]!.params["message"] ?? JSON.stringify(started()[0])).toContain("handoff text");
});

// BREAKOUT PICK MODE: the top-bar button toggles it; each exchange is one click target.
describe("breakout pick mode", () => {
  const HISTORY = [
    { role: "user", text: "first question", uuid: "u1" },
    { role: "assistant", text: "first answer", uuid: "a1" },
    { role: "user", text: "second question", uuid: "u2" },
    { role: "assistant", text: "second answer", uuid: "a2" },
  ];
  type Opened = { context: { block: string; count: number }; forkKey?: string };
  async function mountParent(history: unknown[] = HISTORY, capture?: ((e: unknown) => void)[]) {
    historyTurns = history;
    const opened: Opened[] = [];
    const doc = document as unknown as { addEventListener: (t: string, fn: (e: unknown) => void) => void };
    const prev = doc.addEventListener;
    if (capture) doc.addEventListener = (t, fn) => void (t === "keydown" && capture.push(fn));
    try {
      const m = await mountChat({ initialSessionId: "sess-abc12345", onBreakout: (req: Opened) => void opened.push(req) });
      await settle(20);
      await act(async () => publishBreakoutEnabled(true));
      await settle(20);
      return { ...m, opened };
    } finally {
      doc.addEventListener = prev;
    }
  }
  const toggle = (r: ReturnType<typeof create>) => act(() => kebabOf(r).props.breakout.onToggle());
  const picked = (r: ReturnType<typeof create>) => byClass(r, "is-pick-on").length;
  const exchange = (r: ReturnType<typeof create>, i: number) =>
    r.root.findAll((x) => typeof x.props.className === "string" && /\bturn\b/.test(x.props.className) && /\bis-pick\b/.test(x.props.className) && typeof x.type === "string")[i]!;
  const click = (r: ReturnType<typeof create>, i: number, target: unknown = { closest: () => null }) =>
    act(() => exchange(r, i).props.onClick({ target }));

  test("no per-message breakout button, and no pick UI until the kebab item is pressed", async () => {
    const { r } = await mountParent();
    expect(byClass(r, "turn-breakout").length).toBe(0);
    expect(byClass(r, "breakout-sel-bar").length).toBe(0);
    expect(kebabOf(r).props.breakout.picking).toBe(false);
  });

  test("the kebab item enters pick mode with the latest exchange picked, and toggles it off", async () => {
    const { r } = await mountParent();
    toggle(r);
    expect(kebabOf(r).props.breakout.picking).toBe(true);
    expect(byClass(r, "breakout-sel-bar").length).toBe(1);
    expect(picked(r)).toBe(2);
    expect(byClass(r, "turn-pick-check").length).toBe(2);
    toggle(r);
    expect(byClass(r, "breakout-sel-bar").length).toBe(0);
    expect(byClass(r, "is-pick").length).toBe(0);
  });

  test("clicking an exchange toggles it; a click on a link or button inside does not", async () => {
    const { r } = await mountParent();
    toggle(r);
    click(r, 0);
    expect(picked(r)).toBe(4);
    click(r, 1, { closest: () => ({}) });
    expect(picked(r)).toBe(4);
    click(r, 1);
    expect(picked(r)).toBe(2);
    click(r, 2);
    expect(picked(r)).toBe(0);
  });

  // RIGHT-CLICK on a message starts the pick with THAT exchange picked.
  const idleTurns = (r: ReturnType<typeof create>) =>
    r.root.findAll((x) => typeof x.type === "string" && /\bturn\b/.test(String(x.props.className ?? "")) && typeof x.props.onContextMenu === "function");
  const ctx = (r: ReturnType<typeof create>, i: number, target: unknown = { closest: () => null }) => {
    const ev = { target, prevented: false, preventDefault() { this.prevented = true; } };
    act(() => idleTurns(r)[i]!.props.onContextMenu(ev));
    return ev;
  };

  test("right-click on the FIRST exchange picks it and nothing else, with no custom menu", async () => {
    const { r } = await mountParent();
    const ev = ctx(r, 0);
    expect(ev.prevented).toBe(true);
    expect(byClass(r, "breakout-sel-bar").length).toBe(1);
    expect(kebabOf(r).props.breakout.picking).toBe(true);
    expect(picked(r)).toBe(2);
    // The first exchange, not the default (latest) one.
    expect(exchange(r, 0).props.className).toContain("is-pick-on");
    expect(exchange(r, 2).props.className).not.toContain("is-pick-on");
    expect(byClass(r, "context-menu").length).toBe(0);
  });

  test("right-click on an assistant turn picks its exchange", async () => {
    const { r } = await mountParent();
    ctx(r, 3);
    expect(picked(r)).toBe(2);
    expect(exchange(r, 3).props.className).toContain("is-pick-on");
  });

  test("right-click on a link or a code block keeps the native menu and does not pick", async () => {
    const { r } = await mountParent();
    const ev = ctx(r, 1, { closest: (sel: string) => (sel.includes("pre") ? {} : null) });
    expect(ev.prevented).toBe(false);
    expect(kebabOf(r).props.breakout.picking).toBe(false);
    expect(byClass(r, "breakout-sel-bar").length).toBe(0);
  });

  test("right-click with a text selection keeps the native menu", async () => {
    const { r } = await mountParent();
    const w = window as unknown as { getSelection?: () => { toString(): string } };
    const prev = w.getSelection;
    w.getSelection = () => ({ toString: () => "some words" });
    try {
      expect(ctx(r, 1).prevented).toBe(false);
    } finally {
      w.getSelection = prev;
    }
    expect(kebabOf(r).props.breakout.picking).toBe(false);
  });

  test("while picking, right-click toggles the exchange and suppresses the native menu", async () => {
    const { r } = await mountParent();
    toggle(r);
    expect(picked(r)).toBe(2);
    const ev = exchange(r, 0);
    const e1 = { target: { closest: () => null }, prevented: false, preventDefault() { this.prevented = true; } };
    act(() => ev.props.onContextMenu(e1));
    expect(e1.prevented).toBe(true);
    expect(picked(r)).toBe(4);
    const e2 = { target: { closest: () => null }, prevented: false, preventDefault() { this.prevented = true; } };
    act(() => exchange(r, 0).props.onContextMenu(e2));
    expect(picked(r)).toBe(2);
  });

  test("no right-click pick without onBreakout, or while the flag is off", async () => {
    historyTurns = HISTORY;
    const off = await mountChat({ initialSessionId: "sess-abc12345", onBreakout: () => {} });
    await settle(20);
    expect(idleTurns(off.r).length).toBe(0);
  });

  describe("breakout cwd follows the chat's worktree hint", () => {
    async function openWith(hint: unknown): Promise<Record<string, unknown>> {
      pollHint = hint;
      historyTurns = HISTORY;
      const opened: Record<string, unknown>[] = [];
      const { r } = await mountChat({ initialSessionId: "sess-abc12345", initialAsk: "go", onBreakout: (req: Record<string, unknown>) => void opened.push(req) });
      await settle(20);
      await act(async () => publishBreakoutEnabled(true));
      await settle(20);
      toggle(r);
      act(() => byClass(r, "breakout-sel-start")[0]!.props.onClick());
      expect(opened).toHaveLength(1);
      return opened[0]!;
    }
    test("an absolute hint path rides as cwd", async () => {
      const req = await openWith({ path: "/w/repo/.claude/worktrees/x", branch: "x", via: "bash" });
      expect(req["cwd"]).toBe("/w/repo/.claude/worktrees/x");
      expect(req["file"]).toBe(baseProps.file);
    });
    test("no hint, an exit hint, or a relative path leaves cwd unset", async () => {
      for (const hint of [undefined, { path: "", branch: "", via: "exit" }, { path: "wt/x", branch: "", via: "enter" }, { path: "", branch: "", via: "agent", label: "a" }]) {
        const req = await openWith(hint);
        expect("cwd" in req).toBe(false);
        for (const m of mounted.splice(0)) await act(async () => m.unmount());
      }
    });
  });

  test("Start opens with the picked keys and leaves pick mode; Start is disabled at zero", async () => {
    const { r, opened } = await mountParent();
    toggle(r);
    click(r, 0);
    act(() => byClass(r, "breakout-sel-start")[0]!.props.onClick());
    expect(opened).toHaveLength(1);
    expect(opened[0]!.context.count).toBe(4);
    expect(byClass(r, "breakout-sel-bar").length).toBe(0);
    toggle(r);
    click(r, 2);
    expect(picked(r)).toBe(0);
    expect(byClass(r, "breakout-sel-start")[0]!.props.disabled).toBe(true);
  });

  // Enter while picking is the Start button: the words become the breakout's first
  // message and never reach the main session.
  const box = (r: ReturnType<typeof create>) => r.root.findAllByType("textarea")[0]!;
  const type = (r: ReturnType<typeof create>, value: string) =>
    act(() => box(r).props.onChange({ currentTarget: { value, selectionStart: value.length, selectionEnd: value.length, style: {} }, target: { value } }));
  const enter = (r: ReturnType<typeof create>) =>
    act(() => box(r).props.onKeyDown({ key: "Enter", shiftKey: false, ctrlKey: false, metaKey: false, keyCode: 13, nativeEvent: {}, preventDefault() {}, stopPropagation() {} }));

  test("Enter while picking starts the breakout with the typed text and posts nothing to the main chat", async () => {
    const { r, opened } = await mountParent();
    toggle(r);
    expect(box(r).props.placeholder).toBe("Ask the breakout · Enter starts it");
    type(r, "  why is it slow?  ");
    enter(r);
    await settle(20);
    expect(opened).toHaveLength(1);
    expect((opened[0] as { prompt?: string }).prompt).toBe("why is it slow?");
    expect(started()).toHaveLength(0);
    expect(byClass(r, "breakout-sel-bar").length).toBe(0);
    expect(box(r).props.value).toBe("");
  });

  test("Enter with nothing picked sends nothing and keeps the words", async () => {
    const { r, opened } = await mountParent();
    toggle(r);
    click(r, 2);
    expect(picked(r)).toBe(0);
    type(r, "hello");
    enter(r);
    await settle(20);
    expect(opened).toHaveLength(0);
    expect(started()).toHaveLength(0);
    expect(box(r).props.value).toBe("hello");
  });

  test("the Start button carries the typed text; the send button is named Start breakout", async () => {
    const { r, opened } = await mountParent();
    toggle(r);
    const send = byClass(r, "c-send")[0]!;
    expect(send.props["aria-label"]).toBe("Start breakout");
    expect(send.props.title).toBe("Start breakout");
    type(r, "q?");
    act(() => byClass(r, "breakout-sel-start")[0]!.props.onClick());
    expect(opened).toHaveLength(1);
    expect((opened[0] as { prompt?: string }).prompt).toBe("q?");
    expect(started()).toHaveLength(0);
  });

  test("Start with an empty composer opens without a prompt", async () => {
    const { r, opened } = await mountParent();
    toggle(r);
    act(() => byClass(r, "breakout-sel-start")[0]!.props.onClick());
    expect(opened).toHaveLength(1);
    expect("prompt" in opened[0]!).toBe(false);
  });

  test("an interrupted exchange with no reply text is not the default pick", async () => {
    const { r } = await mountParent([...HISTORY, { role: "user", text: "third question", uuid: "u3" }, { role: "assistant", text: "", uuid: "a3" }]);
    toggle(r);
    expect(picked(r)).toBe(2);
  });

  test("a breakout card sends its prompt once as the first message", async () => {
    const sent: string[] = [];
    await mountChat({ breakout: true, breakoutPrompt: "first q", onBreakoutPromptSent: () => void sent.push("x") });
    await settle(40);
    expect(started()).toHaveLength(1);
    expect(started()[0]!.params["prompt"] ?? JSON.stringify(started()[0])).toContain("first q");
    expect(sent).toHaveLength(1);
  });

  test("Cancel leaves pick mode without opening", async () => {
    const { r, opened } = await mountParent();
    toggle(r);
    act(() => byClass(r, "breakout-sel-cancel")[0]!.props.onClick());
    expect(opened).toHaveLength(0);
    expect(byClass(r, "breakout-sel-bar").length).toBe(0);
  });

  test("Escape cancels pick mode, and is left alone otherwise", async () => {
    const keydown: ((e: unknown) => void)[] = [];
    const { r } = await mountParent(HISTORY, keydown);
    const press = async () => {
      const evs = keydown.map(() => ({ key: "Escape", defaultPrevented: false, target: null, prevented: false, preventDefault() { this.prevented = true; } }));
      await act(async () => keydown.forEach((fn, i) => fn(evs[i])));
      return evs;
    };
    const idle = await press();
    expect(idle.some((e) => e.prevented)).toBe(false);
    toggle(r);
    expect(picked(r)).toBe(2);
    await press();
    expect(byClass(r, "breakout-sel-bar").length).toBe(0);
    expect(picked(r)).toBe(0);
  });
  // THE HOST'S MENU READS THE CHAT'S BREAKOUT STATE (the Tasks peek hides the ⋮).
  test("onBreakoutControl reports available + picking, and its toggle enters pick mode", async () => {
    const seen: Array<{ available: boolean; picking: boolean; toggle: () => void } | null> = [];
    historyTurns = HISTORY;
    const { r } = await mountChat({
      initialSessionId: "sess-abc12345",
      onBreakout: () => {},
      onBreakoutControl: (c: (typeof seen)[number]) => void seen.push(c),
    });
    await settle(20);
    const last = () => seen[seen.length - 1];
    expect(last()?.available).toBe(false); // flag off
    await act(async () => publishBreakoutEnabled(true));
    await settle(20);
    expect(last()).toMatchObject({ available: true, picking: false });
    const first = last()!.toggle;
    act(() => last()!.toggle());
    expect(last()).toMatchObject({ available: true, picking: true });
    expect(byClass(r, "breakout-sel-bar").length).toBe(1);
    expect(last()!.toggle).toBe(first); // stable across renders
    act(() => last()!.toggle());
    expect(last()).toMatchObject({ picking: false });
    await act(async () => r.unmount());
    mounted.splice(mounted.indexOf(r), 1);
    expect(last()).toBeNull();
  });

  test("Escape while picking: a capture listener on window cancels the pick and stops outer handlers", async () => {
    const win = window as unknown as { addEventListener: (t: string, f: (e: unknown) => void, o?: unknown) => void; removeEventListener: (t: string, f: (e: unknown) => void, o?: unknown) => void };
    const prevAdd = win.addEventListener;
    const prevRemove = win.removeEventListener;
    const caps: Array<(e: unknown) => void> = [];
    win.addEventListener = (t, f, o) => void (t === "keydown" && (o === true || (o as { capture?: boolean } | undefined)?.capture) && caps.push(f));
    win.removeEventListener = (t, f) => void (t === "keydown" && caps.includes(f) && caps.splice(caps.indexOf(f), 1));
    try {
      const { r } = await mountParent();
      const outer: unknown[] = [];
      const press = async () => {
        let stopped = false;
        const ev = { key: "Escape", defaultPrevented: false, target: null,
          preventDefault() { this.defaultPrevented = true; }, stopPropagation() { stopped = true; }, stopImmediatePropagation() { stopped = true; } };
        await act(async () => {
          for (const f of [...caps]) f(ev);
          if (!stopped) outer.push(ev); // the bubble listener on document
        });
        return ev;
      };
      // Not picking: no listener, the press reaches the outer handler untouched.
      expect(caps).toHaveLength(0);
      const idle = await press();
      expect(idle.defaultPrevented).toBe(false);
      expect(outer).toHaveLength(1);
      toggle(r);
      expect(kebabOf(r).props.breakout.picking).toBe(true);
      expect(caps).toHaveLength(1);
      const ev = await press();
      expect(ev.defaultPrevented).toBe(true);
      expect(outer).toHaveLength(1); // not called again
      expect(kebabOf(r).props.breakout.picking).toBe(false);
      expect(caps).toHaveLength(0); // listener removed
    } finally {
      win.addEventListener = prevAdd;
      win.removeEventListener = prevRemove;
    }
  });
});
