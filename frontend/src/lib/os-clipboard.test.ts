// The other direction: files copied in Finder/Explorer/Nautilus become the
// app's clipboard when the user returns to the app.
import { afterEach, beforeEach, expect, mock, test } from "bun:test";

import { getClipboard, getLastSeenOsToken, setClipboard, setLastSeenOsToken } from "./fs-clipboard";
import { reconcileOsClipboard } from "./os-clipboard";

const realFetch = globalThis.fetch;
let writes: unknown[] = [];

// Stub only the GET; the POST (an in-app copy mirroring out) is answered with
// a benign success so setClipboard's fire-and-forget write never throws.
function stubClipboard(body: { paths: string[]; token: string; supported: boolean }) {
  globalThis.fetch = mock(async (url: string, init: RequestInit = {}) => {
    if (init.method === "POST") {
      writes.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify({ token: "written", supported: true }), { status: 200 });
    }
    return new Response(JSON.stringify(body), { status: 200 });
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  writes = [];
  setClipboard(null);
  setLastSeenOsToken("");
});

afterEach(() => {
  globalThis.fetch = realFetch;
  setClipboard(null);
  setLastSeenOsToken("");
});

test("adopts files copied in the native file manager", async () => {
  stubClipboard({ paths: ["/a/b.csv", "/a/dir"], token: "t1", supported: true });
  await reconcileOsClipboard();
  expect(getClipboard()).toEqual({ paths: ["/a/b.csv", "/a/dir"], op: "copy" });
  // Adopting must not echo back out to the OS clipboard — it's already there.
  expect(writes).toEqual([]);
  expect(getLastSeenOsToken()).toBe("t1");
});

test("an adopted clipboard is always a copy, never a cut", async () => {
  stubClipboard({ paths: ["/a/b.csv"], token: "t1", supported: true });
  await reconcileOsClipboard();
  expect(getClipboard()?.op).toBe("copy");
});

test("skips an unchanged token", async () => {
  setLastSeenOsToken("t1");
  setClipboard({ paths: ["/x/y"], op: "cut" });
  writes = []; // the cut above writes nothing, but be explicit
  stubClipboard({ paths: ["/a/b.csv"], token: "t1", supported: true });
  await reconcileOsClipboard();
  // A pending in-app cut survives a focus change: the OS clipboard hasn't
  // changed, so there is nothing to adopt.
  expect(getClipboard()).toEqual({ paths: ["/x/y"], op: "cut" });
});

test("skips an empty OS clipboard", async () => {
  setClipboard({ paths: ["/x/y"], op: "cut" });
  stubClipboard({ paths: [], token: "", supported: true });
  await reconcileOsClipboard();
  expect(getClipboard()).toEqual({ paths: ["/x/y"], op: "cut" });
});

test("no-ops when the bridge is unsupported", async () => {
  setClipboard({ paths: ["/x/y"], op: "cut" });
  stubClipboard({ paths: ["/a/b.csv"], token: "t1", supported: false });
  await reconcileOsClipboard();
  expect(getClipboard()).toEqual({ paths: ["/x/y"], op: "cut" });
  expect(getLastSeenOsToken()).toBe("");
});

test("a failed read is swallowed and changes nothing", async () => {
  setClipboard({ paths: ["/x/y"], op: "copy" });
  globalThis.fetch = mock(async () => {
    throw new Error("offline");
  }) as unknown as typeof fetch;
  await reconcileOsClipboard();
  expect(getClipboard()).toEqual({ paths: ["/x/y"], op: "copy" });
});

test("a second reconcile after a genuine change adopts again", async () => {
  stubClipboard({ paths: ["/a/one.csv"], token: "t1", supported: true });
  await reconcileOsClipboard();
  stubClipboard({ paths: ["/a/two.csv"], token: "t2", supported: true });
  await reconcileOsClipboard();
  expect(getClipboard()).toEqual({ paths: ["/a/two.csv"], op: "copy" });
  expect(getLastSeenOsToken()).toBe("t2");
});

test("our own copy is not re-adopted on the next focus", async () => {
  // setClipboard records the token it wrote as last-seen, so the reconcile
  // that follows a return to the app sees no change.
  globalThis.fetch = mock(async (_url: string, init: RequestInit = {}) => {
    if (init.method === "POST") {
      return new Response(JSON.stringify({ token: "mine", supported: true }), { status: 200 });
    }
    return new Response(
      JSON.stringify({ paths: ["/a/b.csv"], token: "mine", supported: true }),
      { status: 200 }
    );
  }) as unknown as typeof fetch;

  setClipboard({ paths: ["/a/b.csv"], op: "copy" });
  await new Promise((r) => setTimeout(r, 0));
  setClipboard({ paths: ["/a/b.csv"], op: "cut" }); // user then cuts something
  await reconcileOsClipboard();
  expect(getClipboard()).toEqual({ paths: ["/a/b.csv"], op: "cut" });
});
