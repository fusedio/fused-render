// The app preview's geometry and its height memory
// (.claude-design/task-side-peek/design.md, "App preview in the peek").
//
// Three rules are worth executing rather than eyeballing: the preview is
// WIDTH-driven (widening the peek makes it taller, which is the whole reason
// for the virtual viewport), it is CAPPED so the conversation is never pushed
// off, and the drag can never take the composer's room — the last of which is a
// never-broken claim and therefore the one a test has to hold.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { beforeEach, describe, expect, it } from "bun:test";

const {
  PREVIEW_CAP_FRACTION,
  PREVIEW_CHAT_MIN,
  PREVIEW_INSET,
  PREVIEW_MIN_H,
  PREVIEW_VH,
  PREVIEW_VW,
  appForProject,
  ensureApps,
  forgetAppsCache,
  getPreviewHeight,
  knownApps,
  previewLoad,
  previewBox,
  resetPeekPreviewForTests,
  resetPreviewHeight,
  setPreviewHeight,
} = await import("./peek-preview");

beforeEach(() => {
  resetPeekPreviewForTests();
});

/** The desk's own row shape, trimmed to what the predicate reads. */
const app = (path: string, over: Record<string, unknown> = {}) => ({
  path,
  name: path.split("/").pop() ?? path,
  entry: `${path}/index.html`,
  kind: "workspace" as const,
  exists: true,
  running: false,
  unread: false,
  iconUrl: null,
  ...over,
});

