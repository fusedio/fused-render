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
const { resetAgentDirCacheForTests } = await import("./protocol/agent");

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
  const frame = { isConnected: true, contentWindow: win } as unknown as HTMLIFrameElement;
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
