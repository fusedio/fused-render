import { describe, expect, test } from "bun:test";
import { shareBotRow } from "./share-bot-row";

describe("shareBotRow", () => {
  test("one row on a file where the server has bots", () => {
    const rows = shareBotRow({ botsAvailable: true, isDir: false, onClick: () => {} });
    expect(rows.length).toBe(1);
    expect((rows[0] as { label: string }).label).toBe("Share with bot…");
  });
  test("absent on a directory and where there are no bots", () => {
    expect(shareBotRow({ botsAvailable: true, isDir: true, onClick: () => {} })).toEqual([]);
    expect(shareBotRow({ botsAvailable: false, isDir: false, onClick: () => {} })).toEqual([]);
  });
});
