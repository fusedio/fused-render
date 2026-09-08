// The app-state channels: the block's grammar, the two trims (12 on the wire, 50
// in the buffer), the outline's caps and elision reporting, and the DOM offload.
//
// The frame is a hand-built stub rather than a real iframe: bun has no DOM, and
// what is under test is the SHAPE of the snapshot, not the browser's framing.
// Every read in `appState.ts` is same-origin and guarded, so a stub that answers
// the same members exercises the same paths (T:4858-4877).
import { describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const {
  appParamsOf,
  clipText,
  createAppStateWatcher,
  fmtLogArg,
  outlineNode,
  searchParamsOf,
  APP_STATE_MAX_LOGS,
  APP_STATE_MAX_NODES,
  APP_STATE_MAX_NODE_TEXT,
  APP_STATE_MAX_TEXT,
  APP_STATE_TAG,
  APP_STATE_WIRE_LOGS,
} = await import("./appState");
const { APP_STATE_UNREADABLE } = await import("./paneUrl");

// ── a minimal element/document, enough for the outline walk ──────────────────

interface FakeNode {
  nodeType: number;
  nodeValue?: string;
}
interface FakeEl {
  tagName: string;
  id?: string;
  className?: string;
  children: FakeEl[];
  childNodes: (FakeNode | FakeEl)[];
  textContent?: string;
  attrs?: Record<string, string>;
  hasAttribute(name: string): boolean;
}

function el(tag: string, opts: Partial<FakeEl> & { text?: string } = {}): FakeEl {
  const kids = opts.children ?? [];
  const node: FakeEl = {
    tagName: tag.toUpperCase(),
    id: opts.id,
    className: opts.className,
    children: kids,
    childNodes: [...(opts.text ? [{ nodeType: 3, nodeValue: opts.text }] : []), ...kids],
    textContent: opts.textContent ?? opts.text,
    attrs: opts.attrs,
    hasAttribute(name: string) {
      return !!this.attrs && name in this.attrs;
    },
  };
  return node;
}

// The stub is structurally what the walk reads; the cast is the one place this
// suite admits it is not a browser.
const asEl = (e: FakeEl) => e as unknown as Element;

function frameStub(win: unknown): () => HTMLIFrameElement | null {
  const frame = { isConnected: true, contentWindow: win } as unknown as HTMLIFrameElement;
  return () => frame;
}

function windowStub(over: Record<string, unknown> = {}): unknown {
  const body = el("body", { children: [el("h1", { text: "Hi" })] });
  return {
    document: { title: "My app", body: asEl(body) },
    location: { href: "http://localhost/render", pathname: "/render", search: "?zoom=3" },
    console: { error() {}, warn() {} },
    addEventListener() {},
    removeEventListener() {},
    ...over,
  };
}

/** A window that RECORDS what was wired to it — the only way to see the other
 *  half of `dispose`, since a listener left behind is invisible in the snapshot
 *  and shows up as a leak instead. */
function listeningWindow(): {
  win: unknown;
  wired: string[];
  console_: { error: (...a: unknown[]) => void; warn: (...a: unknown[]) => void };
} {
  const wired: string[] = [];
  const console_ = { error(..._a: unknown[]) {}, warn(..._a: unknown[]) {} };
  const win = windowStub({
    console: console_,
    addEventListener(type: string) {
      wired.push(type);
    },
    removeEventListener(type: string) {
      const i = wired.indexOf(type);
      if (i >= 0) wired.splice(i, 1);
    },
  });
  return { win, wired, console_ };
}

// ── clipText / fmtLogArg ─────────────────────────────────────────────────────

describe("clipText", () => {
  test("appends an ellipsis so the elision is visible in the payload", () => {
    expect(clipText("abcdef", 3)).toBe("abc…");
    expect(clipText("abc", 3)).toBe("abc");
  });
  test("null and undefined are the empty string, never the words", () => {
    expect(clipText(null, 10)).toBe("");
    expect(clipText(undefined, 10)).toBe("");
  });
});

describe("fmtLogArg — console args are anything at all, cross-realm included", () => {
  test("a string is itself", () => expect(fmtLogArg("boom")).toBe("boom"));
  test("a cross-realm error is duck-typed on `message` (instanceof is useless)", () => {
    expect(fmtLogArg({ message: "not this realm's Error" })).toBe("not this realm's Error");
  });
  test("a plain object is JSON", () => expect(fmtLogArg({ a: 1 })).toBe('{"a":1}'));
  test("a circular object degrades to String(), never throws", () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(fmtLogArg(cyclic)).toBe("[object Object]");
  });
  test("undefined stringifies (JSON.stringify would answer undefined)", () => {
    expect(fmtLogArg(undefined)).toBe("undefined");
  });
});

