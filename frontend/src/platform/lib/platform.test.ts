import { expect, test } from "bun:test";

import { matchesLinux, matchesWindows } from "./platform";

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

test("matchesLinux recognises desktop Linux but not Android or macOS", () => {
  expect(matchesLinux("Linux x86_64")).toBe(true);
  expect(matchesLinux("Linux")).toBe(true);
  expect(matchesLinux("Linux armv8l", "Mozilla/5.0 (Linux; Android 14)")).toBe(false);
  expect(matchesLinux("MacIntel")).toBe(false);
  expect(matchesLinux("Win32")).toBe(false);
});
