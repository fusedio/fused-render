// SPEC-quiet-notifications.md bug 2: `getJson`/`postJson` must attach
// `X-Fused-Source` automatically, with no per-call opt-in, because opting in
// is exactly what two producers (image/video, then text generation) forgot to
// do in live testing. See `ambientSourceHeaders`'s own comment in api.ts.
import { describe, expect, test } from "bun:test";

import { installDomShim } from "@platform/lib/testDomShim";

// api.ts imports presence.ts -> router.ts, which reads `location` at module
// scope (see notifications.test.ts's own comment on this same trap) — the dom
// shim has to be installed before that import EVALUATES, not merely before
// this file's own statements run (static imports are evaluated before a
// module's own top-level code, regardless of where the `import` keyword sits
// in the file). `await import(...)` defers the import past `installDomShim()`.
installDomShim();

const { getJson, postJson } = await import("@platform/lib/api");

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