// ── params ───────────────────────────────────────────────────────────────────

describe("appParamsOf — the chat's own bookkeeping is never the app's state", () => {
  test("drops CHAT_PARAMS and keeps the app's own", () => {
    expect(
      appParamsOf({ zoom: "3", session_id: "s1", split: "70", leftmode: "code", path: "/t/x", msg: "u1" }),
    ).toEqual({ zoom: "3" });
  });
  test("clips a long value rather than dropping it", () => {
    expect(appParamsOf({ q: "x".repeat(300) }).q).toBe("x".repeat(200) + "…");
  });
  test("null/undefined is an empty object", () => {
    expect(appParamsOf(null)).toEqual({});
  });
  test("searchParamsOf is the pre-boot fallback for a page with no runtime yet", () => {
    expect(searchParamsOf("?a=1&b=two")).toEqual({ a: "1", b: "two" });
    expect(searchParamsOf("")).toEqual({});
  });
});

// ── the outline ──────────────────────────────────────────────────────────────

describe("outlineNode", () => {
  test("tag, id, class and OWN text — not the subtree's", () => {
    const node = outlineNode(
      asEl(el("div", { id: "root", className: " card wide ", text: "Title", children: [el("p", { text: "Body" })] })),
      0,
      null,
      null,
    );
    expect(node.tag).toBe("div");
    expect(node.id).toBe("root");
    expect(node.class).toBe("card wide");
    expect(node.text).toBe("Title");
    expect(node.children?.[0].text).toBe("Body");
  });

  test("script/style bodies are listed, never quoted from", () => {
    const node = outlineNode(asEl(el("script", { text: "const secret = 1" })), 0, null, null);
    expect(node.tag).toBe("script");
    expect(node.text).toBeUndefined();
  });

  test("node text is clipped tighter than a console line (it is paid 60 times)", () => {
    const node = outlineNode(asEl(el("p", { text: "y".repeat(400) })), 0, null, null);
    expect(node.text).toBe("y".repeat(APP_STATE_MAX_NODE_TEXT) + "…");
    expect(APP_STATE_MAX_NODE_TEXT).toBeLessThan(APP_STATE_MAX_TEXT);
  });

  test("depth is capped and the elision is REPORTED", () => {
    let deep = el("span", { text: "leaf" });
    for (let i = 0; i < 8; i++) deep = el("div", { children: [deep] });
    const node = outlineNode(asEl(deep), 0, null, null);
    let cur = node;
    let depth = 0;
    while (cur.children?.[0]) {
      cur = cur.children[0];
      depth++;
    }
    expect(depth).toBe(4);
    expect(cur.truncated).toMatch(/deeper element\(s\) not shown/);
  });

  test("the budget is shared across the WHOLE walk, and siblings dropped are reported", () => {
    const kids = Array.from({ length: 100 }, (_, i) => el("li", { text: "row " + i }));
    const node = outlineNode(asEl(el("ul", { children: kids })), 0, null, null);
    expect(node.children?.length).toBe(APP_STATE_MAX_NODES);
    expect(node.truncated).toBe(100 - APP_STATE_MAX_NODES + " sibling(s) not shown");
  });

  test("our own pin layer is skipped WITHOUT spending budget", () => {
    const node = outlineNode(
      asEl(
        el("body", {
          children: [el("div", { attrs: { "data-fused-annotate": "" } }), el("main", { text: "app" })],
        }),
      ),
      0,
      null,
      null,
    );
    expect(node.children?.map((c) => c.tag)).toEqual(["main"]);
  });

  test("`path` comes from the annotation layer's own identifier, and is absent without it", () => {
    const doc = {} as Document;
    const plain = outlineNode(asEl(el("h1", { text: "x" })), 0, null, doc);
    expect(plain.path).toBeUndefined();
    const withPath = outlineNode(asEl(el("h1", { text: "x" })), 0, null, doc, () => "h1:nth-of-type(1)");
    expect(withPath.path).toBe("h1:nth-of-type(1)");
  });
});

