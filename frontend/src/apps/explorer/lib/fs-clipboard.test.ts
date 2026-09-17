// The in-app clipboard's OS side-effect: a copy also publishes the paths to
// the system clipboard, so ⌘V in Finder/Explorer pastes the real files.
//
// `fetch` is stubbed rather than the api module, because the whole point of
// the fire-and-forget posture is what happens when the *request* fails.
import { afterEach, beforeEach, describe, expect, it, mock, test } from "bun:test";

// bun's test runner has no DOM, so the store's one browser dependency is
// supplied here — a real Storage-shaped object, installed BEFORE the module is
// imported, because the seed runs at load (same trick as side-store.test.ts).
const cells = new Map<string, string>();
Object.defineProperty(globalThis, "sessionStorage", {
  configurable: true,
  writable: true,
  value: {
    getItem: (k: string) => (cells.has(k) ? (cells.get(k) as string) : null),
    setItem: (k: string, v: string) => void cells.set(k, String(v)),
    removeItem: (k: string) => void cells.delete(k),
    clear: () => cells.clear(),
    key: (i: number) => [...cells.keys()][i] ?? null,
    get length() {
      return cells.size;
    },
  } as Storage,
});

const {
  CLIPBOARD_STORAGE_KEY,
  getClipboard,
  parseStoredClipboardState,
  setClipboard,
  setLastSeenOsToken,
} = await import("@apps/explorer/lib/fs-clipboard");

/** A fresh copy of the module, so its load-time seed runs against whatever is
 *  in storage now. Bun caches by specifier, so the query string is what makes
 *  each import a new module instance (same trick as side-store.test.ts). */
let freshSeq = 0;
async function reloadClipboard() {
  return (await import(
    `@apps/explorer/lib/fs-clipboard?seed=${++freshSeq}`
  )) as typeof import("@apps/explorer/lib/fs-clipboard");
}

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
  cells.clear();
  setClipboard(null);
  setLastSeenOsToken("");
  stubFetch();
});

afterEach(() => {
  globalThis.fetch = realFetch;
  setClipboard(null);
  setLastSeenOsToken("");
  cells.clear();
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
  const { getLastSeenOsToken } = await import("@apps/explorer/lib/fs-clipboard");
  setClipboard({ paths: ["/a/b.csv"], op: "copy" });
  await settle();
  // Recording our own write as "last seen" is what stops the next focus-time
  // reconcile from re-adopting the clipboard we just wrote.
  expect(getLastSeenOsToken()).toBe("tok");
});

test("an unsupported bridge does not record a token", async () => {
  const { getLastSeenOsToken, setLastSeenOsToken } = await import("@apps/explorer/lib/fs-clipboard");
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
  const { remapClipboardPath } = await import("@apps/explorer/lib/fs-actions");

  setClipboard({ paths: ["/a/b.csv", "/a/c.csv"], op: "copy" });
  await settle();
  const afterCopy = clipboardCalls().length;

  remapClipboardPath("/a/b.csv", "/a/renamed.csv");
  await settle();
  expect(getClipboard()?.paths).toEqual(["/a/renamed.csv", "/a/c.csv"]);
  expect(clipboardCalls().length).toBe(afterCopy);
});

// ---- sessionStorage persistence --------------------------------------------

