// The injected runtime's param→history rule (SPEC PR-3, D8/D99/D268), executed
// rather than grepped. The rule has three moving parts that only make sense
// together — the once-per-visit push, the `fusedParamEntry` flag that travels
// on the history entry, and (D268) the user-gesture gate that decides whether a
// write is allowed to spend the push at all — and the bug they exist to prevent
// is a SEQUENCE (seed → push → Back → re-seed → push again = a Back trap), not
// a string. So this file evaluates the real `fused_render/static/runtime.js`
// inside a hand-built sandbox whose `history` is a genuine entry stack with a
// working `back()`, and asserts on what the stack looks like afterwards.
//
// A hand-built sandbox rather than happy-dom/jsdom: neither implements session
// history as a stack you can walk back through (which is the entire subject),
// and the runtime touches so little DOM at module scope that the stubs below
// are shorter than the setup either library would need. Everything the runtime
// reaches for that the sandbox does NOT define falls through `with` to Bun's
// real globals (URL, URLSearchParams, Event, JSON, Date…), so only the
// browser-shaped pieces are faked.
import { beforeEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const RUNTIME = readFileSync(
  join(import.meta.dir, "../../../../fused_render/static/runtime.js"),
  "utf8"
);

type Entry = { url: string; state: any };

/** One browsing context: an entry stack, a location derived from it, and a log
 *  of every history operation in order (the same push/repl/pop trace the bug
 *  was originally diagnosed with in a real browser). */
function createContext(initialUrl: string) {
  const log: string[] = [];
  const entries: Entry[] = [{ url: initialUrl, state: null }];
  let idx = 0;
  const timers: Array<() => void> = [];

  const location = {
    get pathname() {
      return entries[idx].url.split("?")[0];
    },
    get search() {
      const q = entries[idx].url.indexOf("?");
      return q === -1 ? "" : entries[idx].url.slice(q);
    },
    get href() {
      return "http://localhost" + entries[idx].url;
    },
  };

  const history = {
    get state() {
      return entries[idx].state;
    },
    get length() {
      return entries.length;
    },
    pushState(state: any, _title: string, url: string) {
      entries.length = idx + 1; // a push truncates the forward branch
      entries.push({ url, state });
      idx = entries.length - 1;
      log.push("push " + url);
    },
    replaceState(state: any, _title: string, url: string) {
      entries[idx] = { url, state };
      log.push("repl " + url);
    },
    back() {
      if (idx > 0) idx -= 1;
      log.push("pop  " + entries[idx].url);
    },
  };

  const makeTarget = () => {
    const t: any = new EventTarget();
    return t;
  };

  const win: any = makeTarget();
  const doc: any = makeTarget();
  doc.documentElement = {
    hasAttribute: () => false,
    setAttribute: () => {},
    style: {},
  };
  doc.querySelectorAll = () => [];
  win.document = doc;
  win.location = location;
  win.history = history;
  win.parent = win; // topmost same-origin window: target === window
  win.matchMedia = () => ({ matches: false, addEventListener: () => {} });

  const sandbox: any = {
    window: win,
    document: doc,
    location,
    history,
    localStorage: { getItem: () => null, setItem: () => {} },
    navigator: { userAgent: "bun", userActivation: { hasBeenActive: true } },
    fetch: () => new Promise(() => {}),
    setTimeout: (fn: () => void) => timers.push(fn),
    clearTimeout: () => {},
    MutationObserver: class {
      observe() {}
      disconnect() {}
    },
    WebSocket: class {},
    EventSource: class {},
    HTMLElement: class {},
    CSS: { supports: () => false },
  };

  return {
    log,
    sandbox,
    get url() {
      return entries[idx].url;
    },
    get state() {
      return entries[idx].state;
    },
    get length() {
      return entries.length;
    },
    /** Mount a fresh copy of the runtime in this context — a page load. Each
     *  mount is its own closure, exactly as a reloaded/remounted document is. */
    mount() {
      const fn = new Function("sandbox", "with (sandbox) {\n" + RUNTIME + "\n}");
      fn(sandbox);
      return win.fused;
    },
    /** A real user interaction reaching this document. */
    gesture() {
      doc.dispatchEvent(new Event("pointerdown"));
    },
    back() {
      history.back();
    },
    /** Run whatever the coalescing timer queued (D99's trailing flush). */
    flushTimers() {
      const queued = timers.splice(0, timers.length);
      for (const fn of queued) {
        try {
          fn();
        } catch (e) {
          /* unrelated init timer in the same queue */
        }
      }
    },
  };
}

describe("runtime params → history", () => {
  let ctx: ReturnType<typeof createContext>;
  const VIEW = "/explorer/view/home/me/task-forge";

  beforeEach(() => {
    ctx = createContext(VIEW);
  });

  test("an init-time seed replaces, and leaves the entry pristine", () => {
    // The view computes a default at load and writes it into the URL from its
    // own boot path. That IS the as-loaded state: it must cost no entry…
    const fused = ctx.mount();
    fused.params.set("dir", "/home/me/Downloads");
    ctx.flushTimers();

    expect(ctx.length).toBe(1);
    expect(ctx.log.filter((l) => l.startsWith("push"))).toEqual([]);
    expect(ctx.url).toBe(VIEW + "?dir=%2Fhome%2Fme%2FDownloads");
    // …and must NOT consume the visit's one push: the entry stays unflagged.
    expect(ctx.state && ctx.state.fusedParamEntry).toBeFalsy();
  });

  test("the first write after a gesture pushes exactly one entry", () => {
    const fused = ctx.mount();
    fused.params.set("dir", "/home/me/Downloads"); // seed
    ctx.flushTimers();
    ctx.gesture();
    fused.params.set("dir", "/home/me/Pictures"); // the user picked a folder

    expect(ctx.length).toBe(2);
    expect(ctx.url).toBe(VIEW + "?dir=%2Fhome%2Fme%2FPictures");
    expect(ctx.state.fusedParamEntry).toBe(true);
    // Back therefore restores the seeded default, not a param-less URL.
  });

  test("a second post-gesture write coalesces onto the pushed entry", () => {
    const fused = ctx.mount();
    ctx.gesture();
    fused.params.set("dir", "/a");
    fused.params.set("dir", "/b");
    fused.params.set("dir", "/c");
    ctx.flushTimers();

    expect(ctx.length).toBe(2); // one entry per visit, however much churn
    expect(ctx.log.filter((l) => l.startsWith("push")).length).toBe(1);
    expect(ctx.url).toBe(VIEW + "?dir=%2Fc");
  });

  test("Back to the pristine entry, then a re-seed, does not push (the trap)", () => {
    // The regression this rule exists for: before D268 the re-seed pushed the
    // forward entry back on, so Back bounced straight into the view again and
    // history.length never shrank — the view could never be left.
    const fused = ctx.mount();
    fused.params.set("dir", "/home/me/Downloads"); // init-time seed
    ctx.flushTimers();
    ctx.gesture();
    fused.params.set("dir", "/home/me/Pictures"); // user-driven: one push
    expect(ctx.length).toBe(2);

    ctx.back(); // → the pristine entry, carrying the seeded default
    expect(ctx.url).toBe(VIEW + "?dir=%2Fhome%2Fme%2FDownloads");

    // The shell remounts the view; its boot path seeds the param again.
    const remounted = ctx.mount();
    remounted.params.set("dir", "/home/me/Downloads");
    ctx.flushTimers();

    expect(ctx.length).toBe(2); // NOT 3, and the forward branch is intact
    expect(ctx.log.filter((l) => l.startsWith("push")).length).toBe(1);
    // Another Back now leaves the view for good.
    ctx.back();
    expect(ctx.length).toBe(2);
  });

  test("a seed that changes nothing writes no history at all", () => {
    // D99's no-op guard, unchanged by the gesture gate.
    const ctx2 = createContext(VIEW + "?dir=%2Fhome%2Fme%2FDownloads");
    const fused = ctx2.mount();
    fused.params.set("dir", "/home/me/Downloads");
    ctx2.flushTimers();
    expect(ctx2.log).toEqual([]);
    expect(ctx2.length).toBe(1);
  });
});