// ── the snapshot, and push vs pull ───────────────────────────────────────────

describe("snapshot", () => {
  test("a readable pane reports title, url, params and the outline", () => {
    const w = createAppStateWatcher(frameStub(windowStub()));
    w.setEntry("/w/app/main.html");
    const s = w.snapshot();
    expect(s?.entry).toBe("/w/app/main.html");
    expect(s?.title).toBe("My app");
    expect(s?.url).toBe("/render?zoom=3");
    expect(s?.params).toEqual({ zoom: "3" });
    expect(s?.dom?.tag).toBe("body");
    expect(s?.unreadable).toBeUndefined();
  });

  test("the app's OWN params view wins over its query string", () => {
    const w = createAppStateWatcher(
      frameStub(windowStub({ fused: { params: { getAll: () => ({ zoom: "9", session_id: "s" }) } } })),
    );
    expect(w.snapshot()?.params).toEqual({ zoom: "9" });
  });

  test("about:blank is NOT an app: null, so a turn looks like one from before this feature", () => {
    const w = createAppStateWatcher(
      frameStub(windowStub({ location: { href: "about:blank", pathname: "", search: "" } })),
    );
    expect(w.snapshot()).toBeNull();
  });

  test("no frame at all is also null — `unreadable` alone is not knowledge", () => {
    const w = createAppStateWatcher(() => null);
    expect(w.snapshot()).toBeNull();
  });

  test("but a console line makes the same unreadable state worth reporting", () => {
    const w = createAppStateWatcher(() => null);
    w.pushLog("error", "the left pane could not open the preview: boom");
    const s = w.snapshot();
    expect(s?.unreadable).toBe(APP_STATE_UNREADABLE);
    expect(s?.console?.length).toBe(1);
  });

  test("a throwing document degrades to LESS state, never to a thrown send", () => {
    const w = createAppStateWatcher(
      frameStub({
        document: {
          title: "x",
          get body(): never {
            throw new Error("detached");
          },
        },
        location: { href: "http://localhost/render", pathname: "/render", search: "" },
      }),
    );
    // Nothing was learned, so the read is still not worth a block — the failed
    // read is not itself knowledge (T:5070-5077). The sentence rides along the
    // moment there IS something else to say, which the console usually is.
    expect(w.snapshot()).toBeNull();
    w.pushLog("error", "boom");
    expect(w.snapshot()?.unreadable).toBe("could not read the pane's document: detached");
  });
});

describe("pull — never null, because the model's tool call is BLOCKED on it", () => {
  test("an unreadable pane answers the explicit sentence, with `entry` if known", () => {
    const w = createAppStateWatcher(() => null);
    w.setEntry("/w/app/main.html");
    expect(w.pull()).toEqual({ unreadable: APP_STATE_UNREADABLE, entry: "/w/app/main.html" });
  });
  test("title/url/params/dom are deliberately absent: there is no honest source", () => {
    const out = createAppStateWatcher(() => null).pull();
    expect(out.title).toBeUndefined();
    expect(out.url).toBeUndefined();
    expect(out.dom).toBeUndefined();
  });
});

