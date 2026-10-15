// The one rule every Marketplace pill reads (`actionFor`) — card, hero and
// installed row must agree on Get / ring / Open / Update / Retry, including
// the gap between a job finishing and the catalog re-read that stamps it
// installed (the pill must not flash back to Get there).
import { afterEach, describe, expect, test } from "bun:test";

import { installDomShim } from "./testDomShim";

installDomShim();

const { actionFor, isActive, installedAppHref, categoryLabel, refreshCatalog, peekCatalog, installApp, peekJobs, searchApps } = await import("./marketplace");
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
    release_id: "r1",
    commit: "c1",
    published_at: null,
    updated_at: null,
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

const stamp = { path: "/ws/community/sine-starter", version: "1.0.0", release_id: "r0", installed_at: "", modified: false };

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
    const cat = (commit: string) => ({ status: "ok", catalog_version: commit, categories: [], collections: [], apps: [] });
    const first = refreshCatalog({ again: true });
    const second = refreshCatalog({ again: true });
    resolvers[1](cat("new"));
    await second;
    resolvers[0](cat("old"));
    await first;
    expect(peekCatalog().catalog?.catalog_version).toBe("new");
    expect(peekCatalog().loading).toBe(false);
  });
});

describe("jobs poll", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  test("a poll that left before an install started never wipes the new job", async () => {
    let releaseJobs: ((body: unknown) => void) | null = null;
    const queued = { slug: "race-app", action: "install", state: "queued", progress: 0, bytes_done: 0, bytes_total: 0, message: "" };
    globalThis.fetch = ((url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/jobs") && !releaseJobs) {
        return new Promise((res) => {
          releaseJobs = (body) => res(new Response(JSON.stringify(body), { status: 200 }));
        });
      }
      if (u.includes("/install") && init?.method === "POST") {
        return Promise.resolve(new Response(JSON.stringify({ status: "ok", job: queued }), { status: 200 }));
      }
      if (u.includes("/jobs")) {
        return Promise.resolve(new Response(JSON.stringify({ jobs: { "race-app": queued } }), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ status: "ok", catalog_version: "c", categories: [], collections: [], apps: [] }), { status: 200 }));
    }) as typeof fetch;
    // The catalog read fires one jobs read, held open here.
    void refreshCatalog({ again: true });
    await installApp({ slug: "race-app", name: "Race App" });
    expect(peekJobs()["race-app"]?.state).toBe("queued");
    // The stale poll lands after the install with an empty map.
    releaseJobs!({ jobs: {} });
    await new Promise((r) => setTimeout(r, 0));
    expect(peekJobs()["race-app"]?.state).toBe("queued");
  });
});

describe("searchApps", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  test("builds the query: q always, category/cursor/limit only when set", async () => {
    const urls: string[] = [];
    globalThis.fetch = ((url: string) => {
      urls.push(String(url));
      return Promise.resolve(new Response(JSON.stringify({ status: "ok", apps: [], next_cursor: null }), { status: 200 }));
    }) as typeof fetch;
    await searchApps({ q: "local chat" });
    await searchApps({ q: "x", category: "all", cursor: "" });
    await searchApps({ q: "x", category: "starters", cursor: "24", limit: 12 });
    expect(urls[0]).toBe("/api/marketplace/search?q=local+chat");
    expect(urls[1]).toBe("/api/marketplace/search?q=x");
    expect(urls[2]).toBe("/api/marketplace/search?q=x&category=starters&cursor=24&limit=12");
  });
});
