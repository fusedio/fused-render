// The Git card (GitPeek; owner, 2026-11-10) stands only while the rail it
// stands beside has a repository — or may (its first read still out, a hop's
// next rail): the answer "no repository" closes it, and its door with it.
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { RailGitDoor } from "./rail-git-store";

const { GitPeek, GIT_FOLLOW_MS } = await import("./GitPeek");
const { publishCanvasRailPeek, resetCanvasRailPeekFlagForTests } = await import("./rail-peek-flag");
const { registerRail, resetFavPeekStoreForTests } = await import("./fav-peek-store");
const { publishRailGit, resetRailGitStoreForTests } = await import("./rail-git-store");
const { GIT_CARD_KEY, forgetGitCardRail, gitCardIntent, gitCardState, resetGitCardForTests, setGitCardRail } = await import(
  "./git-peek-store"
);
const { DoorCard } = await import("./DoorCard");
const { PEEK_OPEN_MS } = await import("./rail-peek-lib");

const door: RailGitDoor = {
  repo: { root: "/r", branch: "feature", has_remote: false, default_branch: null, ahead: null, behind: null, commits: [] },
  checking: false,
  checked: null,
  check: () => {},
};

/** What the card's refs see: enough of an element for the chassis. An
 *  earlier file in the run may have left its own body mock behind. */
const node = () => ({
  toggleAttribute() {},
  contains: () => false,
  focus() {},
  getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }),
});
beforeEach(() => {
  installPortalContainer();
  const body = (globalThis as unknown as { document: { body?: { createNodeMock?: unknown } } }).document.body;
  if (body) body.createNodeMock = node;
  resetCanvasRailPeekFlagForTests();
  resetFavPeekStoreForTests();
  resetRailGitStoreForTests();
  resetGitCardForTests();
});
afterEach(() => {
  removePortalContainer();
  resetCanvasRailPeekFlagForTests();
  resetFavPeekStoreForTests();
  resetRailGitStoreForTests();
  resetGitCardForTests();
});

const flush = () =>
  act(async () => {
    await new Promise((res) => setTimeout(res, 0));
  });

describe("GitPeek", () => {
  test("registered only while its rail has a repository or is reading; the answer 'none' closes a pinned card after the follow window", async () => {
    publishCanvasRailPeek({ enabled: true });
    const rail = { isConnected: true } as unknown as HTMLElement;
    registerRail(rail);
    let r!: ReactTestRenderer;
    await act(async () => {
      r = create(<GitPeek />);
    });
    await flush();
    expect(gitCardState().ready).toBe(false);
    act(() => publishRailGit(rail, door));
    expect(gitCardState().ready).toBe(true);
    act(() => gitCardIntent().press(GIT_CARD_KEY));
    expect(gitCardState()).toMatchObject({ up: true, pinned: true });
    // A path change: the rail reads again — the pinned card waits.
    act(() => publishRailGit(rail, "reading"));
    expect(gitCardState()).toMatchObject({ up: true, pinned: true, ready: true });
    // …and the answer is "no repository": a pinned card waits the follow
    // window (a hop's next rail may still come), then goes.
    act(() => publishRailGit(rail, null));
    expect(gitCardState()).toMatchObject({ up: true, pinned: true });
    await act(async () => {
      await new Promise((res) => setTimeout(res, GIT_FOLLOW_MS + 20));
    });
    expect(gitCardState()).toMatchObject({ up: false, ready: false });
    act(() => r.unmount());
  });

  test("beside the rail that opened it, not the top rail; that rail going: a preview closes, a pinned card falls back to the top", async () => {
    publishCanvasRailPeek({ enabled: true });
    const left = { isConnected: true } as unknown as HTMLElement;
    const right = { isConnected: true } as unknown as HTMLElement;
    registerRail(left);
    registerRail(right);
    publishRailGit(left, { ...door, repo: { ...door.repo, branch: "left" } });
    publishRailGit(right, { ...door, repo: { ...door.repo, branch: "right" } });
    let r!: ReactTestRenderer;
    await act(async () => {
      r = create(<GitPeek />);
    });
    await flush();
    const standsBy = () => r.root.findByType(DoorCard).props.rail;
    expect(standsBy()).toBe(right);
    // A hover's preview off the left rail's door.
    await act(async () => {
      setGitCardRail(left);
      gitCardIntent().enterRing(GIT_CARD_KEY, "pointer");
      await new Promise((res) => setTimeout(res, PEEK_OPEN_MS + 20));
    });
    expect(standsBy()).toBe(left);
    expect(gitCardState()).toMatchObject({ up: true, pinned: false });
    act(() => forgetGitCardRail(left));
    expect(gitCardState().up).toBe(false);
    expect(standsBy()).toBe(right);
    // Pinned off the left rail: it follows to the top rail when that one goes.
    act(() => {
      setGitCardRail(left);
      gitCardIntent().press(GIT_CARD_KEY);
    });
    expect(standsBy()).toBe(left);
    act(() => forgetGitCardRail(left));
    expect(gitCardState()).toMatchObject({ up: true, pinned: true });
    expect(standsBy()).toBe(right);
    act(() => r.unmount());
  });
});
