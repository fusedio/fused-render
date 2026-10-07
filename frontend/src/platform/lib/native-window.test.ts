import { afterEach, describe, expect, test } from "bun:test";
import { openAppWindow } from "./native-window";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function stub(body: unknown, status = 200) {
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    })) as unknown as typeof fetch;
}

describe("openAppWindow", () => {
  test("true when the server opened a window", async () => {
    stub({ ok: true });
    expect(await openAppWindow("/a")).toBe(true);
  });
  test("false when apps_open_in_home says the app opens in this window", async () => {
    stub({ ok: true, in_home: true });
    expect(await openAppWindow("/a")).toBe(false);
  });
  test("false when the request fails", async () => {
    stub({ error: "nope" }, 404);
    expect(await openAppWindow("/a")).toBe(false);
  });
});