describe("push — the console trim", () => {
  test("the buffer is a 50-line ring; the wire gets the newest 12", () => {
    const w = createAppStateWatcher(frameStub(windowStub()));
    for (let i = 0; i < 60; i++) w.pushLog("error", "line " + i);
    expect(w.logs().length).toBe(APP_STATE_MAX_LOGS);
    // The buffer holds the tail of what was pushed.
    expect(w.logs()[0].text).toBe("line 10");

    const pushed = w.push();
    expect(pushed?.console?.length).toBe(APP_STATE_WIRE_LOGS);
    expect(pushed?.console?.[0].text).toBe("line " + (60 - APP_STATE_WIRE_LOGS));
    expect(pushed?.consoleTruncated).toBe(
      APP_STATE_MAX_LOGS -
        APP_STATE_WIRE_LOGS +
        " older console line(s) not shown — call the app_state tool for the whole buffer",
    );
  });

  test("under the wire cap nothing is trimmed and nothing claims it was", () => {
    const w = createAppStateWatcher(frameStub(windowStub()));
    w.pushLog("warn", "one");
    const pushed = w.push();
    expect(pushed?.console?.length).toBe(1);
    expect(pushed?.consoleTruncated).toBeUndefined();
  });

  test("a COPY: the pull channel still hands over the whole buffer after a push", () => {
    const w = createAppStateWatcher(frameStub(windowStub()));
    for (let i = 0; i < 30; i++) w.pushLog("error", "line " + i);
    w.push();
    expect(w.pull().console?.length).toBe(30);
  });

  test("a line is clipped to the console cap, and the source/line ride along", () => {
    const w = createAppStateWatcher(frameStub(windowStub()));
    w.pushLog("error", "z".repeat(400), "/w/app/main.html", 12);
    const line = w.logs()[0];
    expect(line.text).toBe("z".repeat(APP_STATE_MAX_TEXT) + "…");
    expect(line.source).toBe("/w/app/main.html");
    expect(line.line).toBe(12);
  });
});

describe("watchApp — the reload is marked IN the buffer, never cleared", () => {
  test("the second load of a NEW document appends the reload line", () => {
    let win = windowStub();
    const frame = { isConnected: true, get contentWindow() { return win; } } as unknown as HTMLIFrameElement;
    const w = createAppStateWatcher(() => frame);
    w.pushLog("error", "before the edit");
    w.watchApp();
    expect(w.logs().map((l) => l.level)).toEqual(["error"]);
    // A same-origin navigation replaces the DOCUMENT, which is where the wrap
    // flag lives — so the next load re-wraps and counts.
    win = windowStub();
    w.watchApp();
    const levels = w.logs().map((l) => l.level);
    expect(levels).toEqual(["error", "reload"]);
    expect(w.logs()[0].text).toBe("before the edit");
  });

  test("the same document twice is ONE load: no phantom reload line", () => {
    const w = createAppStateWatcher(frameStub(windowStub()));
    w.watchApp();
    w.watchApp();
    expect(w.logs()).toEqual([]);
  });

  test("dispose puts the window back WHOLE: console unwrapped and both listeners off", () => {
    // The framed document outlives this watcher (a held-frame swap, a pane that
    // outlives one chat). `console.error` was always restored; `error` and
    // `unhandledrejection` were not, so a disposed watcher kept collecting into
    // a ring buffer nobody would ever read — and kept itself alive doing it
    // (Bugbot, PR #1061).
    const { win, wired, console_ } = listeningWindow();
    const ownError = console_.error;
    const w = createAppStateWatcher(frameStub(win));
    w.watchApp();
    expect(wired).toEqual(["error", "unhandledrejection"]);
    expect(console_.error).not.toBe(ownError);
    w.dispose();
    expect(wired).toEqual([]);
    expect(console_.error).toBe(ownError);
    // And the document is unclaimed, so a NEXT watcher can wrap the same one.
    const w2 = createAppStateWatcher(frameStub(win));
    w2.watchApp();
    expect(wired).toEqual(["error", "unhandledrejection"]);
  });

  test("the app's own console.error is captured AND called through", () => {
    const seen: { args: unknown[] | null } = { args: null };
    const win = windowStub({
      console: {
        error(...args: unknown[]) {
          seen.args = args;
        },
        warn() {},
      },
    }) as { console: { error: (...a: unknown[]) => void } };
    const w = createAppStateWatcher(frameStub(win));
    w.watchApp();
    win.console.error("chart failed", { message: "no data" });
    expect(w.logs()[0].level).toBe("error");
    expect(w.logs()[0].text).toBe("chart failed no data");
    expect(seen.args).toEqual(["chart failed", { message: "no data" }]);
  });
});

