import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

const { setBotsAvailableForTests, setLauncherAvailableForTests, useBotsAvailable, useLauncherAvailable } = await import("./bots-available");

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
