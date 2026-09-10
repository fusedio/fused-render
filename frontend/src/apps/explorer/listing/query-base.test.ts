import { describe, expect, test } from "bun:test";
import { escapesBase } from "@apps/explorer/listing/query-base";

describe("escapesBase", () => {
  test.each([
    ["*/*.json"],
    ["data/2024"],
    [".csv"],
    ["**/*.csv"],
    ["readme"],
    ["..config"],
    ["a..b/c"],
  ])("%s does not escape the box root", (query) => {
    expect(escapesBase(query)).toBe(false);
  });

  test.each([
    ["~"],
    ["~/Work"],
    ["~/a/*/b.csv"],
    ["/tmp"],
    ["/tmp/abc.txt"],
    ["C:/x"],
    ["C:\\x"],
    ["../sibling/*.json"],
  ])("%s escapes the box root", (query) => {
    expect(escapesBase(query)).toBe(true);
  });
});
