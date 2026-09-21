// The two-button share sheet for a plain file — sibling of ShareAppModal's
// own (nonexistent) coverage.
//
// This drives `useShareFile` directly through a Probe component (the same
// harness share-file.test.ts's openShareFile/useShareFileRequest test uses),
// rather than mounting `<ShareFileModal>` itself. `<ShareFileModal>`'s chrome
// is `@platform/shadcn/ui/dialog`'s Base UI `Dialog`/`AlertDialog`, which
// portals through `FloatingPortal` — a real `ReactDOM.createPortal` call
// that `react-test-renderer`'s mock tree cannot satisfy ("Target container is
// not a DOM element", confirmed while building this file). That is not a
// harness gap specific to this component: `apps/claude/ui/receipt-door.test.tsx`
// is the only other test in the codebase touching `shadcn/ui/dialog`, and it
// asserts its OWN component's source does *not* import it, using the shared
// modal chassis instead for exactly this reason. ShareAppModal.tsx — this
// component's own reference for the sheet's shape — carries no test either.
// Testing the hook head-on exercises every real decision (the two-action
// gate, the confirm-before-stop, the upload fallback, the mode/expiry
// wiring) without depending on a portal the harness cannot host; the actual
// JSX in ShareFileModal.tsx is a thin, direct reading of this same state
// (see its `phase` switch), kept in visual parity with ShareAppModal.tsx.
//
// Mocked at the `fetch` layer, not via `mock.module` on @platform/lib/api or
// @platform/lib/share-file: DownloadManager.test.tsx's own header records
// that `mock.module` replaces a module for the WHOLE bun process, not just
// this file, and contaminated an unrelated suite the first time it was tried
// here — confirmed again while building this file (share-file.test.ts's own
// fetch-level mocks broke when a mock.module in this file ran first in the
// same process). A route-keyed fetch stub sidesteps that entirely.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create } from "react-test-renderer";
import type { ShareableFile } from "@platform/lib/share-file";
import type { ShareFileHookState } from "@platform/ui/ShareFileModal";

// A dynamic import, not a static one: static `import` specifiers are hoisted
// above every other top-level statement (including the installDomShim() call
// above), so ShareFileModal.tsx — and the router.ts side effect it pulls in
// transitively, which reads `location` at module-init time — would load
// before the shim installs `location`. share-file.test.ts hits the same
// trap and dodges it the same way.
const { PRIMARY_ACTIONS, useShareFile } = await import("@platform/ui/ShareFileModal");

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

const FILE: ShareableFile = { path: "/data/demo.parquet", name: "demo.parquet" };

const STATUS_BASE = {
  file_id: "demo_abc123",
  can_share: true,
  refusal: null,
  viewer: "Parquet",
  cli_found: true,
  logged_in: true,
  creds_stamp: 1,
  shared: null,
};

const SHARED_PUBLIC = {
  file_id: "demo_abc123",
  path: FILE.path,
  name: FILE.name,
  viewer: "Parquet",
  url: "https://udf.fused.ai/tok/demo.html",
  canvas_id: "c1",
  canvas_name: "demo_abc123",
  share_token: "tok",
  slug: "demo_abc123",
  remote: "fd://h/x/demo.parquet",
  workbench_url: "https://x",
  mode: "public" as const,
  session_token: null,
  session_expires: null,
  shared_at: 1,
  updated_at: 1,
  expired: false,
};

/** A route-keyed fetch stub: each entry matches a URL PREFIX and returns a
 *  JSON body (or a function of the parsed request body, for POSTs whose
 *  response depends on what was sent). Falls through to a 404 for anything
 *  unlisted, so a request nobody described fails loudly rather than hanging. */
function stubFetch(routes: Record<string, unknown | ((body: unknown) => unknown)>) {
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    const path = url.split("?")[0]!;
    const entry = routes[path];
    if (entry === undefined) return new Response(JSON.stringify({ error: "unmocked" }), { status: 404 });
    const body =
      typeof entry === "function"
        ? (entry as (b: unknown) => unknown)(init?.body ? JSON.parse(init.body as string) : {})
        : entry;
    if (body && typeof body === "object" && "__status" in (body as Record<string, unknown>)) {
      const { __status, ...rest } = body as { __status: number } & Record<string, unknown>;
      return new Response(JSON.stringify(rest), { status: __status });
    }
    return new Response(JSON.stringify(body), { status: 200 });
  }) as unknown as typeof fetch;
}

/** Mounts `useShareFile(file)` through a Probe and hands back a live-updating
 *  getter, mirroring share-file.test.ts's own store test. */
async function mountHook(file: ShareableFile) {
  let latest!: ShareFileHookState;
  const Probe = (): null => {
    latest = useShareFile(file);
    return null;
  };
  let renderer!: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(createElement(Probe));
  });
  return {
    get state() {
      return latest;
    },
    unmount: () => act(() => renderer.unmount()),
  };
}

