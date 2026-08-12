import { describe, expect, it } from "bun:test";
import { startRace, type Source } from "@apps/explorer/listing/source-race";

function race() {
  const cancelled: Source[] = [];
  return { cancelled, r: startRace((loser) => cancelled.push(loser)) };
}

describe("startRace", () => {
  it("lets the first source to produce publish, and cancels the other", () => {
    const { cancelled, r } = race();
    expect(r.claim("index")).toBe(true);
    expect(cancelled).toEqual(["walk"]);
    expect(r.winner()).toBe("index");
  });

  it("refuses the loser, however late it produces", () => {
    const { r } = race();
    r.claim("walk");
    expect(r.claim("index")).toBe(false);
    expect(r.claim("index")).toBe(false);
  });

  it("lets the winner keep publishing — a stream claims on every flush", () => {
    const { cancelled, r } = race();
    expect(r.claim("walk")).toBe(true);
    expect(r.claim("walk")).toBe(true);
    expect(r.claim("walk")).toBe(true);
    // Cancelled once, not once per batch.
    expect(cancelled).toEqual(["index"]);
  });

  it("is unclaimed until someone produces", () => {
    const { cancelled, r } = race();
    expect(r.claimed()).toBe(false);
    expect(r.winner()).toBeNull();
    expect(cancelled).toEqual([]);
  });
});