describe("previewBox", () => {
  it("is 16:9 at the peek's width, and grows in BOTH dimensions with it", () => {
    // The virtual viewport is the point: the app lays out at 1280 and is drawn
    // at the panel's width, so a wider peek is a bigger picture of the same
    // desktop rather than a narrower window shown to the app.
    //
    // …AT THE INNER WIDTH, not the panel's (design.md, Polish batch 4): the box
    // is inset one header-button either side, and padding on a scroller does
    // not shrink what is inside it — a frame drawn at the panel's own width
    // would push 56px of app out past the gutter and grow a sideways scrollbar
    // for it.
    const narrow = previewBox(564, 2000, null);
    const wide = previewBox(900, 2000, null);
    const inner = (w: number) => w - 2 * PREVIEW_INSET;
    expect(PREVIEW_INSET).toBe(28); // `--peek-icon-w`, styles/task-peek.css
    expect(narrow.scale).toBeCloseTo(inner(564) / PREVIEW_VW, 6);
    expect(wide.scale).toBeCloseTo(inner(900) / PREVIEW_VW, 6);
    // `frameHeight` is the VIRTUAL viewport, unscaled — 720 whenever the box is
    // at or under its natural footprint, which both of these are.
    expect(narrow.frameHeight).toBe(PREVIEW_VH);
    expect(wide.frameHeight).toBe(PREVIEW_VH);
    // What grows with the width is the DRAWN footprint, which is that viewport
    // at the panel's scale.
    expect(narrow.frameHeight * narrow.scale).toBeCloseTo(
      (inner(564) * PREVIEW_VH) / PREVIEW_VW,
      6,
    );
    expect(wide.frameHeight * wide.scale).toBeGreaterThan(narrow.frameHeight * narrow.scale);
    // 16:9 on the INNER width — 508 wide is 285.75 tall, and on a body with
    // room it is shown whole.
    expect(narrow.height).toBeCloseTo(inner(564) * (9 / 16), 4);
    expect(narrow.cropped).toBe(false);
  });

  it("caps at half the body and crops rather than squashing", () => {
    // A 564 peek is 508 inside its gutters and wants 285 of height; a 400px
    // body allows 200.
    const box = previewBox(564, 400, null);
    expect(box.height).toBe(400 * PREVIEW_CAP_FRACTION);
    // The frame is still its full 16:9 self — the box simply shows less of it,
    // which is what `overflow: auto` on the box is for.
    expect(box.frameHeight).toBe(PREVIEW_VH);
    expect(box.frameHeight * box.scale).toBeCloseTo((564 - 2 * PREVIEW_INSET) * (9 / 16), 4);
    expect(box.cropped).toBe(true);
  });

  it("gives the APP a taller window when the box is dragged past 16:9", () => {
    // Akshil, 2026-09-14 (design.md, Polish batch 3). The scale is the panel's
    // and does not move — dragging the horizontal seam is the only thing that
    // changes how big the app's text is. What a taller box buys is MORE APP:
    // the virtual viewport grows to `height / scale`, the app lays out into it,
    // and the box is filled at the same scale instead of showing a band of the
    // app's background under a scrollbar with nothing to scroll.
    // 696 wide is 640 inside its gutters, so the scale is a round 0.5.
    const natural = previewBox(696, 2000, null);
    expect(natural.frameHeight).toBe(PREVIEW_VH);
    const taller = previewBox(696, 2000, 600);
    expect(taller.scale).toBe(natural.scale);
    expect(taller.height).toBe(600);
    // 640 / 1280 = 0.5, so a 600px box is a 1200px window.
    expect(taller.frameHeight).toBe(1200);
    // …and it is not "cropped": the drawn frame is exactly the box.
    expect(taller.frameHeight * taller.scale).toBeCloseTo(600, 6);
    expect(taller.cropped).toBe(false);
  });

  it("never hands the app a viewport shorter than a desktop's", () => {
    // Below the natural footprint the box crops, and the app keeps its 720 —
    // a layout built for a desktop must not be asked to render into 200px
    // just because the reader dragged the seam up.
    const short = previewBox(696, 2000, 200);
    expect(short.height).toBe(200);
    expect(short.frameHeight).toBe(PREVIEW_VH);
    expect(short.cropped).toBe(true);
  });

  it("never takes the composer's room, however hard the seam is dragged", () => {
    // THE NEVER-BROKEN RULE. A drag to the bottom of a 700px body leaves the
    // chat exactly its minimum and not a pixel less.
    const box = previewBox(900, 700, 10_000);
    expect(box.height).toBe(700 - PREVIEW_CHAT_MIN);
    expect(700 - box.height).toBe(PREVIEW_CHAT_MIN);
  });

  it("floors a drag at the smallest thing that is still a preview", () => {
    expect(previewBox(900, 700, 0).height).toBe(PREVIEW_MIN_H);
    expect(previewBox(900, 700, -50).height).toBe(PREVIEW_MIN_H);
  });

  it("honours a dragged height between the two, cap and all", () => {
    // The cap governs the UNDRAGGED case only: once the reader has said, they
    // have said, and a preview bigger than half the panel is theirs to ask for.
    const dragged = previewBox(900, 700, 420);
    expect(dragged.height).toBe(420);
    expect(dragged.height).toBeGreaterThan(700 * PREVIEW_CAP_FRACTION);
  });

  it("gives the body entirely to the chat when there is no room for both", () => {
    // A very short panel: no preview is better than a preview with nowhere to
    // type. Zero height is what the caller reads as "do not draw it".
    expect(previewBox(900, PREVIEW_CHAT_MIN + PREVIEW_MIN_H - 10, null).height).toBe(0);
    expect(previewBox(900, 100, 400).height).toBe(0);
  });

  it("survives a panel that has not been laid out yet", () => {
    const box = previewBox(0, 0, null);
    expect(box.height).toBe(0);
    expect(box.scale).toBe(0);
    expect(Number.isFinite(box.frameHeight)).toBe(true);
  });

  it("has no preview at all in a panel narrower than its own two gutters", () => {
    // Not a negative scale and not a frame drawn backwards: `inner > 0` is the
    // guard, and below it the answer is the same "there is nothing to show" a
    // zero-width panel gets.
    const box = previewBox(2 * PREVIEW_INSET, 2000, null);
    expect(box.scale).toBe(0);
    expect(box.height).toBe(0);
  });
});

describe("the remembered height", () => {
  it("starts unset — the cap rule owns an untouched preview", () => {
    expect(getPreviewHeight()).toBeNull();
  });

  it("keeps a dragged height across a task swap", () => {
    // In MEMORY, deliberately: it is a thing the reader did to this sitting of
    // the page, and swapping task is not leaving it.
    setPreviewHeight(320);
    expect(getPreviewHeight()).toBe(320);
    setPreviewHeight(320.4);
    expect(getPreviewHeight()).toBe(320); // rounded, and no needless publish
  });

  it("is forgotten when the page is left", () => {
    setPreviewHeight(320);
    resetPreviewHeight();
    expect(getPreviewHeight()).toBeNull();
  });

  it("is not in any storage — a reload starts over", () => {
    setPreviewHeight(320);
    // Nothing was written anywhere: the module is the whole store, so a fresh
    // process (which is what a reload is) has nothing to read back.
    for (const store of ["localStorage", "sessionStorage"] as const) {
      const s = (globalThis as Record<string, unknown>)[store] as Storage | undefined;
      if (!s) continue;
      for (let i = 0; i < s.length; i += 1) {
        expect(s.key(i) ?? "").not.toContain("preview");
      }
    }
  });
});

