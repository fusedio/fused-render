// Client half of fused_render/share_file.py — modeled on share-app.test's
// absence (there isn't one) and api.test.ts's fetch-mocking pattern. Covers
// the two-mode publish call, the not-logged-in auth-code fold (share-app.ts's
// own trick, reused verbatim), the detached-upload trio, and the
// openShareFile/closeShareFile/useShareFileRequest store.
import { afterEach, expect, mock, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

const {
  cancelUpload,
  closeShareFile,
  getShareFileStatus,
  lookupShareFile,
  openShareFile,
  publishShareFile,
  removeShareFile,
  startUpload,
  uploadStatus,
  useShareFileRequest,
} = await import("@platform/lib/share-file");

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function mockJson(status: number, body: unknown) {
  globalThis.fetch = mock(async () => new Response(JSON.stringify(body), { status })) as unknown as
    typeof fetch;
}

test("getShareFileStatus GETs the status route with the path as a query param", async () => {
  let seenUrl = "";
  globalThis.fetch = mock(async (url: string) => {
    seenUrl = url;
    return new Response(JSON.stringify({ file_id: "demo_abc123", can_share: true }), {
      status: 200,
    });
  }) as unknown as typeof fetch;
  const status = await getShareFileStatus("/data/demo.parquet");
  expect(seenUrl).toBe("/api/share/file/status?path=" + encodeURIComponent("/data/demo.parquet"));
  expect(status.file_id).toBe("demo_abc123");
});

test("publishShareFile posts the path and mode as JSON, defaulting mode to public", async () => {
  let sentBody: unknown;
  globalThis.fetch = mock(async (_url: string, init?: RequestInit) => {
    sentBody = JSON.parse(init!.body as string);
    return new Response(JSON.stringify({ ok: true, shared: { mode: "public" } }), { status: 200 });
  }) as unknown as typeof fetch;
  await publishShareFile("/data/demo.parquet");
  expect(sentBody).toEqual({ path: "/data/demo.parquet", mode: "public" });
});

test("publishShareFile carries an explicit mode and upload_id through", async () => {
  let sentBody: unknown;
  globalThis.fetch = mock(async (_url: string, init?: RequestInit) => {
    sentBody = JSON.parse(init!.body as string);
    return new Response(JSON.stringify({ ok: true, shared: { mode: "temporary" } }), {
      status: 200,
    });
  }) as unknown as typeof fetch;
  await publishShareFile("/data/demo.parquet", "temporary", "demo_abc123");
  expect(sentBody).toEqual({
    path: "/data/demo.parquet",
    mode: "temporary",
    upload_id: "demo_abc123",
  });
});

test("a 409 asking for the upload route first is surfaced with its own code", async () => {
  mockJson(409, {
    error: "the upload has not finished (state: none); call /api/share/file/upload first",
  });
  const err = await publishShareFile("/data/big.parquet").catch((e) => e);
  expect(err.message).toContain("call /api/share/file/upload first");
  expect(err.code).toBe("upload_required");
});

test("a 401 or a not-signed-in 409 both fold onto not_logged_in, the same as share-app.ts", async () => {
  mockJson(401, { error: "token refused" });
  const unauthorized = await publishShareFile("/data/demo.parquet").catch((e) => e);
  expect(unauthorized.code).toBe("not_logged_in");

  mockJson(409, { error: "not signed in to Fused" });
  const notSignedIn = await lookupShareFile("/data/demo.parquet").catch((e) => e);
  expect(notSignedIn.code).toBe("not_logged_in");
});

test("removeShareFile posts to /remove with just the path", async () => {
  let sentUrl = "";
  let sentBody: unknown;
  globalThis.fetch = mock(async (url: string, init?: RequestInit) => {
    sentUrl = url;
    sentBody = JSON.parse(init!.body as string);
    return new Response(JSON.stringify({ ok: true, deleted_canvas: true }), { status: 200 });
  }) as unknown as typeof fetch;
  const res = await removeShareFile("/data/demo.parquet");
  expect(sentUrl).toBe("/api/share/file/remove");
  expect(sentBody).toEqual({ path: "/data/demo.parquet" });
  expect(res.deleted_canvas).toBe(true);
});

test("startUpload posts the path to /upload", async () => {
  let sentBody: unknown;
  globalThis.fetch = mock(async (_url: string, init?: RequestInit) => {
    sentBody = JSON.parse(init!.body as string);
    return new Response(JSON.stringify({ id: "demo_abc123", state: "running" }), { status: 200 });
  }) as unknown as typeof fetch;
  const state = await startUpload("/data/big.parquet");
  expect(sentBody).toEqual({ path: "/data/big.parquet" });
  expect(state.state).toBe("running");
});

test("uploadStatus GETs /upload/status with the id as a query param", async () => {
  let seenUrl = "";
  globalThis.fetch = mock(async (url: string) => {
    seenUrl = url;
    return new Response(JSON.stringify({ id: "demo_abc123", state: "done" }), { status: 200 });
  }) as unknown as typeof fetch;
  const state = await uploadStatus("demo_abc123");
  expect(seenUrl).toBe("/api/share/file/upload/status?id=demo_abc123");
  expect(state.state).toBe("done");
});

test("cancelUpload posts the id to /upload/cancel", async () => {
  let sentBody: unknown;
  globalThis.fetch = mock(async (_url: string, init?: RequestInit) => {
    sentBody = JSON.parse(init!.body as string);
    return new Response(JSON.stringify({ id: "demo_abc123", state: "cancelled" }), {
      status: 200,
    });
  }) as unknown as typeof fetch;
  const state = await cancelUpload("demo_abc123");
  expect(sentBody).toEqual({ id: "demo_abc123" });
  expect(state.state).toBe("cancelled");
});

// -- the open-request store, mirrored from share-app.ts's own tests-by-absence —
// exercised directly through the hook rather than a component, same as
// drafts.test.ts's renderAutosave harness.
test("openShareFile/closeShareFile drive useShareFileRequest, bumping seq per open", async () => {
  const { createElement } = await import("react");
  const { act, create } = await import("react-test-renderer");
  // A boxed holder, not a bare `let`: bun-types' `toBeNull()` assertion
  // narrows a directly-tracked variable to `null` for the rest of the test
  // (TS does not see the reassignment inside `Probe`, called only via
  // `act`), so every read after the first `toBeNull()` check sees `never`.
  // Reading through `.current` sidesteps that narrowing entirely.
  const box: { current: ReturnType<typeof useShareFileRequest> } = { current: null };
  const Probe = (): null => {
    box.current = useShareFileRequest();
    return null;
  };
  let renderer!: ReturnType<typeof create>;
  act(() => {
    renderer = create(createElement(Probe));
  });
  expect(box.current).toBeNull();

  act(() => {
    openShareFile({ path: "/data/demo.parquet", name: "demo.parquet" });
  });
  expect(box.current?.file.path).toBe("/data/demo.parquet");
  const firstSeq = box.current?.seq;

  act(() => {
    openShareFile({ path: "/data/demo.parquet", name: "demo.parquet" });
  });
  expect(box.current?.seq).not.toBe(firstSeq);

  act(() => {
    closeShareFile();
  });
  expect(box.current).toBeNull();
  act(() => renderer.unmount());
});
