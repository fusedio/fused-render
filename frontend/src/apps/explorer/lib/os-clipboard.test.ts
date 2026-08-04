// The other direction: files copied in Finder/Explorer/Nautilus become the
// app's clipboard when the user returns to the app.
import { afterEach, beforeEach, expect, mock, test } from "bun:test";

import { getClipboard, getLastSeenOsToken, setClipboard, setLastSeenOsToken } from "@apps/explorer/lib/fs-clipboard";
import { reconcileOsClipboard } from "@apps/explorer/lib/os-clipboard";

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

// ---- review findings --------------------------------------------------------

test("a copy made while the reconcile's read is in flight is not overwritten", async () => {
  // Found in review. The token check happened only AFTER the await, so a read
  // that started before the user copied still adopted its stale paths on the
  // way back — silently discarding the gesture they had just made.
  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  globalThis.fetch = mock(async (_url: string, init: RequestInit = {}) => {
    if (init.method === "POST") {
      return new Response(JSON.stringify({ token: "written", supported: true }), { status: 200 });
    }
    await held;
    return new Response(
      JSON.stringify({ paths: ["/from/finder"], token: "finder", supported: true }),
      { status: 200 }
    );
  }) as unknown as typeof fetch;

  const reconcile = reconcileOsClipboard();
  setClipboard({ paths: ["/in/app"], op: "cut" });   // the user, mid-read
  release!();
  await reconcile;

  expect(getClipboard()).toEqual({ paths: ["/in/app"], op: "cut" });
  // And the stale token was NOT recorded: doing so would make the next
  // reconcile treat that Finder copy as already seen and skip it for good.
  expect(getLastSeenOsToken()).not.toBe("finder");
});

test("a superseded mirror-write does not rewind the last-seen token", async () => {
  // The same race from the other side: the write is fire-and-forget, so its
  // response can land after a second copy has already published a newer token.
  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  let post = 0;
  globalThis.fetch = mock(async (_url: string, init: RequestInit = {}) => {
    if (init.method === "POST") {
      post += 1;
      if (post === 1) {
        await held;
        return new Response(JSON.stringify({ token: "first", supported: true }), { status: 200 });
      }
      return new Response(JSON.stringify({ token: "second", supported: true }), { status: 200 });
    }
    return new Response(JSON.stringify({ paths: [], token: "", supported: true }), { status: 200 });
  }) as unknown as typeof fetch;

  setClipboard({ paths: ["/a"], op: "copy" });
  setClipboard({ paths: ["/b"], op: "copy" });
  await new Promise((r) => setTimeout(r, 0));
  release!();
  await new Promise((r) => setTimeout(r, 0));

  expect(getLastSeenOsToken()).toBe("second");
});

test("a cut made while a copy's mirror-write is in flight still protects that cut", async () => {
  // Found in review, and a regression introduced by the guard in the test
  // above: gating the write's response on `clipboardEpoch` meant ANY later
  // set superseded it — including a cut, which publishes nothing. The copy's
  // token was then never recorded, so the reconcile below saw the OS copy as
  // never-seen and adopted it straight over the cut.
  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  globalThis.fetch = mock(async (_url: string, init: RequestInit = {}) => {
    if (init.method === "POST") {
      await held;
      return new Response(JSON.stringify({ token: "copied", supported: true }), { status: 200 });
    }
    // The OS still holds what our own copy put there.
    return new Response(
      JSON.stringify({ paths: ["/a"], token: "copied", supported: true }),
      { status: 200 }
    );
  }) as unknown as typeof fetch;

  setClipboard({ paths: ["/a"], op: "copy" });      // mirror-write in flight
  setClipboard({ paths: ["/b"], op: "cut" });        // user cuts, publishing nothing
  release!();
  await new Promise((r) => setTimeout(r, 0));

  // The write's token describes the SYSTEM clipboard, which the cut never
  // touched, so it is still valid and must have been recorded.
  expect(getLastSeenOsToken()).toBe("copied");

  await reconcileOsClipboard();
  expect(getClipboard()).toEqual({ paths: ["/b"], op: "cut" });
});

test("a late mirror-write does not rewind a token the reconcile recorded after it", async () => {
  // Found in review. Both writers of `lastSeenOsToken` compute across an
  // await, so they can finish out of order: a copy's mirror-write ISSUED
  // before a reconcile's read can be DELIVERED after it. Guarding each writer
  // privately let the older one win, rewinding the fresher foreign token —
  // and the next focus then re-adopted that foreign clipboard over whatever
  // the user had done since.
  let release: (() => void) | undefined;
  const held = new Promise<void>((r) => (release = r));
  globalThis.fetch = mock(async (_url: string, init: RequestInit = {}) => {
    if (init.method === "POST") {
      await held;                       // our write's response is slow
      return new Response(JSON.stringify({ token: "ours", supported: true }), { status: 200 });
    }
    // Meanwhile the OS clipboard holds a Finder copy made after our write.
    return new Response(
      JSON.stringify({ paths: ["/from/finder"], token: "finder", supported: true }),
      { status: 200 }
    );
  }) as unknown as typeof fetch;

  setClipboard({ paths: ["/ours"], op: "copy" });   // mirror-write in flight
  await reconcileOsClipboard();                      // reads + records "finder"
  expect(getLastSeenOsToken()).toBe("finder");

  release!();                                        // our older write lands now
  await new Promise((r) => setTimeout(r, 0));

  // It started BEFORE the reconcile, so it may not overwrite what the
  // reconcile saw. Otherwise the Finder copy reads as unseen next time.
  expect(getLastSeenOsToken()).toBe("finder");

  // Concretely: the user cuts, and the already-seen Finder copy must not
  // clobber it on the next return to the app.
  setClipboard({ paths: ["/cut/me"], op: "cut" });
  await reconcileOsClipboard();
  expect(getClipboard()).toEqual({ paths: ["/cut/me"], op: "cut" });
});
