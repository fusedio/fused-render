// `useSelfFixReadiness`, DRIVEN — the hook the download manager's failed row and
// the Preferences panel use to decide what it is honest to OFFER, before anyone
// clicks (SPEC §43, SF-13d, SF-13f).
//
// Worth driving rather than asserting on source, because three of the four
// things it has to get right are invisible in a screenshot and in a grep: how
// many requests N rows make, what a failed request leaves behind for the next
// row, and which way absence answers.
//
// react-test-renderer with a local probe rather than the listing's hook-harness:
// platform may not import apps (frontend/scripts/check-boundaries.mjs), and all
// this hook needs is a mount and a microtask — no virtual clock.
import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";
import { act, create } from "react-test-renderer";
import { createElement, type ReactElement } from "react";
import type { Config } from "@platform/lib/api";
import type { SelfFixReadinessState } from "@platform/lib/hooks";

// --- the module boundary ------------------------------------------------------
let reply: () => Promise<Config>;
let calls = 0;

mock.module("@platform/lib/api", () => ({
  getConfig: () => {
    calls += 1;
    return reply();
  },
}));

// hooks.ts pulls in the router and the sidebar store, which read `location` and
// register listeners at module scope; bun has no DOM. Same `??=` shim as
// router.test.ts and toast.test.ts — never an assignment, and never a delete
// afterwards: the suite shares one process, so a file that OVERWRITES `window`
// hands the toast queue a stub with no `setTimeout`, and one that removes it
// takes the shim out from under every file whose own `??=` already ran.
(globalThis as { location?: unknown }).location ??= {
  pathname: "/",
  search: "",
  href: "http://localhost/",
};
(globalThis as { history?: unknown }).history ??= {
  state: null,
  pushState() {},
  replaceState() {},
};
(globalThis as { window?: unknown }).window ??= {
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() {},
  setTimeout: globalThis.setTimeout.bind(globalThis),
  clearTimeout: globalThis.clearTimeout.bind(globalThis),
};

const { useSelfFixReadiness, resetSelfFixReadiness } = await import("@platform/lib/hooks");

const config = (over: Partial<Config> = {}) => ({ version: "1.2.3", ...over }) as Config;

/** Mount the hook and expose its latest return value. */
function mount(): { current: () => SelfFixReadinessState; unmount: () => void } {
  let latest: SelfFixReadinessState = {
    readOnly: false,
    claudeMissing: false,
    recheck: () => {},
  };
  const Probe = (): ReactElement | null => {
    latest = useSelfFixReadiness();
    return null;
  };
  let renderer: ReturnType<typeof create>;
  act(() => {
    renderer = create(createElement(Probe));
  });
  return {
    current: () => latest,
    unmount: () => act(() => renderer.unmount()),
  };
}

/** Let the config reply and the state update it causes both settle. */
async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  calls = 0;
  reply = () => Promise.resolve(config());
  resetSelfFixReadiness();
});

afterEach(() => {
  resetSelfFixReadiness();
});

