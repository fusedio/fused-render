// The Git card off the rail's Git door (owner, 2026-11-10): the Bots card's
// store on its own key — hover previews, a press pins, a press on the pinned
// door closes — and with no card to stand the press opens the page's Git
// panel (the door has no page of its own). The rail's facts are per rail.
import { beforeEach, describe, expect, it } from "bun:test";
import { PEEK_CLOSE_MS, PEEK_OPEN_MS, type Timers } from "./rail-peek-lib";
import {
  GIT_CARD_KEY,
  GIT_PEEK_ID,
  closeGitCard,
  gitCardIntent,
  gitCardState,
  gitDoorAction,
  railGitDoorEl,
  registerGitCard,
  registerGitDoor,
  resetGitCardForTests,
  setGitCardRoom,
} from "./git-peek-store";
import { BOTS_CARD_KEY, BOTS_PEEK_ID, botsCardIntent, botsCardState, resetBotsCardForTests } from "./bots-peek-store";
import { publishRailGit, railGitCount, railGitMaybe, railGitOf, resetRailGitStoreForTests, type RailGitDoor } from "./rail-git-store";

class Clock implements Timers {
  now = 0;
  private id = 0;
  private timers = new Map<number, { at: number; fn: () => void }>();
  set(fn: () => void, ms: number): number {
    const id = ++this.id;
    this.timers.set(id, { at: this.now + ms, fn });
    return id;
  }
  clear(id: number): void {
    this.timers.delete(id);
  }
  tick(ms: number): void {
    const end = this.now + ms;
    for (;;) {
      const next = [...this.timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      this.timers.delete(next[0]);
      this.now = next[1].at;
      next[1].fn();
    }
    this.now = end;
  }
}

let clock: Clock;
beforeEach(() => {
  clock = new Clock();
  resetGitCardForTests(clock);
  resetBotsCardForTests(clock);
  resetRailGitStoreForTests();
});

describe("gitDoorAction", () => {
  it("no card: the page's Git panel; else a toggle (a button: no modified press of its own)", () => {
    expect(gitDoorAction({ ready: false, pinned: false })).toBe("open");
    expect(gitDoorAction({ ready: true, pinned: false })).toBe("pin");
    expect(gitDoorAction({ ready: true, pinned: true })).toBe("close");
  });
});

describe("the Git card's intent", () => {
  it("a hover previews after window A's dwell and goes on the grace; a press pins; the close unpins", () => {
    const i = gitCardIntent();
    i.enterRing(GIT_CARD_KEY);
    clock.tick(PEEK_OPEN_MS);
    expect(gitCardState()).toMatchObject({ up: true, pinned: false, seq: 1 });
    i.leaveRing();
    clock.tick(PEEK_CLOSE_MS);
    expect(gitCardState().up).toBe(false);
    i.press(GIT_CARD_KEY);
    expect(gitCardState()).toMatchObject({ up: true, pinned: true, seq: 2 });
    closeGitCard();
    expect(gitCardState()).toMatchObject({ up: false, pinned: false });
  });
  it("is its own store: the Bots card's press leaves it alone, and the ids and keys differ", () => {
    botsCardIntent().press(BOTS_CARD_KEY);
    expect(botsCardState().up).toBe(true);
    expect(gitCardState().up).toBe(false);
    expect(GIT_CARD_KEY).not.toBe(BOTS_CARD_KEY);
    expect(GIT_PEEK_ID).not.toBe(BOTS_PEEK_ID);
  });
  it("ready only with a card mounted and room; doors by rail", () => {
    expect(gitCardState().ready).toBe(false);
    const off = registerGitCard();
    expect(gitCardState().ready).toBe(true);
    setGitCardRoom(false);
    expect(gitCardState().ready).toBe(false);
    setGitCardRoom(true);
    off();
    expect(gitCardState().ready).toBe(false);
    const rail = {} as HTMLElement;
    const door = {} as HTMLElement;
    const offDoor = registerGitDoor(rail, door);
    expect(railGitDoorEl(rail)).toBe(door);
    offDoor();
    expect(railGitDoorEl(rail)).toBeNull();
  });
});

describe("rail-git-store: per rail (an explorer split is two rails on two repositories)", () => {
  const door = (branch: string): RailGitDoor => ({
    repo: { root: "/r", branch, has_remote: false, default_branch: null, ahead: null, behind: null, commits: [] },
    checking: false,
    checked: null,
    check: () => {},
  });
  it("each rail's own facts; reading is a maybe, not a repository; null drops the entry", () => {
    const a = {} as HTMLElement;
    const b = {} as HTMLElement;
    publishRailGit(a, door("left"));
    publishRailGit(b, "reading");
    expect(railGitOf(a)?.repo.branch).toBe("left");
    expect(railGitOf(b)).toBeNull();
    expect(railGitMaybe(b)).toBe(true);
    publishRailGit(b, door("right"));
    expect(railGitOf(b)?.repo.branch).toBe("right");
    expect(railGitCount()).toBe(2);
    publishRailGit(a, null);
    expect(railGitOf(a)).toBeNull();
    expect(railGitMaybe(a)).toBe(false);
    expect(railGitOf(null)).toBeNull();
    expect(railGitCount()).toBe(1);
  });
});
