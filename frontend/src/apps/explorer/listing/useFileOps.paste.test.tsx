// Regression coverage for the paste-progress popup surviving its own popup's
// expiry (code-review finding on useFileOps.ts:217, PR #1104). `doPaste`'s
// loop calls `notify({...}, progressToastId)` on every iteration to update
// ONE standing popup card ("Copying N of M…") — but the original code never
// wrote the id `notify()` returns back into `progressToastId`. Under the old
// toast.ts store nothing ever auto-expired, so `replaceId` always matched
// and this was invisible. Under `lib/notifications.ts`, a popup that isn't
// re-armed in time (a file slower than JOB_POPUP_VISIBLE_MS + TOAST_EXIT_MS)
// auto-expires; the next iteration's `notify(..., progressToastId)` then
// misses (there is nothing to replace), mints a FRESH id, and — because
// `progressToastId` was never updated — every remaining iteration repeats
// the same miss, popping a brand-new card per remaining file instead of one
// continuous one.
//
// This drives the REAL `useFileOps` hook (not a copy of its logic) through a
// real multi-file copy paste, with one artificially slow file placed first so
// the popup expires mid-loop, and counts how many DISTINCT popup ids the
// remaining iterations actually used.
import { expect, test } from "bun:test";
import { useRef, type MutableRefObject } from "react";
import { act, create } from "react-test-renderer";

import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const { useFileOps } = await import("@apps/explorer/listing/useFileOps");
const { setClipboard } = await import("@apps/explorer/lib/fs-clipboard");
const { getPopupNotification, _resetNotificationsForTest } = await import(
  "@platform/lib/notifications"
);
const { JOB_POPUP_VISIBLE_MS } = await import("@platform/lib/jobs");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// One slow file (SRC[0]) so the popup's own exit timer runs out mid-loop —
// same shape the finding describes ("copying 10 files where one file takes
// >2.65s"), shrunk to 3 files for a fast test.
const SLOW_MS = JOB_POPUP_VISIBLE_MS + 300; // > JOB_POPUP_VISIBLE_MS + TOAST_EXIT_MS
const SRC = ["/src/f1.txt", "/src/f2.txt", "/src/f3.txt"];

function installFetchStub(observedCopyPopupIds: (number | undefined)[]): () => void {
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
    if (method === "GET" && url.startsWith("/api/fs/list")) {
      return jsonResponse({ path: "/dst", entries: [] });
    }
    if (method === "GET" && url.startsWith("/api/fs/stat")) {
      const path = new URL(url, "http://localhost").searchParams.get("path") ?? "";
      const name = path.split("/").pop() ?? "";
      return jsonResponse({ path, name, is_dir: false, size: 0, mtime: 0 });
    }
    if (method === "POST" && url.startsWith("/api/fs/copy")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as { src: string; dst: string };
      // The point of interest: which popup id is CURRENTLY showing right as
      // this iteration's copy kicks off — `notify()` for this iteration
      // always runs before the await chain that reaches here.
      observedCopyPopupIds.push(getPopupNotification()?.id);
      if (body.src === SRC[0]) await sleep(SLOW_MS);
      const name = body.dst.split("/").pop() ?? "";
      return jsonResponse({ path: body.dst, name, is_dir: false, size: 0, mtime: 0 });
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

test(
  "a paste slower than the popup's own lifetime keeps updating ONE card, not a fresh one per remaining file",
  async () => {
    _resetNotificationsForTest();
    const observed: (number | undefined)[] = [];
    const restoreFetch = installFetchStub(observed);
    try {
      setClipboard({ paths: SRC, op: "copy" }, false);

      const handleRef: MutableRefObject<ReturnType<typeof useFileOps> | null> = { current: null };
      await act(async () => {
        create(<Harness handleRef={handleRef} />);
      });

      await act(async () => {
        handleRef.current!.doPaste("/dst");
        // Long enough to clear the slow file's delay plus both remaining
        // (fast) iterations and the final dismissPopup().
        await sleep(SLOW_MS + 1000);
      });

      // 3 files copied: one popup id per iteration was observed.
      expect(observed.length).toBe(3);

      // The first file's copy is issued under the ORIGINAL popup. The popup
      // then genuinely expires while that copy is still in flight (SLOW_MS
      // exceeds its lifetime) — an unavoidable, one-time re-mint is fine.
      // What the bug produced was a SECOND re-mint on the very next
      // iteration too, because `progressToastId` was frozen at its original
      // (now-dead) value forever after. Fixed: iteration 2 and 3 share the
      // SAME id — at most 2 distinct ids across the whole paste.
      const distinct = new Set(observed);
      expect(distinct.size).toBeLessThanOrEqual(2);
      // And the two later iterations, in particular, must not each mint
      // their own fresh id — this is the exact "repeats for every
      // remaining file" symptom the finding describes.
      expect(observed[1]).toBe(observed[2]);
    } finally {
      restoreFetch();
      _resetNotificationsForTest();
    }
  },
  10_000,
);
