// AppPageGitPeek.tsx — the git peek's own body.
//
// Finding 6, confirmed against the closing sequence before this file
// existed: `closeGit` (useAppPageGitColumn.ts) sets `open` false; `useDirMode`
// resets to ABSENT one commit LATER, so `gitSrc` — and the `src` prop this
// component is handed — goes to null while the panel is still visible for
// the whole 200ms slide-out. Rendering that transition literally showed the
// live template blink to "Loading…" and only then slide away. This pins the
// fix: the panel keeps the last non-null `src` for as long as it is shut, and
// only falls back to "Loading…" for a genuinely fresh open with nothing
// resolved yet.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import AppPageGitPeek from "@shell/AppPageGitPeek";
import type { DirMode } from "@apps/explorer/lib/dir-mode";

const RESOLVED: DirMode = {
  entry: { mode: "git", path: "/templates/git", icon: null },
  bound: { mode: "git", path: "/templates/git", icon: null },
  pending: false,
  failed: false,
};
const ABSENT: DirMode = { entry: null, bound: null, pending: false, failed: false };
const PENDING: DirMode = {
  entry: { mode: "git", path: null, icon: null },
  bound: null,
  pending: true,
  failed: false,
};

const LAYOUT = { width: 400, onSeamPointerDown: () => {}, dragging: false };

function findIframe(renderer: ReactTestRenderer) {
  return renderer.root.findAllByType("iframe");
}
function findText(renderer: ReactTestRenderer, text: string): boolean {
  return renderer.root.findAllByType("p").some((p) => p.children.join("") === text);
}

test("closing keeps the last frame on screen through the slide-out instead of flashing Loading", () => {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      createElement(AppPageGitPeek, {
        open: true,
        mode: RESOLVED,
        src: "/render?path=x",
        onClose: () => {},
        layout: LAYOUT,
      }),
    );
  });
  expect(findIframe(renderer)[0]!.props.src).toBe("/render?path=x");

  // The close sequence: `open` flips false, then (one commit later,
  // useDirMode's own timing) `mode`/`src` settle back to ABSENT/null — while
  // the panel is still `is-open`-less but visible for its slide-out.
  act(() => {
    renderer.update(
      createElement(AppPageGitPeek, {
        open: false,
        mode: RESOLVED,
        src: "/render?path=x",
        onClose: () => {},
        layout: LAYOUT,
      }),
    );
  });
  act(() => {
    renderer.update(
      createElement(AppPageGitPeek, {
        open: false,
        mode: ABSENT,
        src: null,
        onClose: () => {},
        layout: LAYOUT,
      }),
    );
  });

  expect(findText(renderer, "Loading…")).toBe(false);
  expect(findIframe(renderer)[0]!.props.src).toBe("/render?path=x");
});

test("reopening before the probe resolves still shows Loading, not a stale frame", () => {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      createElement(AppPageGitPeek, {
        open: true,
        mode: RESOLVED,
        src: "/render?path=old",
        onClose: () => {},
        layout: LAYOUT,
      }),
    );
  });
  act(() => {
    renderer.update(
      createElement(AppPageGitPeek, {
        open: false,
        mode: ABSENT,
        src: null,
        onClose: () => {},
        layout: LAYOUT,
      }),
    );
  });
  // Reopened — pending again, nothing resolved yet.
  act(() => {
    renderer.update(
      createElement(AppPageGitPeek, {
        open: true,
        mode: PENDING,
        src: null,
        onClose: () => {},
        layout: LAYOUT,
      }),
    );
  });
  expect(findText(renderer, "Loading…")).toBe(true);
  expect(findIframe(renderer).length).toBe(0);
});
