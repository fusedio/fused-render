// THE BUG (breadcrumb-up lands on the parent with nothing selected): the
// merged search field's own breadcrumb strip — `PathCrumbs`, rendered
// whenever a folder's bar is "claimed" (folder-chrome.ts), which is the
// default view for a folder, not the unclaimed-bar fallback — is a SEPARATE
// implementation from Breadcrumb.tsx's own `.crumbs` strip. Breadcrumb.tsx's
// crumb clicks and useListingShortcuts.ts's Mod+Up chord both compute
// `sel: cameFromSelParam(target, fsPath)` before calling `navigate`, so
// useListingSelection's mount-time seed can land on the child folder just
// exited. `PathCrumbs`'s two onClick handlers called `navigate(target, {
// isDir: true })` with no `sel` at all — clicking a crumb in the ordinary
// claimed-bar view (the common case) silently dropped the child selection,
// even though the keyboard shortcut through the exact same folder always
// worked.
//
// Confirmed in a real browser (not just from source): with the bar claimed
// (the default for a folder listing), `#breadcrumb`'s rendered markup is
// `.listing-search-crumbs` (this component), not Breadcrumb.tsx's `.crumbs` —
// so this file, not Breadcrumb.tsx, is what a plain crumb click in the app
// actually runs.
//
// Same harness shape as search-button-empty-open.render.test.tsx:
// react-test-renderer + `mock.module` for `navigate`, captured rather than
// driven through a real DOM click event (there is none in this suite).
import { afterEach, beforeEach, expect, mock, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";

const navCalls: { path: string; opts: Record<string, unknown> | undefined }[] = [];
mock.module("@platform/lib/router", () => ({
  navigate: (p: string, opts?: Record<string, unknown>) => {
    navCalls.push({ path: p, opts });
  },
}));

const { PathCrumbs } = await import("@apps/explorer/listing/path-crumbs");

const mounted: ReactTestRenderer[] = [];

// The scroll-pin effect (`useLayoutEffect`) observes its own ref with a real
// `ResizeObserver`, which this suite's DOM-less renderer has no host for.
class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  navCalls.length = 0;
  (globalThis as Record<string, unknown>).ResizeObserver = FakeResizeObserver;
});

afterEach(() => {
  while (mounted.length) {
    const renderer = mounted.pop()!;
    act(() => renderer.unmount());
  }
  delete (globalThis as Record<string, unknown>).ResizeObserver;
});

function mount(fsPath: string, home?: string): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(createElement(PathCrumbs, { fsPath, home }), {
      // useLayoutEffect's scroll-pin reads a real DOM div; give it an inert one.
      createNodeMock: () => ({ scrollLeft: 0, scrollWidth: 0 }),
    });
  });
  mounted.push(renderer);
  return renderer;
}

function crumbLink(renderer: ReactTestRenderer, title: string) {
  return renderer.root.findAll((n) => n.type === "a" && n.props.title === title)[0];
}

function rootCrumb(renderer: ReactTestRenderer) {
  return renderer.root.findAll((n) => n.type === "a" && n.props.title === undefined)[0];
}

test("clicking an ancestor crumb seeds ?sel= with the child folder just exited", () => {
  const renderer = mount("/a/b/c");
  const link = crumbLink(renderer, "b");
  act(() => link.props.onClick({ preventDefault: () => {} }));
  expect(navCalls).toEqual([{ path: "/a/b", opts: { isDir: true, sel: "c" } }]);
});

test("clicking the root crumb seeds ?sel= with the top-level folder just exited", () => {
  const renderer = mount("/a/b/c");
  const link = rootCrumb(renderer);
  act(() => link.props.onClick({ preventDefault: () => {} }));
  expect(navCalls).toEqual([{ path: "/", opts: { isDir: true, sel: "a" } }]);
});

test("clicking the root crumb under a home prefix targets home and seeds the first segment under it", () => {
  const renderer = mount("/home/user/a/b", "/home/user");
  const link = rootCrumb(renderer);
  act(() => link.props.onClick({ preventDefault: () => {} }));
  expect(navCalls).toEqual([{ path: "/home/user", opts: { isDir: true, sel: "a" } }]);
});
