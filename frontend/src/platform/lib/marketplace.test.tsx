// The one rule every Marketplace pill reads (`actionFor`) — card, hero and
// installed row must agree on Get / ring / Open / Update / Retry, including
// the gap between a job finishing and the catalog re-read that stamps it
// installed (the pill must not flash back to Get there).
import { afterEach, describe, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

import { installDomShim } from "./testDomShim";

installDomShim();

const { actionFor, isActive, installedAppHref, sourceUrlOf, categoryLabel, refreshCatalog, peekCatalog, installApp, peekJobs, searchApps, useMarketplaceSearch, SEARCH_DEBOUNCE_MS } = await import("./marketplace");
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

  test("Source links only a github.com address — the row's string is the catalog's", () => {
    expect(sourceUrlOf({ source_url: "https://github.com/alice/sine" })).toBe("https://github.com/alice/sine");
    expect(sourceUrlOf({ source_url: "javascript:alert(1)" })).toBeNull();
    expect(sourceUrlOf({ source_url: "http://github.com/alice/sine" })).toBeNull();
    expect(sourceUrlOf({ source_url: "https://github.com.evil.example/x" })).toBeNull();
    expect(sourceUrlOf({ source_url: null })).toBeNull();
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

describe("useMarketplaceSearch", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = realFetch;
  });
  const hit = (slug: string) => ({ ...app({ slug }), score: 1, highlights: [] });
  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

  function mount(initial: { q: string; category?: string }) {
    let latest!: ReturnType<typeof useMarketplaceSearch>;
    function Probe({ q, category }: { q: string; category?: string }) {
      latest = useMarketplaceSearch(q, category);
      return null;
    }
    let r!: ReturnType<typeof create>;
    act(() => {
      r = create(<Probe {...initial} />);
    });
    return {
      get: () => latest,
      set: (next: { q: string; category?: string }) =>
        act(() => {
          r.update(<Probe {...next} />);
        }),
    };
  }

  test("a double loadMore sends one request and appends no duplicates", async () => {
    const calls: string[] = [];
    let release: (() => void) | null = null;
    globalThis.fetch = ((url: string) => {
      const u = String(url);
      calls.push(u);
      if (u.includes("cursor=c1")) {
        return new Promise((res) => {
          release = () => res(new Response(JSON.stringify({ status: "ok", apps: [hit("a"), hit("b")], next_cursor: null }), { status: 200 }));
        });
      }
      return Promise.resolve(new Response(JSON.stringify({ status: "ok", apps: [hit("a")], next_cursor: "c1" }), { status: 200 }));
    }) as typeof fetch;
    const h = mount({ q: "chat" });
    await act(async () => {
      await sleep(SEARCH_DEBOUNCE_MS + 30);
    });
    expect(h.get().nextCursor).toBe("c1");
    act(() => {
      h.get().loadMore();
      h.get().loadMore();
    });
    expect(calls.filter((u) => u.includes("cursor=c1")).length).toBe(1);
    expect(h.get().loadingMore).toBe(true);
    await act(async () => {
      release!();
      await sleep(0);
    });
    expect(h.get().apps.map((a) => a.slug)).toEqual(["a", "b"]);
    expect(h.get().loadingMore).toBe(false);
  });

  test("a query change mid-loadMore drops the stale page and clears the cursor", async () => {
    let release: (() => void) | null = null;
    globalThis.fetch = ((url: string) => {
      const u = String(url);
      if (u.includes("cursor=c1")) {
        return new Promise((res) => {
          release = () => res(new Response(JSON.stringify({ status: "ok", apps: [hit("stale")], next_cursor: "c2" }), { status: 200 }));
        });
      }
      if (u.includes("q=other")) {
        return Promise.resolve(new Response(JSON.stringify({ status: "ok", apps: [hit("fresh")], next_cursor: null }), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ status: "ok", apps: [hit("a")], next_cursor: "c1" }), { status: 200 }));
    }) as typeof fetch;
    const h = mount({ q: "chat" });
    await act(async () => {
      await sleep(SEARCH_DEBOUNCE_MS + 30);
    });
    act(() => h.get().loadMore());
    h.set({ q: "other" });
    expect(h.get().nextCursor).toBeNull(); // cleared at once, before the debounce
    await act(async () => {
      release!();
      await sleep(0);
    });
    expect(h.get().apps.map((a) => a.slug)).not.toContain("stale");
    await act(async () => {
      await sleep(SEARCH_DEBOUNCE_MS + 30);
    });
    expect(h.get().apps.map((a) => a.slug)).toEqual(["fresh"]);
    expect(h.get().nextCursor).toBeNull();
    expect(h.get().loadingMore).toBe(false);
  });
});

describe("installApp confirm answers", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  test("a showcase copy's overwrite question starts no job and the yes posts overwrite: true", async () => {
    const bodies: Record<string, unknown>[] = [];
    const queued = { slug: "sine-starter", action: "install", state: "queued", progress: 0, bytes_done: 0, bytes_total: 0, message: "" };
    globalThis.fetch = ((url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/install") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        bodies.push(body);
        const answer = body.overwrite
          ? { status: "ok", job: queued }
          : { status: "confirm", overwrite: "showcase", path: "/Users/me/Fused/showcase/sine-starter", message: "Overwrite?" };
        return Promise.resolve(new Response(JSON.stringify(answer), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({ jobs: {} }), { status: 200 }));
    }) as typeof fetch;
    const first = await installApp({ slug: "sine-starter", name: "Sine Starter" });
    expect("status" in first && first.status === "confirm" && "overwrite" in first && first.overwrite).toBe("showcase");
    expect(peekJobs()["sine-starter"]).toBeUndefined();
    const second = await installApp({ slug: "sine-starter", name: "Sine Starter" }, { overwrite: true });
    expect("state" in second && second.state).toBe("queued");
    expect(bodies).toEqual([{ slug: "sine-starter" }, { slug: "sine-starter", overwrite: true }]);
  });
});
