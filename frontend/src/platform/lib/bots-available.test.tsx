import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

const { readBotsMissing, resetConfigReadForTests, setBotsAvailableForTests, setLauncherAvailableForTests, useBotsAvailable, useBotsMissing, useLauncherAvailable } = await import("./bots-available");

function Probe({ out }: { out: { bots?: boolean; launcher?: boolean } }) {
  out.bots = useBotsAvailable(true);
  out.launcher = useLauncherAvailable(true);
  return null;
}

function mountOnce(out: { bots?: boolean; launcher?: boolean }): void {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Probe out={out} />);
  });
  act(() => r.unmount());
}

afterEach(() => {
  setBotsAvailableForTests(null);
  setLauncherAvailableForTests(null);
});

describe("the shared config flags' test pins (CodeRabbit on #2201)", () => {
  test("one flag's pin leaves the other's alone, and letting it go does not leave a stale value behind", () => {
    setBotsAvailableForTests(false);
    setLauncherAvailableForTests(true);
    const out: { bots?: boolean; launcher?: boolean } = {};
    mountOnce(out);
    expect(out).toEqual({ bots: false, launcher: true });
    // Bots let go while the launcher stays pinned: no server answer here, so
    // Bots reads the absent flag's default (on), not the old pinned false.
    setBotsAvailableForTests(null);
    mountOnce(out);
    expect(out).toEqual({ bots: true, launcher: true });
  });
});

// A SERVER WITHOUT BOTS (Windows) still opens the Bots page for the experts alone: `useBotsMissing` /
// `readBotsMissing` are what the page reads to leave its `/api/bots*` parts out.
function MissingProbe({ out }: { out: { missing?: boolean } }) {
  out.missing = useBotsMissing(true);
  return null;
}

describe("bots missing (the experts-only Bots page)", () => {
  test("only an explicit bots_available false is missing, in React and out of it", async () => {
    const out: { missing?: boolean } = {};
    setBotsAvailableForTests(false);
    let r!: ReturnType<typeof create>;
    act(() => { r = create(<MissingProbe out={out} />); });
    act(() => r.unmount());
    expect(out.missing).toBe(true);
    expect(await readBotsMissing()).toBe(true);
    setBotsAvailableForTests(true);
    act(() => { r = create(<MissingProbe out={out} />); });
    act(() => r.unmount());
    expect(out.missing).toBe(false);
    expect(await readBotsMissing()).toBe(false);
  });

  test("a config read that fails is NOT missing (the page polls bots as before), and never waits for the retry", async () => {
    const orig = globalThis.fetch;
    globalThis.fetch = (() => Promise.reject(new Error("down"))) as unknown as typeof fetch;
    try {
      expect(await readBotsMissing()).toBe(false);
    } finally {
      globalThis.fetch = orig;
    }
  });

  test("the server's own answer: bots_available false reads as missing", async () => {
    const orig = globalThis.fetch;
    globalThis.fetch = (() =>
      Promise.resolve(new Response(JSON.stringify({ bots_available: false }), { status: 200, headers: { "Content-Type": "application/json" } }))) as unknown as typeof fetch;
    try {
      expect(await readBotsMissing()).toBe(true);
    } finally {
      globalThis.fetch = orig;
      // The answer stays in the module once read, and the module is shared with every later suite in the run.
      resetConfigReadForTests();
    }
  });
});