// ── the block ────────────────────────────────────────────────────────────────

describe("block — the grammar agent.py strips back out", () => {
  test("a null state is no block at all, and there is no trailing separator", () => {
    const w = createAppStateWatcher(() => null);
    expect(w.block(null)).toBe("");
  });

  test("the tag, the pane noun, and the preamble that names `dom`", () => {
    const w = createAppStateWatcher(frameStub(windowStub()), { paneNoun: () => "app" });
    const block = w.block({ title: "x" });
    expect(block.startsWith("<" + APP_STATE_TAG + ">\n")).toBe(true);
    expect(block.endsWith("\n</" + APP_STATE_TAG + ">")).toBe(true);
    expect(block).toContain("A snapshot of the app the user is looking at in the left pane");
    expect(block).toContain("`path` on each node is the same anchorPath");
    expect(block).toContain("call the app_state tool for a fresh read");
    // The DOM sentence belongs to the offloaded shape only.
    expect(block).not.toContain("dom_path");
    // The payload is the state, verbatim, on its own line.
    const lines = block.split("\n");
    expect(lines[lines.length - 2]).toBe(JSON.stringify({ title: "x" }));
  });

  test("the preamble describes whichever key ARRIVED, or the model hunts for one that is not there", () => {
    const w = createAppStateWatcher(() => null);
    const block = w.block({ dom_path: "/tmp/shots/appstate-1-1.json" });
    expect(block).toContain("The DOM outline is the JSON file at `dom_path`");
  });

  test("the noun defaults to the kind-free word before the decision lands", () => {
    expect(createAppStateWatcher(() => null).block({ title: "x" })).toContain("A snapshot of the preview");
  });
});

// ── the DOM offload ──────────────────────────────────────────────────────────

