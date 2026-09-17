// saveAppFileToDisk's contract with the server-side export route: the
// caller's own display name must reach the server (a version-suffixed export
// must not collide with a live one at the same content hash), and a
// non-JSON failure body must not itself throw before the real error message
// (the status-based fallback) is produced.
import { afterEach, expect, mock, test } from "bun:test";

import { saveAppFileToDisk } from "./api";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

test("the caller's display name is sent as its own form field, not folded into path", async () => {
  let sentForm: FormData | undefined;
  globalThis.fetch = mock(async (_url: string, init?: RequestInit) => {
    sentForm = init?.body as FormData;
    return new Response(JSON.stringify({ path: "/home/x/Downloads/myapp-v7.fused" }), {
      status: 200,
    });
  }) as unknown as typeof fetch;

  const result = await saveAppFileToDisk("/apps/myapp", "myapp-v7");
  expect(result).toBe("/home/x/Downloads/myapp-v7.fused");
  expect(sentForm?.get("path")).toBe("/apps/myapp");
  expect(sentForm?.get("name")).toBe("myapp-v7");
});

test("a non-JSON failure body still surfaces the status-based message", async () => {
  globalThis.fetch = mock(async () => new Response("Internal Server Error", { status: 500 })) as unknown as typeof fetch;

  await expect(saveAppFileToDisk("/apps/myapp", "myapp")).rejects.toThrow("export failed (500)");
});

test("a JSON failure body's own error message wins over the status fallback", async () => {
  globalThis.fetch = mock(
    async () => new Response(JSON.stringify({ error: "not a fused app" }), { status: 400 }),
  ) as unknown as typeof fetch;

  await expect(saveAppFileToDisk("/apps/myapp", "myapp")).rejects.toThrow("not a fused app");
});