describe("useSelfFixReadiness", () => {
  test("a read-only installation is reported, so the button can change its verb", async () => {
    reply = () => Promise.resolve(config({ read_only: true }));
    const probe = mount();
    await settle();
    expect(probe.current().readOnly).toBe(true);
    probe.unmount();
  });

  test("absence is the ordinary case — an install the user owns", async () => {
    // /api/config carries no `read_only: false`; the field is present only when
    // the tree cannot be written to. A hook that checked for the false value
    // would report every writable install as read-only.
    const probe = mount();
    await settle();
    expect(probe.current().readOnly).toBe(false);
    probe.unmount();
  });

  test("the FIRST paint promises a fix rather than a diagnosis", async () => {
    // Before the reply lands there is no answer, and the label has to say
    // something. It says "Fix this": nearly every installation is writable, and
    // the cost of being wrong for one paint is a verb that corrects itself —
    // against a session that is told the truth either way.
    reply = () => new Promise(() => {}); // a config read nobody answers
    const probe = mount();
    await settle();
    expect(probe.current().readOnly).toBe(false);
    probe.unmount();
  });

  test("a missing Claude Code is reported, so nothing offers a session", async () => {
    // The precondition that outranks the other one: without the CLI neither a
    // fix nor a diagnosis can start, so the surfaces stop offering a session at
    // all and name the thing that would make one possible.
    reply = () => Promise.resolve(config({ claude_missing: true }));
    const probe = mount();
    await settle();
    expect(probe.current().claudeMissing).toBe(true);
    probe.unmount();
  });

  test("both preconditions ride ONE read", async () => {
    // They are read together because they are used together — an admin-installed
    // copy on a machine with no Claude is one config fetch, not two.
    reply = () => Promise.resolve(config({ read_only: true, claude_missing: true }));
    const probe = mount();
    await settle();
    expect(probe.current().readOnly).toBe(true);
    expect(probe.current().claudeMissing).toBe(true);
    expect(calls).toBe(1);
    probe.unmount();
  });

  test("absence answers no on BOTH — the ordinary machine", async () => {
    // Neither field has a false shape on the wire; a hook that checked for one
    // would report every healthy machine as broken.
    const probe = mount();
    await settle();
    expect(probe.current().readOnly).toBe(false);
    expect(probe.current().claudeMissing).toBe(false);
    probe.unmount();
  });

  test("recheck notices that the user installed what the button asked for", async () => {
    // THE BUG THIS EXISTS FOR. The button says "Set up Claude Code", so the one
    // state it caches is the one state it is asking the user to change — and
    // before `recheck`, a user who did exactly that and clicked again in the
    // same tab was still told the binary was missing until they reloaded.
    reply = () => Promise.resolve(config({ claude_missing: true }));
    const probe = mount();
    await settle();
    expect(probe.current().claudeMissing).toBe(true);

    reply = () => Promise.resolve(config());          // they installed it
    await act(async () => {
      probe.current().recheck();
    });
    await settle();
    expect(probe.current().claudeMissing).toBe(false);
    expect(calls).toBe(2);                            // asked again, not guessed
    probe.unmount();
  });

  test("recheck drops the SHARED cache, so every row agrees about one machine", async () => {
    reply = () => Promise.resolve(config({ claude_missing: true }));
    const first = mount();
    const second = mount();
    await settle();
    expect(calls).toBe(1);

    reply = () => Promise.resolve(config());
    await act(async () => {
      first.current().recheck();
    });
    await settle();
    // One re-read, and the row that did not ask for it sees the new answer too:
    // three failed rows must not disagree about whether Claude is installed.
    expect(calls).toBe(2);
    expect(first.current().claudeMissing).toBe(false);
    expect(second.current().claudeMissing).toBe(false);
    first.unmount();
    second.unmount();
  });

  test("three failed rows ask ONCE, not three times", async () => {
    // The PROMISE is cached, not just the answer, so simultaneous mounts share
    // one request. A user with three failed downloads is the ordinary way to
    // mount three of these, and they are all asking about one directory.
    reply = () => Promise.resolve(config({ read_only: true }));
    const probes = [mount(), mount(), mount()];
    await settle();
    expect(calls).toBe(1);
    for (const probe of probes) expect(probe.current().readOnly).toBe(true);
    for (const probe of probes) probe.unmount();
  });

  test("a failed read is not remembered as an answer", async () => {
    // A transient fetch failure must not pin "writable" for the rest of the
    // session — the next row that mounts asks again. Cached rejection would make
    // one dropped request outlive the reason for it.
    reply = () => Promise.reject(new Error("offline"));
    const first = mount();
    await settle();
    expect(first.current().readOnly).toBe(false);
    first.unmount();

    reply = () => Promise.resolve(config({ read_only: true }));
    const second = mount();
    await settle();
    expect(second.current().readOnly).toBe(true);
    expect(calls).toBe(2);
    second.unmount();
  });

  test("a successful read IS remembered", async () => {
    // Permissions on the install root are a property of how the app was
    // installed, so a later row re-uses the answer instead of re-asking.
    reply = () => Promise.resolve(config({ read_only: true }));
    const first = mount();
    await settle();
    first.unmount();

    const second = mount();
    await settle();
    expect(second.current().readOnly).toBe(true);
    expect(calls).toBe(1);
    second.unmount();
  });
});
