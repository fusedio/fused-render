import { expect, test } from "bun:test";

import { matchesWindows } from "./platform";

test("matchesWindows recognises Windows platform strings", () => {
  expect(matchesWindows("Win32")).toBe(true);
  expect(matchesWindows("Windows")).toBe(true);
  expect(matchesWindows("Mozilla/5.0 (Windows NT 10.0; Win64; x64)")).toBe(true);
});

test("matchesWindows does not mistake darwin for Windows", () => {
  expect(matchesWindows("darwin")).toBe(false);
  expect(matchesWindows("MacIntel")).toBe(false);
  expect(matchesWindows("Linux x86_64")).toBe(false);
});
