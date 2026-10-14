// The one rule every Marketplace pill reads (`actionFor`) — card, hero and
// installed row must agree on Get / ring / Open / Update / Retry, including
// the gap between a job finishing and the catalog re-read that stamps it
// installed (the pill must not flash back to Get there).
import { afterEach, describe, expect, test } from "bun:test";

import { installDomShim } from "./testDomShim";

installDomShim();

const { actionFor, isActive, installedAppHref, categoryLabel, refreshCatalog, peekCatalog } = await import("./marketplace");
type AppEntry = import("./marketplace").AppEntry;
type InstallJob = import("./marketplace").InstallJob;

function app(over: Partial<AppEntry> = {}): AppEntry {
  return {
    slug: "sine-starter",
    name: "Sine Starter",
    description: "",
    author: { name: "Sina" },
    tags: [],
    category: "starters",
    version: "1.0.1",
    requires_python: true,
    min_fused_render: null,
    ai_capabilities: [],
    ai_models: [],
    ai_model_param: null,
    icon_url: null,
    preview_url: null,
    size_bytes: 1,
    file_count: 1,
    tree_sha: "a",
    source_url: null,
    featured: false,
    installed: null,
    update_available: false,
    compatible: true,
    ...over,
  };
}

function job(over: Partial<InstallJob> = {}): InstallJob {
  return { slug: "sine-starter", action: "install", state: "downloading", progress: 0.4, bytes_done: 4, bytes_total: 10, message: "", ...over };
}

const stamp = { path: "/ws/community/sine-starter", version: "1.0.0", tree_sha: "b", installed_at: "", modified: false };

describe("actionFor", () => {
  test("not installed → get; incompatible → incompatible", () => {
    expect(actionFor(app(), null).kind).toBe("get");
    expect(actionFor(app({ compatible: false }), null).kind).toBe("incompatible");
  });

  test("an active job is a ring, queued is indeterminate", () => {
    expect(actionFor(app(), job())).toMatchObject({ kind: "progress", progress: 0.4 });
    expect(actionFor(app(), job({ state: "queued" }))).toMatchObject({ kind: "progress", progress: null });
  });

  test("done before the catalog caught up still reads as open", () => {
    const a = actionFor(app(), job({ state: "done", path: "/ws/community/sine-starter" }));
    expect(a).toMatchObject({ kind: "open", path: "/ws/community/sine-starter" });
  });

  test("a finished UPDATE reads as open until the catalog drops update_available", () => {
    const stale = app({ installed: stamp, update_available: true });
    expect(actionFor(stale, job({ action: "update", state: "done", path: stamp.path })).kind).toBe("open");
  });

  test("installed → open, or update when the tree moved", () => {
    expect(actionFor(app({ installed: stamp }), null).kind).toBe("open");
    expect(actionFor(app({ installed: stamp, update_available: true }), null).kind).toBe("update");
  });

  test("an error is a retry carrying the reason", () => {
    expect(actionFor(app(), job({ state: "error", error: "boom" }))).toMatchObject({ kind: "retry", error: "boom" });
  });
});

describe("helpers", () => {
  test("isActive", () => {
    expect(isActive(job())).toBe(true);
    expect(isActive(job({ state: "done" }))).toBe(false);
    expect(isActive(null)).toBe(false);
  });

  test("Open lands on the entry page in the explorer", () => {
    expect(installedAppHref("/ws/community/my app/")).toBe("/explorer/view/ws/community/my%20app/index.html");
  });

  test("category label falls back to a title-cased id", () => {
    expect(categoryLabel(null, "local-ai")).toBe("Local AI");
  });
});

describe("refreshCatalog", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  test("an older read that answers last never overwrites a newer one", async () => {
    const resolvers: Array<(body: unknown) => void> = [];
    globalThis.fetch = ((url: string) => {
      if (String(url).includes("/jobs")) {
        return Promise.resolve(new Response(JSON.stringify({ jobs: {} }), { status: 200 }));
      }
      return new Promise((res) => {
        resolvers.push((body) => res(new Response(JSON.stringify(body), { status: 200 })));
      });
    }) as typeof fetch;
    const cat = (commit: string) => ({ status: "ok", commit, categories: [], apps: [] });
    const first = refreshCatalog({ again: true });
    const second = refreshCatalog({ again: true });
    resolvers[1](cat("new"));
    await second;
    resolvers[0](cat("old"));
    await first;
    expect(peekCatalog().catalog?.commit).toBe("new");
    expect(peekCatalog().loading).toBe(false);
  });
});
