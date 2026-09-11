// Regression coverage for the delete-notification reversal (user: "lets not
// send notifications for file deletion (no need)"). A trashed row used to
// raise a bare "Deleted" / "Deleted N items" `trail`-tier notification on
// success — this migration's own named motivating example, per
// DECISIONS-toasts-become-notifications.md — and the user asked for it back
// out: a card naming neither the file nor any real context is not worth
// keeping around. The error path is UNCHANGED and still must notify.
//
// Drives the REAL `useFileOps` hook (not a copy of its logic) through a real
// `doTrash` call, exactly as `useFileOps.paste.test.tsx` does for paste.
import { expect, test } from "bun:test";
import { useRef, type MutableRefObject } from "react";
import { act, create } from "react-test-renderer";

import type { RowCtx } from "@apps/explorer/listing/types";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const { useFileOps } = await import("@apps/explorer/listing/useFileOps");
const { getRetainedNotifications, _resetNotificationsForTest } = await import(
  "@platform/lib/notifications"
);

function row(name: string): RowCtx {
  return { path: `/dst/${name}`, name, isDir: false, parentDir: "/dst" };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetchStub(opts: { failPath?: string }): () => void {
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
    if (method === "POST" && url.startsWith("/api/fs/delete")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as { path: string; trash: boolean };
      if (opts.failPath && body.path === opts.failPath) {
        return jsonResponse({ detail: "permission denied" }, 500);
      }
      return jsonResponse({ deleted: body.path, trashed: true, trashed_to: body.path + ".trashed" });
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

test("a successful trash of one file raises no notification", async () => {
  _resetNotificationsForTest();
  const restoreFetch = installFetchStub({});
  try {
    const handleRef = await mountHarness();
    await act(async () => {
      handleRef.current!.doTrash([row("a.txt")]);
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(getRetainedNotifications()).toEqual([]);
  } finally {
    restoreFetch();
    _resetNotificationsForTest();
  }
});

test("a successful trash of multiple files raises no notification", async () => {
  _resetNotificationsForTest();
  const restoreFetch = installFetchStub({});
  try {
    const handleRef = await mountHarness();
    await act(async () => {
      handleRef.current!.doTrash([row("a.txt"), row("b.txt")]);
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(getRetainedNotifications()).toEqual([]);
  } finally {
    restoreFetch();
    _resetNotificationsForTest();
  }
});

test("a failed trash still raises an attention notification", async () => {
  _resetNotificationsForTest();
  const restoreFetch = installFetchStub({ failPath: "/dst/a.txt" });
  try {
    const handleRef = await mountHarness();
    await act(async () => {
      handleRef.current!.doTrash([row("a.txt")]);
      await new Promise((r) => setTimeout(r, 50));
    });
    const retained = getRetainedNotifications();
    expect(retained.length).toBe(1);
    expect(retained[0].tier).toBe("attention");
  } finally {
    restoreFetch();
    _resetNotificationsForTest();
  }
});
