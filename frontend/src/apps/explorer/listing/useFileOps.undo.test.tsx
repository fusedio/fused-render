// Regression coverage for the retention-narrowing reversal (user: "don't
// keep this in the list. just show popup. anything non actionable or error
// doesn't belong in the list" — see DECISIONS-toasts-become-notifications.md).
//
// useFileOps.ts's undo/redo confirmation dropped its `tier: "trail"`
// override on the success branch (relocationToast's "Undid/Redid the
// <kind>." message) — that message now only pops. The FAILURE branch was
// left untouched on the theory that `tone: "error"` already promotes to
// `attention` regardless of tier, with no explicit override needed — this
// test proves that theory rather than assuming it, by driving the real
// `useFileOps` hook through a real `doUndo()` call against a stubbed
// `/api/fs/rename` failure.
//
// Pattern mirrors useFileOps.delete.test.tsx: drive the REAL hook, not a
// copy of its logic.
import { expect, test } from "bun:test";
import { useRef, type MutableRefObject } from "react";
import { act, create } from "react-test-renderer";

import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const { useFileOps } = await import("@apps/explorer/listing/useFileOps");
const { getRetainedNotifications, _resetNotificationsForTest } = await import(
  "@platform/lib/notifications"
);
const { recordFsOp, resetFsUndo } = await import("@apps/explorer/lib/fs-undo");

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetchStub(opts: { failRename?: { src: string; dst: string } }): () => void {
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.startsWith("/api/config")) {
      return jsonResponse({
        start_dir: "/",
        home: "/home",
        fused_dir: "/home/Fused",
        version: "0",
        installed_version: null,
        mounts_root: "/home/.fused-render/mounts",
        cache_dir: "/home/.fused-render/cache",
        native_dir_picker: false,
      });
    }
    if (method === "POST" && url.startsWith("/api/fs/rename")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as { src: string; dst: string };
      if (
        opts.failRename &&
        body.src === opts.failRename.src &&
        body.dst === opts.failRename.dst
      ) {
        return jsonResponse({ detail: "internal error" }, 500);
      }
      return jsonResponse({ path: body.dst, is_dir: false, size: 0, mtime: 0 });
    }
    throw new Error(`unexpected fetch in test: ${method} ${url}`);
  }) as typeof fetch;
  return () => {
    globalThis.fetch = original;
  };
}

function Harness({
  handleRef,
}: {
  handleRef: MutableRefObject<ReturnType<typeof useFileOps> | null>;
}) {
  const pendingSelectRef = useRef<string | null>(null);
  const fileOps = useFileOps({
    base: "/dst",
    clipboard: null,
    refetch: () => {},
    pendingSelectRef,
    ownsBar: false,
  });
  handleRef.current = fileOps;
  return null;
}

async function mountHarness(): Promise<MutableRefObject<ReturnType<typeof useFileOps> | null>> {
  const handleRef: MutableRefObject<ReturnType<typeof useFileOps> | null> = { current: null };
  await act(async () => {
    create(<Harness handleRef={handleRef} />);
  });
  return handleRef;
}

test("a successful undo raises no notification (it only pops)", async () => {
  _resetNotificationsForTest();
  resetFsUndo();
  // Seed the undo stack directly — the module-level stack outlives the
  // gesture that recorded it, so a test can push an op straight onto it
  // rather than performing a real prior move first.
  recordFsOp({ kind: "move", pairs: [{ from: "/dst/a.txt", to: "/dst/b.txt" }] });
  const restoreFetch = installFetchStub({});
  try {
    const handleRef = await mountHarness();
    await act(async () => {
      handleRef.current!.doUndo();
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(getRetainedNotifications()).toEqual([]);
  } finally {
    restoreFetch();
    _resetNotificationsForTest();
    resetFsUndo();
  }
});

test("a failed undo still raises an attention notification, via the tone: \"error\" default (no explicit tier override)", async () => {
  _resetNotificationsForTest();
  resetFsUndo();
  recordFsOp({ kind: "move", pairs: [{ from: "/dst/a.txt", to: "/dst/b.txt" }] });
  // Undo inverts the pair: it renames /dst/b.txt back to /dst/a.txt. Fail
  // exactly that call.
  const restoreFetch = installFetchStub({
    failRename: { src: "/dst/b.txt", dst: "/dst/a.txt" },
  });
  try {
    const handleRef = await mountHarness();
    await act(async () => {
      handleRef.current!.doUndo();
      await new Promise((r) => setTimeout(r, 50));
    });
    const retained = getRetainedNotifications();
    expect(retained.length).toBe(1);
    expect(retained[0].tier).toBe("attention");
  } finally {
    restoreFetch();
    _resetNotificationsForTest();
    resetFsUndo();
  }
});
