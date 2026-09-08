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

test("a chat-only mount's first send says has_pane: 0", async () => {
  // CHAT_ONLY has no pane of ours whatever the target turns out to be
  // (`decidePane` answers `kind: "none"` for every kind there), so the status is
  // "none" from the first render and never spends a round-trip in "resolving".
  // Pinned with the pane's own stat held: the ask leaves with nothing answered.
  holdPaneStat = true;
  await mountChat({ initialAsk: "fix the chart" });
  await settle(20);
  expect(started().length).toBe(1);
  expect(started()[0].params.has_pane).toBe("0");
  expect(started()[0].params.message).toContain("fix the chart");
});

test("an UNRESOLVED pane is not a pane either: has_pane: 0 while it is resolving", async () => {
  // The split layout, whose target may well end up with a pane — but has not
  // yet. "resolving" used to count as one, which made the first turn's answer a
  // guess the model had no way to check.
  holdPaneStat = true;
  await mountChat({ chatOnly: false, initialAsk: "fix the chart" });
  await settle(20);
  expect(started().length).toBe(1);
  expect(started()[0].params.has_pane).toBe("0");
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