describe("offloadDom", () => {
  const state = () => ({ title: "x", dom: { tag: "body", text: "hello" } });

  test("the outline goes to a file in the shots dir and `dom` is dropped", async () => {
    const wrote: { path: string; body: string }[] = [];
    const w = createAppStateWatcher(() => null, {
      shotsDir: async () => "/tmp/shots/",
      upload: async (path, blob) => {
        wrote.push({ path, body: await blob.text() });
      },
      now: () => 1700000000000,
    });
    const out = await w.offloadDom(state());
    expect(out?.dom).toBeUndefined();
    expect(out?.dom_path).toMatch(/^\/tmp\/shots\/appstate-1700000000000-\d+\.json$/);
    expect(out?.title).toBe("x");
    // The FILE carries the outline alone, not the whole snapshot.
    expect(wrote[0].body).toBe(JSON.stringify(state().dom));
  });

  test("two sends in the same millisecond cannot share a file", async () => {
    const paths: string[] = [];
    const w = createAppStateWatcher(() => null, {
      shotsDir: async () => "/tmp/shots",
      upload: async (path) => {
        paths.push(path);
      },
      now: () => 42,
    });
    await w.offloadDom(state());
    await w.offloadDom(state());
    expect(paths[0]).not.toBe(paths[1]);
  });

  test("a failed write keeps the outline INLINE — never knowing less than before", async () => {
    const w = createAppStateWatcher(() => null, {
      shotsDir: async () => "/tmp/shots",
      upload: async () => {
        throw new Error("readonly");
      },
    });
    const out = await w.offloadDom(state());
    expect(out?.dom).toEqual(state().dom);
    expect(out?.dom_path).toBeUndefined();
  });

  test("no shots dir is the same fallback, not a throw", async () => {
    const w = createAppStateWatcher(() => null, {});
    expect((await w.offloadDom(state()))?.dom).toEqual(state().dom);
  });

  test("a snapshot with no outline is handed back untouched (and null stays null)", async () => {
    const w = createAppStateWatcher(() => null, { shotsDir: async () => "/tmp/shots" });
    expect(await w.offloadDom({ title: "x" })).toEqual({ title: "x" });
    expect(await w.offloadDom(null)).toBeNull();
  });

  test("the size threshold: below it the outline stays inline, above it it is offloaded", async () => {
    const big = { tag: "body", text: "y".repeat(500) };
    const mk = (min: number) =>
      createAppStateWatcher(() => null, {
        shotsDir: async () => "/tmp/shots",
        upload: async () => {},
        domOffloadMinBytes: min,
        now: () => 7,
      });
    expect((await mk(1024).offloadDom({ dom: big }))?.dom).toEqual(big);
    expect((await mk(64).offloadDom({ dom: big }))?.dom_path).toMatch(/^\/tmp\/shots\/appstate-7-\d+\.json$/);
    // The DEFAULT is T's own behaviour: always offload, however small.
    const always = createAppStateWatcher(() => null, {
      shotsDir: async () => "/tmp/shots",
      upload: async () => {},
      now: () => 7,
    });
    expect((await always.offloadDom({ dom: { tag: "body" } }))?.dom_path).toBeTruthy();
  });

  test("blockForSend is push → offload → block, in that order", async () => {
    const w = createAppStateWatcher(frameStub(windowStub()), {
      shotsDir: async () => "/tmp/shots",
      upload: async () => {},
      now: () => 5,
      paneNoun: () => "app",
    });
    for (let i = 0; i < 30; i++) w.pushLog("error", "line " + i);
    const block = await w.blockForSend();
    expect(block).toContain("The DOM outline is the JSON file at `dom_path`");
    const lines = block.split("\n");
    const payload = JSON.parse(lines[lines.length - 2]);
    expect(payload.dom).toBeUndefined();
    expect(payload.dom_path).toMatch(/^\/tmp\/shots\/appstate-5-\d+\.json$/);
    expect(payload.console.length).toBe(APP_STATE_WIRE_LOGS);
  });
});

// ── the pull channel's memos ──────────────────────────────────────────────────

describe("appStateTrim", () => {
  test("drops the OLDEST first, and only past the cap", async () => {
    const { appStateTrim, APP_STATE_MEMO_MAX } = await import("./useAppStateResponder");
    const memo = new Set<string>();
    for (let i = 0; i < APP_STATE_MEMO_MAX + 5; i++) memo.add("id" + i);
    appStateTrim(memo);
    expect(memo.size).toBe(APP_STATE_MEMO_MAX);
    expect(memo.has("id0")).toBe(false);
    expect(memo.has("id" + (APP_STATE_MEMO_MAX + 4))).toBe(true);
  });

  test("a map under the cap is untouched", async () => {
    const { appStateTrim } = await import("./useAppStateResponder");
    const memo = new Map<string, number>([["a", 1]]);
    appStateTrim(memo);
    expect(memo.size).toBe(1);
  });
});
