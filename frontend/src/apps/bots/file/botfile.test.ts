import { describe, expect, test } from "bun:test";
import { visibleWarnings, type BotFileAppResult } from "./botfile";

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
