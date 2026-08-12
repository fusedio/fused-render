// The folder-that-is-an-app button, in one place: what it says and where it
// goes. Two surfaces render it — the title bar for the OPEN folder (Preview)
// and the preview pane's header for a SELECTED folder — and they used to
// answer these questions separately, which is the whole reason this module
// exists.
//
// app-button reaches the router, which reads `location` at module scope, so the
// stub precedes the (therefore dynamic) import — the same trade
// fs-actions.test.ts makes rather than carrying a DOM.
import { expect, test } from "bun:test";
import type { AppLinkStatus } from "@platform/lib/api";

(globalThis as { location?: unknown }).location = new URL("http://x/");
const { appButtonSpec } = await import("@apps/explorer/lib/app-button");

const FOLDER = "/w/proj";
const APP_FILE = "/w/proj/index.html";

test("an unlinked folder offers to BECOME an app", () => {
  // The gap that made this button look broken: a folder outside the workspace
  // cannot offer `_mode=app` at all (templates/app/condition.py gates on
  // <workspace>/<tag>/<project> or the linked-app registry), so "Open as app"
  // had nowhere to go. The honest offer is to link it first.
  const link: AppLinkStatus = { status: "unlinked", name: null };
  expect(appButtonSpec(FOLDER, APP_FILE, link)).toEqual({ action: "link", label: "Add as app" });
});

test("a linked folder opens its own folder in the app view", () => {
  const link: AppLinkStatus = { status: "linked", name: "my app", tag: "linked" };
  expect(appButtonSpec(FOLDER, APP_FILE, link)).toEqual({
    action: "open",
    label: "Open as app",
    target: { path: FOLDER, isDir: true, mode: "app" },
  });
});

test("a workspace app folder opens the same way", () => {
  // Same shape, different tag: the identity only decides WHETHER the gate will
  // take the folder — the destination is the folder either way.
  const link: AppLinkStatus = { status: "workspace", name: "demo", tag: "scratch" };
  expect(appButtonSpec(FOLDER, APP_FILE, link)).toEqual({
    action: "open",
    label: "Open as app",
    target: { path: FOLDER, isDir: true, mode: "app" },
  });
});

test("`_mode=app` is never omitted from a folder open", () => {
  // Without it the destination falls to its own default template, which for a
  // directory is `_listing` — the folder's file list, i.e. the bug this module
  // was extracted to kill. (App CARDS open that listing on purpose; this
  // button is the request for the app view specifically.)
  const link: AppLinkStatus = { status: "linked", name: "app", tag: "linked" };
  const spec = appButtonSpec(FOLDER, APP_FILE, link);
  expect(spec).toMatchObject({ action: "open" });
  expect((spec as { target: { mode?: string } }).target.mode).toBe("app");
});

test("no identity falls back to the app PAGE, never to the folder", () => {
  // An older backend (no `tag`) or a workspace folder that isn't exactly an app
  // dir — templates/app/condition.py would refuse it, so `_mode=app` would
  // resolve to the listing. Opening the FILE renders the page instead, which is
  // the one answer that must never come out of here.
  const link: AppLinkStatus = { status: "workspace", name: null };
  expect(appButtonSpec(FOLDER, APP_FILE, link)).toEqual({
    action: "open",
    label: "Open as app",
    target: { path: APP_FILE, isDir: false },
  });
});

test("a half-known identity is no identity", () => {
  // A tag with no name (or the reverse) does not say "exactly an app dir".
  expect(appButtonSpec(FOLDER, APP_FILE, { status: "linked", name: null, tag: "linked" })).toEqual({
    action: "open",
    label: "Open as app",
    target: { path: APP_FILE, isDir: false },
  });
  expect(appButtonSpec(FOLDER, APP_FILE, { status: "linked", name: "app" })).toEqual({
    action: "open",
    label: "Open as app",
    target: { path: APP_FILE, isDir: false },
  });
});

test("no button at all until every half is known", () => {
  // No lone page = not an app folder; no link status = the probe is still out,
  // and a button that appears and then changes its own label is worse than one
  // that arrives a beat late.
  expect(appButtonSpec(FOLDER, null, { status: "linked", name: "a", tag: "linked" })).toBeNull();
  expect(appButtonSpec(FOLDER, APP_FILE, null)).toBeNull();
  expect(appButtonSpec(null, APP_FILE, { status: "linked", name: "a", tag: "linked" })).toBeNull();
  expect(appButtonSpec(null, null, null)).toBeNull();
});
