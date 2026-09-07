// Pure-helper tests for shell/app-files-lib.ts — no DOM required.
import { describe, expect, it } from "bun:test";
import { renderSrc } from "@shell/app-files-lib";
import type { TemplateEntry } from "@platform/lib/api";

const APP = "/repo/myapp";
const DIR = "/cache/key/abc1234";
const SNAP = { sha: "abc1234", dir: DIR, app_dir: APP };

const RENDER_MODE: TemplateEntry = { mode: "_render", path: null } as TemplateEntry;
const CSV_MODE: TemplateEntry = {
  mode: "table",
  path: "/templates/csv/template.html",
} as TemplateEntry;

describe("renderSrc — the app page's Files tab iframe src (finding 2)", () => {
  it("live: builds the ordinary /render src with no snapshot params", () => {
    const src = renderSrc(APP + "/index.html", RENDER_MODE, null, null);
    expect(src).toBe("/render?path=" + encodeURIComponent(APP + "/index.html"));
  });

  it(
    "THE regression for finding 2: a snapshotted file's src carries " +
      "_snapshot/_snapshot_dir/_snapshot_app, not merely the rewritten path",
    () => {
      // `file` here is already rewritten onto the extracted tree, as
      // AppFiles.tsx's own `effectiveDir` would have done before calling this.
      const file = DIR + "/index.html";
      const src = renderSrc(file, RENDER_MODE, SNAP, SNAP.sha);
      expect(src).not.toBeNull();
      const u = new URL(src as string, "http://x");
      expect(u.searchParams.get("path")).toBe(file);
      expect(u.searchParams.get("_snapshot")).toBe(SNAP.sha);
      expect(u.searchParams.get("_snapshot_dir")).toBe(SNAP.dir);
      expect(u.searchParams.get("_snapshot_app")).toBe(SNAP.app_dir);
    },
  );

  it("an ordinary (non-_render) template carries `_file` alongside the three snapshot params", () => {
    const file = DIR + "/data.csv";
    const src = renderSrc(file, CSV_MODE, SNAP, SNAP.sha);
    const u = new URL(src as string, "http://x");
    expect(u.searchParams.get("path")).toBe(CSV_MODE.path);
    expect(u.searchParams.get("_file")).toBe(file);
    expect(u.searchParams.get("_snapshot")).toBe(SNAP.sha);
    expect(u.searchParams.get("_snapshot_dir")).toBe(SNAP.dir);
    expect(u.searchParams.get("_snapshot_app")).toBe(SNAP.app_dir);
  });

  it("pending (sha claimed, snap not yet resolved) returns null, never a live-tree src", () => {
    expect(renderSrc(APP + "/index.html", RENDER_MODE, null, SNAP.sha)).toBeNull();
  });
});
