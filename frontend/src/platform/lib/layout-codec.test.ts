// A pane's URL changes two ways, and the shell must re-encode `_layout` for
// both. A WRITE inside the pane (the runtime's set(), the shell's wrapped
// push/replaceState) surfaces as `fused:urlchange`. A TRAVERSAL inside the pane
// — Back/Forward while a pane iframe has focus — writes nothing and surfaces
// only as `popstate`. Hooking just the first left the top-level `_layout`
// carrying the pre-Back segment query, so a reload or bookmark silently undid
// the Back.
import { describe, expect, test } from "bun:test";

// layout-codec pulls in router.ts, which reads `location` at module scope; bun
// has no DOM. Same shim as router.test.ts (see the comment there).
(globalThis as { location?: unknown }).location ??= {
  pathname: "/",
  search: "",
  href: "http://localhost/",
};

const { attachEmbedUrlChange, detachEmbedUrlChange } = await import("./layout-codec");

/** The minimum of an iframe: a contentWindow you can dispatch events on. */
function fakeIframe() {
  const win = new EventTarget() as unknown as Window;
  return { iframe: { contentWindow: win } as HTMLIFrameElement, win };
}

describe("attachEmbedUrlChange", () => {
  test("re-syncs on a pane traversal, not only on a pane write", () => {
    const { iframe, win } = fakeIframe();
    let calls = 0;
    const hook = attachEmbedUrlChange(iframe, () => calls++);
    expect(hook).not.toBeNull();

    win.dispatchEvent(new Event("fused:urlchange"));
    expect(calls).toBe(1);
    win.dispatchEvent(new Event("popstate")); // Back inside the pane
    expect(calls).toBe(2);
  });

  test("detach removes both listeners", () => {
    const { iframe, win } = fakeIframe();
    let calls = 0;
    const hook = attachEmbedUrlChange(iframe, () => calls++);
    detachEmbedUrlChange(hook);

    win.dispatchEvent(new Event("fused:urlchange"));
    win.dispatchEvent(new Event("popstate"));
    expect(calls).toBe(0);
  });

  test("a window already hooked is not hooked twice", () => {
    const { iframe, win } = fakeIframe();
    let calls = 0;
    attachEmbedUrlChange(iframe, () => calls++);
    expect(attachEmbedUrlChange(iframe, () => calls++)).toBeNull();

    win.dispatchEvent(new Event("popstate"));
    expect(calls).toBe(1);
  });
});
