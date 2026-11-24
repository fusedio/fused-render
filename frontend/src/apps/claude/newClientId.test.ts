import { afterEach, expect, test } from "bun:test";
import { newClientId } from "./ClaudeChat";

const CANON = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const realCrypto = Object.getOwnPropertyDescriptor(globalThis, "crypto");

afterEach(() => {
  if (realCrypto) Object.defineProperty(globalThis, "crypto", realCrypto);
});

function withCrypto(value: unknown): void {
  Object.defineProperty(globalThis, "crypto", { value, configurable: true, writable: true });
}

test("without randomUUID the id is still a canonical v4 uuid (getRandomValues)", () => {
  withCrypto({ getRandomValues: (a: Uint8Array) => a.fill(0xab) });
  expect(newClientId()).toMatch(CANON);
});

test("without crypto at all the id is still a canonical v4 uuid", () => {
  withCrypto(undefined);
  const ids = new Set(Array.from({ length: 20 }, () => newClientId()));
  expect(ids.size).toBe(20);
  for (const id of ids) expect(id).toMatch(CANON);
});