describe("parseStoredClipboardState", () => {
  it("nothing stored is the empty state, not an error", () => {
    expect(parseStoredClipboardState(null)).toEqual({ clipboard: null, lastSeenOsToken: "" });
    expect(parseStoredClipboardState(undefined)).toEqual({ clipboard: null, lastSeenOsToken: "" });
    expect(parseStoredClipboardState("")).toEqual({ clipboard: null, lastSeenOsToken: "" });
  });

  it("unparsable JSON is the empty state", () => {
    expect(parseStoredClipboardState("not json")).toEqual({ clipboard: null, lastSeenOsToken: "" });
  });

  it("a well-formed pair round-trips", () => {
    const raw = JSON.stringify({ clipboard: { paths: ["/a"], op: "cut" }, lastSeenOsToken: "tok" });
    expect(parseStoredClipboardState(raw)).toEqual({
      clipboard: { paths: ["/a"], op: "cut" },
      lastSeenOsToken: "tok",
    });
  });

  it("a non-array paths field is rejected — clipboard falls back to null", () => {
    const raw = JSON.stringify({ clipboard: { paths: "/a", op: "cut" }, lastSeenOsToken: "tok" });
    expect(parseStoredClipboardState(raw)).toEqual({ clipboard: null, lastSeenOsToken: "tok" });
  });

  it("an op outside copy/cut is rejected", () => {
    const raw = JSON.stringify({ clipboard: { paths: ["/a"], op: "move" }, lastSeenOsToken: "tok" });
    expect(parseStoredClipboardState(raw)).toEqual({ clipboard: null, lastSeenOsToken: "tok" });
  });

  it("an empty paths array is rejected — the invariant is at least one path", () => {
    const raw = JSON.stringify({ clipboard: { paths: [], op: "copy" }, lastSeenOsToken: "tok" });
    expect(parseStoredClipboardState(raw)).toEqual({ clipboard: null, lastSeenOsToken: "tok" });
  });

  it("a non-string lastSeenOsToken falls back to empty", () => {
    const raw = JSON.stringify({ clipboard: null, lastSeenOsToken: 7 });
    expect(parseStoredClipboardState(raw)).toEqual({ clipboard: null, lastSeenOsToken: "" });
  });
});

describe("fs-clipboard persistence (Fix 2)", () => {
  it("a copy survives a reload", async () => {
    setClipboard({ paths: ["/a/b.csv"], op: "copy" });
    await settle(); // let the mirror-write's token land before we snapshot storage
    const fresh = await reloadClipboard();
    expect(fresh.getClipboard()).toEqual({ paths: ["/a/b.csv"], op: "copy" });
  });

  it("a cut survives a reload", () => {
    setClipboard({ paths: ["/a/b.csv", "/a/dir"], op: "cut" });
    // A cut never mirrors to the OS, so there is no write to await.
    return reloadClipboard().then((fresh) => {
      expect(fresh.getClipboard()).toEqual({ paths: ["/a/b.csv", "/a/dir"], op: "cut" });
    });
  });

  it("the last-seen OS token is restored alongside the clipboard, not just the clipboard", async () => {
    const { setLastSeenOsToken } = await import("@apps/explorer/lib/fs-clipboard");
    setLastSeenOsToken("t1");
    setClipboard({ paths: ["/a/b.csv"], op: "cut" });
    const fresh = await reloadClipboard();
    // This is the pairing this store exists to guarantee: without it a fresh
    // document starts the token at "", and the mount-time reconcile would
    // read the OS clipboard as unseen and adopt it straight over this cut.
    expect(fresh.getLastSeenOsToken()).toBe("t1");
    expect(fresh.getClipboard()).toEqual({ paths: ["/a/b.csv"], op: "cut" });
  });

  it("clearing the clipboard persists the clear — an old cut does not reappear on reload", () => {
    setClipboard({ paths: ["/a/b.csv"], op: "cut" });
    setClipboard(null);
    return reloadClipboard().then((fresh) => {
      expect(fresh.getClipboard()).toBeNull();
    });
  });

  it("with nothing stored a fresh module starts empty", async () => {
    const fresh = await reloadClipboard();
    expect(fresh.getClipboard()).toBeNull();
    expect(fresh.getLastSeenOsToken()).toBe("");
  });

  it("a corrupted stored entry cannot produce an invalid clipboard", async () => {
    sessionStorage.setItem(CLIPBOARD_STORAGE_KEY, JSON.stringify({ clipboard: { paths: [], op: "cut" } }));
    const fresh = await reloadClipboard();
    expect(fresh.getClipboard()).toBeNull();
  });

  it("blocked storage degrades to in-memory only — never throws", () => {
    const real = globalThis.sessionStorage;
    Object.defineProperty(globalThis, "sessionStorage", {
      configurable: true,
      get() {
        throw new Error("SecurityError");
      },
    });
    try {
      expect(() => setClipboard({ paths: ["/a"], op: "cut" })).not.toThrow();
      expect(getClipboard()).toEqual({ paths: ["/a"], op: "cut" });
    } finally {
      Object.defineProperty(globalThis, "sessionStorage", {
        configurable: true,
        value: real,
        writable: true,
      });
    }
  });
});
