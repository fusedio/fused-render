// SPEC-quiet-notifications.md bug 2: `getJson`/`postJson` must attach
// `X-Fused-Source` automatically, with no per-call opt-in, because opting in
// is exactly what two producers (image/video, then text generation) forgot to
// do in live testing. See `ambientSourceHeaders`'s own comment in api.ts.
//
// Also covers saveAppFileToDisk's contract with the server-side export
// route: the caller's own display name must reach the server (a
// version-suffixed export must not collide with a live one at the same
// content hash), and a non-JSON failure body must not itself throw before
// the real error message (the status-based fallback) is produced.
import { afterEach, describe, expect, mock, test } from "bun:test";

import { installDomShim } from "@platform/lib/testDomShim";

// api.ts imports presence.ts -> router.ts, which reads `location` at module
// scope (see notifications.test.ts's own comment on this same trap) — the dom
// shim has to be installed before that import EVALUATES, not merely before
// this file's own statements run (static imports are evaluated before a
// module's own top-level code, regardless of where the `import` keyword sits
// in the file). `await import(...)` defers the import past `installDomShim()`.
installDomShim();

const { getJson, postJson, saveAppFileToDisk } = await import("@platform/lib/api");

function recordFetch(): {
  calls: { url: string; init: RequestInit }[];
  restore: () => void;
} {
  const calls: { url: string; init: RequestInit }[] = [];
  const real = globalThis.fetch;
  globalThis.fetch = ((url: string, init: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve({
      ok: true,
      json: async () => ({}),
    } as Response);
  }) as typeof fetch;
  return {
    calls,
    restore: () => {
      globalThis.fetch = real;
    },
  };
}

function headerValue(init: RequestInit, name: string): string | undefined {
  const headers = init.headers as Record<string, string> | undefined;
  return headers?.[name];
}

describe("api.ts ambient X-Fused-Source", () => {
  test("getJson attaches a non-empty X-Fused-Source with no explicit attribution", async () => {
    const rec = recordFetch();
    try {
      await getJson("/api/whatever");
      expect(rec.calls.length).toBe(1);
      const source = headerValue(rec.calls[0].init, "X-Fused-Source");
      expect(source).toBeTruthy();
    } finally {
      rec.restore();
    }
  });

  test("postJson attaches a non-empty X-Fused-Source with no explicit attribution", async () => {
    const rec = recordFetch();
    try {
      await postJson("/api/whatever", {});
      expect(rec.calls.length).toBe(1);
      const source = headerValue(rec.calls[0].init, "X-Fused-Source");
      expect(source).toBeTruthy();
    } finally {
      rec.restore();
    }
  });

  test("an explicit caller header still wins over the ambient default", async () => {
    const rec = recordFetch();
    try {
      await getJson("/api/whatever", {
        headers: { "X-Fused-Source": "explicit-attribution" },
      });
      expect(rec.calls.length).toBe(1);
      const source = headerValue(rec.calls[0].init, "X-Fused-Source");
      expect(source).toBe("explicit-attribution");
    } finally {
      rec.restore();
    }
  });

  test("postJson's explicit headers still win over the ambient default", async () => {
    const rec = recordFetch();
    try {
      await postJson(
        "/api/whatever",
        {},
        { headers: { "X-Fused-Source": "explicit-attribution" } },
      );
      expect(rec.calls.length).toBe(1);
      const source = headerValue(rec.calls[0].init, "X-Fused-Source");
      expect(source).toBe("explicit-attribution");
    } finally {
      rec.restore();
    }
  });
});

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
