// listing/source.ts: the folder view's data calls, answered from the bucket
// index for an `s3://` location in the local routes' own shapes — and never
// sent to a local route.
import { afterEach, beforeEach, expect, test } from "bun:test";
import { listFolder, rankFolder, requestScan, statLocation } from "@apps/explorer/listing/source";

const realFetch = globalThis.fetch;
let calls: string[] = [];
let answer: (url: string) => unknown = () => ({});

beforeEach(() => {
  calls = [];
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    return new Response(JSON.stringify(answer(url)), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

const params = (url: string) => new URL(url, "http://x").searchParams;

test("an S3 folder lists through the remote route, as a ListResult with its cursor", async () => {
  answer = () => ({
    url: "s3://fused-asset/crops/",
    entries: [
      { url: "s3://fused-asset/crops/2024/", name: "2024", is_dir: true, size: null, mtime: null },
      { url: "s3://fused-asset/crops/a.csv", name: "a.csv", is_dir: false, size: 3, mtime: 9 },
    ],
    cursor: "c1",
  });
  const res = await listFolder("s3://fused-asset/crops", null);
  expect(calls).toHaveLength(1);
  expect(calls[0].startsWith("/api/remote/list?")).toBe(true);
  // The folder is asked for as a prefix, with its trailing "/".
  expect(params(calls[0]).get("url")).toBe("s3://fused-asset/crops/");
  expect(res).toEqual({
    path: "s3://fused-asset/crops",
    entries: [
      { name: "2024", is_dir: true, size: null, mtime: null },
      { name: "a.csv", is_dir: false, size: 3, mtime: 9 },
    ],
    truncated: true,
    cursor: "c1",
  });
});

test("a local folder still lists through /api/fs/list", async () => {
  answer = () => ({ path: "/tmp/x", entries: [] });
  await listFolder("/tmp/x");
  expect(calls[0].startsWith("/api/fs/list?")).toBe(true);
});

test("an S3 search is scoped with `under`, and hits come back relative to the folder", async () => {
  answer = () => ({
    ready: true,
    hits: [
      { url: "s3://fused-asset/crops/2024/", key: "crops/2024/", is_dir: true, size: null, mtime: null },
      { url: "s3://fused-asset/crops/2024/b.tif", key: "crops/2024/b.tif", is_dir: false, size: 5, mtime: 1 },
    ],
    total: 2,
    truncated: false,
    mode: "glob",
    pattern: "**/*2024*",
  });
  const res = await rankFolder("s3://fused-asset/crops", "2024", { limit: 50, ranked: true });
  const p = params(calls[0]);
  expect(calls[0].startsWith("/api/remote-index/rank?")).toBe(true);
  expect(p.get("under")).toBe("s3://fused-asset/crops/");
  expect(p.get("q")).toBe("2024");
  expect(res).toEqual({
    covered: true,
    reason: "",
    hits: [
      { rel: "2024", is_dir: true, size: null, mtime: null },
      { rel: "2024/b.tif", is_dir: false, size: 5, mtime: 1 },
    ],
    truncated: false,
    total: 2,
    base: "s3://fused-asset/crops",
    mode: "glob",
    pattern: "**/*2024*",
  });
});

test("an S3 index still loading reads as `scanning`, which the box polls", async () => {
  answer = () => ({ ready: false, hits: [], total: 0, truncated: false });
  const res = await rankFolder("s3://fused-asset/", "x");
  expect(params(calls[0]).get("under")).toBe("s3://fused-asset/");
  expect(res.covered).toBe(false);
  expect(res.reason).toBe("scanning");
});

test("a typed S3 address resolves through the remote stat", async () => {
  answer = () => ({ url: "s3://fused-asset/crops/2024/", key: "crops/2024/", kind: "dir" });
  expect(await statLocation("s3://fused-asset/crops/2024")).toEqual({
    path: "s3://fused-asset/crops/2024",
    is_dir: true,
  });
  expect(calls[0].startsWith("/api/remote/stat?")).toBe(true);
});

test("an S3 folder never asks the local index for a scan", async () => {
  const r = await requestScan("s3://fused-asset/crops");
  expect(r.started).toBe(false);
  expect(calls).toHaveLength(0);
});
