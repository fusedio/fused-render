// The in-app clipboard's OS side-effect: a copy also publishes the paths to
// the system clipboard, so ⌘V in Finder/Explorer pastes the real files.
//
// `fetch` is stubbed rather than the api module, because the whole point of
// the fire-and-forget posture is what happens when the *request* fails.
import { afterEach, beforeEach, expect, mock, test } from "bun:test";

import { getClipboard, setClipboard } from "./fs-clipboard";

interface Call {
  url: string;
  body: unknown;
}

let calls: Call[] = [];
const realFetch = globalThis.fetch;

function stubFetch(impl?: (url: string, init: RequestInit) => Promise<Response>) {
  globalThis.fetch = mock(async (url: string, init: RequestInit = {}) => {
    calls.push({ url: String(url), body: init.body ? JSON.parse(String(init.body)) : null });
    if (impl) return impl(String(url), init);
    return new Response(JSON.stringify({ token: "tok", supported: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
}

// The write is fired without being awaited, so a test has to yield once for
// the microtask + the stubbed response to land before asserting.
const settle = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  calls = [];
  setClipboard(null);
  stubFetch();
});

afterEach(() => {
  globalThis.fetch = realFetch;
  setClipboard(null);
});

const clipboardCalls = () => calls.filter((c) => c.url === "/api/clipboard/files");

test("a copy publishes its paths to the OS clipboard", async () => {
  setClipboard({ paths: ["/a/b.csv", "/a/dir"], op: "copy" });
  await settle();
  expect(clipboardCalls()).toEqual([
    { url: "/api/clipboard/files", body: { paths: ["/a/b.csv", "/a/dir"] } },
  ]);
});

test("a copy is stored in-app immediately, before the OS write resolves", () => {
  setClipboard({ paths: ["/a/b.csv"], op: "copy" });
  expect(getClipboard()).toEqual({ paths: ["/a/b.csv"], op: "copy" });
});

test("a cut stays in-app only", async () => {
  // Cut is out of scope on purpose: no platform exposes a reliable
  // cut-vs-copy flag on read, so we never publish one.
  setClipboard({ paths: ["/a/b.csv"], op: "cut" });
  await settle();
  expect(clipboardCalls()).toEqual([]);
});

test("clearing the clipboard does not touch the OS clipboard", async () => {
  setClipboard(null);
  await settle();
  expect(clipboardCalls()).toEqual([]);
});

test("a rejected OS write leaves the in-app clipboard intact", async () => {
  stubFetch(async () => new Response(JSON.stringify({ error: "nope" }), { status: 500 }));
  setClipboard({ paths: ["/a/b.csv"], op: "copy" });
  await settle();
  expect(getClipboard()).toEqual({ paths: ["/a/b.csv"], op: "copy" });
});

test("a network failure on the OS write is swallowed", async () => {
  stubFetch(async () => {
    throw new Error("offline");
  });
  setClipboard({ paths: ["/a/b.csv"], op: "copy" });
  await settle();
  expect(getClipboard()).toEqual({ paths: ["/a/b.csv"], op: "copy" });
});

test("the token from a successful write becomes the last-seen token", async () => {
  const { getLastSeenOsToken } = await import("./fs-clipboard");
  setClipboard({ paths: ["/a/b.csv"], op: "copy" });
  await settle();
  // Recording our own write as "last seen" is what stops the next focus-time
  // reconcile from re-adopting the clipboard we just wrote.
  expect(getLastSeenOsToken()).toBe("tok");
});

test("an unsupported bridge does not record a token", async () => {
  const { getLastSeenOsToken, setLastSeenOsToken } = await import("./fs-clipboard");
  setLastSeenOsToken("previous");
  stubFetch(
    async () =>
      new Response(JSON.stringify({ token: "", supported: false }), { status: 200 })
  );
  setClipboard({ paths: ["/a/b.csv"], op: "copy" });
  await settle();
  expect(getLastSeenOsToken()).toBe("previous");
});

// ---- review findings --------------------------------------------------------

test("bookkeeping after a delete or a rename does not republish to the OS", async () => {
  // Found in review. Both repair OUR reference and keep op: "copy", so the
  // default mirror republished them — rewriting a clipboard the user may not
  // have put there, and on Linux stealing selection ownership from the file
  // manager that legitimately held it.
  // fs-actions reaches the router, which reads `location` at module scope —
  // hence the stub and the dynamic import. These two files are the whole
  // frontend suite and carry no DOM; adding one for a two-line assertion is a
  // worse trade than this.
  (globalThis as { location?: unknown }).location = new URL("http://x/");
  const { remapClipboardPath } = await import("./fs-actions");

  setClipboard({ paths: ["/a/b.csv", "/a/c.csv"], op: "copy" });
  await settle();
  const afterCopy = clipboardCalls().length;

  remapClipboardPath("/a/b.csv", "/a/renamed.csv");
  await settle();
  expect(getClipboard()?.paths).toEqual(["/a/renamed.csv", "/a/c.csv"]);
  expect(clipboardCalls().length).toBe(afterCopy);
});
