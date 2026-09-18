// KindCard's own poll (bugbot finding, a7aef9472): before this fix it fetched
// `/api/index/status` once on mount and once more after any button click, but
// never again — a scan started by "Re-index"/"Full scan" left the card
// showing "Scanning…" (and both actions disabled) forever, until the whole
// page was remounted, even though the scan itself finished seconds later.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";

import { installDomShim } from "@platform/lib/testDomShim";
import type { IndexStatus } from "@platform/lib/api";

installDomShim();
// IndexManager -> IndexProposalsDock -> router.ts, whose module-init
// `rewriteLegacyPath` reads `location` at IMPORT time (GlobalSearchOverlay.
// test.tsx's own header documents the same requirement) — install it, and
// the other globals router.ts's init path touches, before the dynamic import
// below runs that module code.
(globalThis as Record<string, unknown>).location = { pathname: "/index", search: "" };
(globalThis as Record<string, unknown>).window = {
  parent: undefined,
  top: undefined,
  dispatchEvent: () => true,
  setTimeout: (...args: Parameters<typeof globalThis.setTimeout>) => globalThis.setTimeout(...args),
  clearTimeout: (...args: Parameters<typeof globalThis.clearTimeout>) => globalThis.clearTimeout(...args),
};
(globalThis as Record<string, unknown>).history = {
  state: null,
  replaceState: () => {},
  pushState: () => {},
};

const { default: IndexManager } = await import("@shell/IndexManager");

function findAll(node: ReactTestRendererJSON | null, className: string): ReactTestRendererJSON[] {
  if (node === null || typeof node === "string") return [];
  const hits: ReactTestRendererJSON[] = [];
  if (typeof node.props?.className === "string" && node.props.className.split(" ").includes(className)) {
    hits.push(node);
  }
  for (const child of node.children ?? []) {
    if (typeof child !== "string") hits.push(...findAll(child, className));
  }
  return hits;
}

function text(node: ReactTestRendererJSON | null): string {
  if (node === null) return "";
  if (typeof node === "string") return node;
  return (node.children ?? []).map((c) => text(c as ReactTestRendererJSON)).join("");
}

function status(over: Partial<IndexStatus> = {}): IndexStatus {
  return {
    scanning: false,
    has_index: true,
    files_indexed: 3,
    last_completed_at: 1,
    running: false,
    run_id: null,
    root: "/Users/me",
    phase: "idle",
    dirs: 0,
    files: 0,
    ...over,
  } as IndexStatus;
}

function okResponse(data: unknown): Response {
  return { ok: true, status: 200, json: async () => data } as unknown as Response;
}

/** Same technique as IndexProposalsDock.test.tsx's `captureWindowTimers`,
 *  scoped to the GLOBAL `setTimeout`/`clearTimeout` — what `index-status.ts`'s
 *  poll and (after this fix) `KindCard`'s own poll call, rather than
 *  `window.setTimeout`. */
function captureGlobalTimers(): {
  pendingCount: () => number;
  runAllPending: () => void;
  restore: () => void;
} {
  const pending = new Map<number, () => void>();
  let nextId = 1;
  const realSetTimeout = globalThis.setTimeout;
  const realClearTimeout = globalThis.clearTimeout;
  globalThis.setTimeout = ((fn: () => void) => {
    const id = nextId++;
    pending.set(id, fn);
    return id;
  }) as unknown as typeof globalThis.setTimeout;
  globalThis.clearTimeout = ((id: number) => void pending.delete(id)) as typeof globalThis.clearTimeout;
  return {
    pendingCount: () => pending.size,
    runAllPending: () => {
      const fns = [...pending.values()];
      pending.clear();
      for (const fn of fns) fn();
    },
    restore: () => {
      globalThis.setTimeout = realSetTimeout;
      globalThis.clearTimeout = realClearTimeout;
    },
  };
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

test("a scanning kind's card re-fetches status on its own, without a remount, until the scan finishes", async () => {
  const timers = captureGlobalTimers();
  const realFetch = globalThis.fetch;
  let statusCall = 0;
  globalThis.fetch = (async (url: string) => {
    const u = String(url);
    if (u === "/api/index/kinds") return okResponse({ kinds: ["files"] });
    if (u.startsWith("/api/index/status")) {
      statusCall += 1;
      // First read: a scan is running. Second (and later) reads: it finished.
      return okResponse(status({ scanning: statusCall === 1 }));
    }
    throw new Error(`unstubbed fetch: ${u}`);
  }) as typeof fetch;

  try {
    let tree!: ReactTestRenderer;
    await act(async () => {
      tree = create(<IndexManager />);
    });
    await flush();

    // First fetch landed: scanning, so the card is showing "Scanning…" and the
    // scan-triggering buttons are disabled — and, crucially, a follow-up poll
    // was scheduled rather than the effect going quiet.
    expect(statusCall).toBe(1);
    expect(timers.pendingCount()).toBeGreaterThan(0);

    let json = tree.toJSON() as ReactTestRendererJSON;

    // Fire the scheduled poll tick myself (this test owns time) instead of
    // waiting on a real interval.
    await act(async () => {
      timers.runAllPending();
    });
    await flush();

    expect(statusCall).toBe(2);
    json = tree.toJSON() as ReactTestRendererJSON;
    expect(text(findAll(json, "deploy-muted")[0])).not.toContain("Scanning");
  } finally {
    globalThis.fetch = realFetch;
    timers.restore();
  }
});
