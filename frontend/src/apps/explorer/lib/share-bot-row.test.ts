import { describe, expect, test } from "bun:test";
import { shareBotRow } from "./share-bot-row";

describe("shareBotRow", () => {
  test("one row on a file and on a folder where the server has bots", () => {
    for (const isDir of [false, true]) {
      const rows = shareBotRow({ botsAvailable: true, isDir, onClick: () => {} });
      expect(rows.length).toBe(1);
      expect((rows[0] as { label: string }).label).toBe("Share with bot…");
    }
  });
  test("absent where there are no bots", () => {
    expect(shareBotRow({ botsAvailable: false, isDir: false, onClick: () => {} })).toEqual([]);
  });
});
