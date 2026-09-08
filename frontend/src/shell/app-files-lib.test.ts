// Pure-helper tests for shell/app-files-lib.ts — no DOM required.
import { describe, expect, it } from "bun:test";
import { isAwaitingFile, renderSrc } from "@shell/app-files-lib";
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

  it(
    "THE regression for finding 4 (second round): a template path that happens to sit " +
      "UNDER the app folder is never rewritten onto the extracted tree — only the file's " +
      "own path is",
    () => {
      // A contrived template path under APP — not how any real template
      // resolves today, but nothing enforces that it never could (this
      // finding's whole point). Before `rewritePath: false` existed at this
      // call site, `snapshotFrameSrc` rewrote EVERY `path` unconditionally,
      // so this would have come back pointing at the extracted tree (or a
      // 404 if the template did not exist at that commit) instead of the
      // live template every other part of the page assumes is running.
      const templateUnderApp: TemplateEntry = {
        mode: "table",
        path: APP + "/vendored-template.html",
      } as TemplateEntry;
      const file = DIR + "/data.csv";
      const src = renderSrc(file, templateUnderApp, SNAP, SNAP.sha);
      const u = new URL(src as string, "http://x");
      expect(u.searchParams.get("path")).toBe(templateUnderApp.path);
      // The FILE itself (the actual subject) is still correctly resolved.
      expect(u.searchParams.get("_file")).toBe(file);
    },
  );
});

describe("isAwaitingFile — the Files tab's right-pane loading gate (finding 2)", () => {
  it("false when nothing is selected at all — the ordinary blank state applies", () => {
    expect(isAwaitingFile(null, null)).toBe(false);
  });

  it("false once the effective target has resolved", () => {
    expect(isAwaitingFile("main.py", DIR + "/main.py")).toBe(false);
  });

  it(
    "THE regression for finding 2: a file IS selected but the effective target has not " +
      "resolved yet (a pending snapshot) — this must read as 'loading', never 'nothing " +
      "selected'",
    () => {
      expect(isAwaitingFile("main.py", null)).toBe(true);
    },
  );
});