describe("appForProject", () => {
  const apps = [app("/w/sine"), app("/w/local/insta-edit-pro")];

  it("finds the app a task's folder belongs to", () => {
    expect(appForProject("/w/sine", apps)?.name).toBe("sine");
    // A task on a SUBFOLDER is still that app's — the app page's own scope test.
    expect(appForProject("/w/sine/src/deep", apps)?.name).toBe("sine");
  });

  it("does not let one folder claim another that merely starts the same", () => {
    expect(appForProject("/w/sine2", apps)).toBeNull();
  });

  it("picks the DEEPEST app when one is nested inside another", () => {
    const nested = [app("/w"), app("/w/local/insta-edit-pro")];
    expect(appForProject("/w/local/insta-edit-pro/x", nested)?.name).toBe("insta-edit-pro");
  });

  it("skips an app with nothing to show", () => {
    expect(appForProject("/w/sine", [app("/w/sine", { entry: null })])).toBeNull();
    expect(appForProject("/w/sine", [app("/w/sine", { exists: false })])).toBeNull();
  });

  it("answers null for an ordinary project, and while the desk is unread", () => {
    expect(appForProject("/w/some/repo", apps)).toBeNull();
    expect(appForProject("/w/sine", null)).toBeNull();
    expect(appForProject("", apps)).toBeNull();
  });
});

describe("previewLoad", () => {
  it("starts waiting and settles on the first thing that happens", () => {
    expect(previewLoad("ready", "src")).toBe("waiting");
    expect(previewLoad("waiting", "load")).toBe("ready");
    expect(previewLoad("waiting", "error")).toBe("failed");
    expect(previewLoad("waiting", "timeout")).toBe("failed");
  });

  it("does NOT un-fail on a load that lands after the clock gave up", () => {
    // The document that finally arrives is, as often as not, the very error
    // page the timeout was about: an iframe that boots into one fires `load`,
    // not `error`. Reverting the strip there would put a broken app back behind
    // a working-looking frame.
    expect(previewLoad("failed", "load")).toBe("failed");
  });

  it("does not fail a preview that is already up", () => {
    // A stale timer from a previous src, or one that fires in the same tick as
    // the load: neither may take down a working app.
    expect(previewLoad("ready", "timeout")).toBe("ready");
    expect(previewLoad("ready", "load")).toBe("ready");
  });

  it("lets a REAL error land at any time — an app can break after it booted", () => {
    expect(previewLoad("ready", "error")).toBe("failed");
  });

  it("only a new src returns it to waiting", () => {
    for (const from of ["waiting", "ready", "failed"] as const) {
      expect(previewLoad(from, "src")).toBe("waiting");
    }
  });
});

describe("the desk's table, cached", () => {
  function stubFetch(): { calls: number; done: () => void } {
    const state = { calls: 0, done: () => {} };
    (globalThis as { fetch: unknown }).fetch = () => {
      state.calls += 1;
      return new Promise((resolve) => {
        state.done = () =>
          resolve({
            ok: true,
            status: 200,
            headers: { get: () => "application/json" },
            json: async () => ({ apps: [] }),
            text: async () => JSON.stringify({ apps: [] }),
          } as unknown as Response);
      });
    };
    return state;
  }

  it("reads COLD, once, and serves warm from memory after", async () => {
    const net = stubFetch();
    expect(knownApps()).toBeNull();
    const first = ensureApps();
    net.done();
    await first;
    expect(net.calls).toBe(1);
    expect(knownApps()).toEqual([]);
    await ensureApps();
    expect(net.calls).toBe(1); // warm — no second read
  });

  it("gives two concurrent mounts the SAME in-flight read", async () => {
    // The peek and the Cards wall can mount in the same commit; two reads of a
    // table neither of them owns is one too many.
    const net = stubFetch();
    const a = ensureApps();
    const b = ensureApps();
    expect(net.calls).toBe(1);
    net.done();
    await Promise.all([a, b]);
    expect(net.calls).toBe(1);
  });

  it("reads again once the table is declared stale", async () => {
    const net = stubFetch();
    const first = ensureApps();
    net.done();
    await first;
    forgetAppsCache();
    expect(knownApps()).toBeNull();
    const second = ensureApps();
    net.done();
    await second;
    expect(net.calls).toBe(2);
  });

  it("treats a failed read as an empty desk rather than an error", async () => {
    // No desk, no preview. The conversation below is what the panel is for and
    // it is unaffected — so a failure is "there is no app here", not a state
    // the peek has to show.
    (globalThis as { fetch: unknown }).fetch = () => Promise.reject(new Error("offline"));
    await ensureApps();
    expect(knownApps()).toEqual([]);
    expect(appForProject("/w/sine", knownApps())).toBeNull();
  });
});
