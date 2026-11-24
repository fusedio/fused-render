import { describe, expect, test } from "bun:test";
import { botMatch, defaultMode, modeFields, visibleWarnings, type BotFileAppResult, type BotFileBotMatch } from "./botfile";

const row = (folder: string, state: string, ok = false): BotFileAppResult => ({ folder, ok, state });

describe("visibleWarnings", () => {
  const first = ["settings: disk full", "playbook x: its name collapsed onto 'y' and replaced another"];

  test("a row still in settings keeps the first install's settings warning", () => {
    const rows = { a: row("a", "installed", true), b: row("b", "settings") };
    expect(visibleWarnings(first, true, rows, { a: [] })).toEqual(first);
  });

  test("every settings row retried drops it and keeps the rest", () => {
    const rows = { a: row("a", "installed", true), b: row("b", "installed", true) };
    expect(visibleWarnings(first, true, rows, { a: [], b: ["skill z: late"] })).toEqual([first[1], "skill z: late"]);
  });

  test("a settings warning that flipped no row (the bot's own settings step) stays", () => {
    const rows = { a: row("a", "installed", true) };
    expect(visibleWarnings(first, false, rows, {})).toEqual(first);
  });
});

const existing = { id: "a1b2c3d4", name: "Scout", face: null };
const match = (state: BotFileBotMatch["state"], edited_here = false): BotFileBotMatch => ({ state, existing, edited_here });

describe("botMatch", () => {
  test("an older server's probe without a bot block reads as new", () => {
    expect(botMatch({})).toEqual({ state: "new", existing: null, edited_here: false });
  });

  test("a match passes through", () => {
    expect(botMatch({ bot: match("diverged", true) })).toEqual(match("diverged", true));
  });

  test("a match with no existing bot to target, or an unknown state, reads as new", () => {
    expect(botMatch({ bot: { state: "update", existing: null, edited_here: false } }).state).toBe("new");
    expect(botMatch({ bot: { ...match("update"), state: "later" as never } }).state).toBe("new");
  });
});

describe("defaultMode", () => {
  test("same and update default to update; diverged and new to a new bot", () => {
    expect(defaultMode(match("same"))).toBe("update");
    expect(defaultMode(match("update"))).toBe("update");
    expect(defaultMode(match("diverged", true))).toBe("new");
    expect(defaultMode(botMatch({}))).toBe("new");
  });
});

describe("modeFields", () => {
  test("update targets the existing bot", () => {
    expect(modeFields("update", match("diverged", true))).toEqual({ mode: "update", target: "a1b2c3d4" });
  });

  test("new never sends a target", () => {
    expect(modeFields("new", match("update"))).toEqual({ mode: "new" });
  });

  test("update without a match falls back to new", () => {
    expect(modeFields("update", botMatch({}))).toEqual({ mode: "new" });
  });
});