test("nothing shared yet: exactly two primary actions, public and 30-minute, no third mode", async () => {
  stubFetch({ "/api/share/file/status": STATUS_BASE });
  const h = await mountHook(FILE);
  expect(PRIMARY_ACTIONS.map((a) => a.mode)).toEqual(["public", "temporary"]);
  expect(PRIMARY_ACTIONS.length).toBe(2);
  expect(h.state.phase).toBe("share");
  expect(h.state.shared).toBeNull();
  await h.unmount();
});

test("once shared, the record carries a url, and Stop sharing (requestStop) is available", async () => {
  stubFetch({ "/api/share/file/status": { ...STATUS_BASE, shared: SHARED_PUBLIC } });
  const h = await mountHook(FILE);
  expect(h.state.phase).toBe("shared");
  expect(h.state.shared?.url).toBe("https://udf.fused.ai/tok/demo.html");
  await h.unmount();
});

test("a temporary share reports its mode and expired flag distinctly from public", async () => {
  stubFetch({
    "/api/share/file/status": {
      ...STATUS_BASE,
      shared: { ...SHARED_PUBLIC, mode: "temporary", session_token: "sess-1" },
    },
  });
  const h = await mountHook(FILE);
  expect(h.state.shared?.mode).toBe("temporary");
  expect(h.state.shared?.expired).toBe(false);
  await h.unmount();
});

test("stop sharing is gated behind a confirmation: requestStop alone never calls /remove", async () => {
  let removeCalled = false;
  stubFetch({
    "/api/share/file/status": { ...STATUS_BASE, shared: SHARED_PUBLIC },
    "/api/share/file/remove": () => {
      removeCalled = true;
      return { ok: true, deleted_canvas: true };
    },
  });
  const h = await mountHook(FILE);
  expect(h.state.phase).toBe("shared");

  act(() => h.state.requestStop());
  expect(h.state.confirmStop).toBe(true);
  expect(removeCalled).toBe(false);

  await act(async () => {
    h.state.confirmStopNow();
  });
  expect(removeCalled).toBe(true);
  expect(h.state.confirmStop).toBe(false);
  expect(h.state.shared).toBeNull();
  await h.unmount();
});

test("cancelStopRequest backs out of the confirmation without calling /remove", async () => {
  let removeCalled = false;
  stubFetch({
    "/api/share/file/status": { ...STATUS_BASE, shared: SHARED_PUBLIC },
    "/api/share/file/remove": () => {
      removeCalled = true;
      return { ok: true, deleted_canvas: true };
    },
  });
  const h = await mountHook(FILE);
  act(() => h.state.requestStop());
  expect(h.state.confirmStop).toBe(true);
  act(() => h.state.cancelStopRequest());
  expect(h.state.confirmStop).toBe(false);
  expect(removeCalled).toBe(false);
  expect(h.state.shared).not.toBeNull();
  await h.unmount();
});

test("a file over the inline cap falls back to the detached upload, then publishes once it finishes", async () => {
  let publishCalls = 0;
  let cancelCalled = false;
  stubFetch({
    "/api/share/file/status": STATUS_BASE,
    "/api/share/file/publish": (body: unknown) => {
      publishCalls += 1;
      const b = body as { upload_id?: string };
      if (!b.upload_id) {
        return {
          __status: 409,
          error: "the upload has not finished (state: none); call /api/share/file/upload first",
        };
      }
      return { ok: true, shared: { ...SHARED_PUBLIC, mode: "public" } };
    },
    "/api/share/file/upload": { id: "demo_abc123", state: "done", bytes: 999999999 },
    "/api/share/file/upload/cancel": () => {
      cancelCalled = true;
      return { id: "demo_abc123", state: "cancelled" };
    },
  });
  const h = await mountHook(FILE);
  expect(h.state.phase).toBe("share");

  await act(async () => {
    h.state.share("public");
    // let the publish -> 409 -> /upload -> (state already "done") -> finishPublish chain settle
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
  });

  expect(publishCalls).toBe(2);
  expect(h.state.shared).not.toBeNull();
  expect(cancelCalled).toBe(false);
  await h.unmount();
});

test("a still-running detached upload shows the uploading phase, and cancelUploadNow calls /upload/cancel", async () => {
  let cancelCalled = false;
  stubFetch({
    "/api/share/file/status": STATUS_BASE,
    "/api/share/file/publish": {
      __status: 409,
      error: "the upload has not finished (state: none); call /api/share/file/upload first",
    },
    "/api/share/file/upload": { id: "demo_abc123", state: "running", bytes: 999999999 },
    "/api/share/file/upload/status": { id: "demo_abc123", state: "running", bytes: 999999999 },
    "/api/share/file/upload/cancel": () => {
      cancelCalled = true;
      return { id: "demo_abc123", state: "cancelled" };
    },
  });
  const h = await mountHook(FILE);

  await act(async () => {
    h.state.share("public");
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
  });

  expect(h.state.phase).toBe("uploading");
  expect(h.state.upload?.state).toBe("running");

  act(() => h.state.cancelUploadNow());
  expect(cancelCalled).toBe(true);
  expect(h.state.upload).toBeNull();
  expect(h.state.busy).toBeNull();
  await h.unmount();
});
