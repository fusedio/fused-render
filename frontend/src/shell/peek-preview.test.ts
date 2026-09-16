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
  PREVIEW_PAD_Y,
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
    expect(PREVIEW_INSET).toBe(38); // `--peek-preview-inset`, styles/task-peek.css
    expect(narrow.scale).toBeCloseTo(inner(564) / PREVIEW_VW, 6);
    expect(wide.scale).toBeCloseTo(inner(900) / PREVIEW_VW, 6);
    // `frameHeight` is the VIRTUAL viewport, unscaled — 720 when the box is
    // shown at its natural 16:9 footprint, which both of these are.
    expect(narrow.frameHeight).toBeCloseTo(PREVIEW_VH, 6);
    expect(wide.frameHeight).toBeCloseTo(PREVIEW_VH, 6);
    // What grows with the width is the DRAWN footprint, which is that viewport
    // at the panel's scale.
    expect(narrow.frameHeight * narrow.scale).toBeCloseTo(
      (inner(564) * PREVIEW_VH) / PREVIEW_VW,
      6,
    );
    expect(wide.frameHeight * wide.scale).toBeGreaterThan(narrow.frameHeight * narrow.scale);
    // 16:9 on the INNER width — 508 wide is 285.75 tall, and on a body with
    // room it is shown whole, with the card's 12px of air above and below.
    expect(narrow.height).toBeCloseTo(inner(564) * (9 / 16) + 2 * PREVIEW_PAD_Y, 4);
  });

  it("caps at 35% of the body and CONTAINS the 16:9 frame in the shorter card", () => {
    // A 564 peek is 508 inside its gutters and wants 285 of height; a 600px
    // body allows 210. The frame stays 16:9 and shrinks to the 186px card that
    // is left — the height fit wins, and the spare width is padding either side
    // (Akshil, 2026-09-16: contain, not crop).
    expect(PREVIEW_CAP_FRACTION).toBe(0.35);
    const box = previewBox(564, 600, null);
    expect(box.height).toBe(600 * PREVIEW_CAP_FRACTION);
    const card = box.height - 2 * PREVIEW_PAD_Y;
    expect(box.frameHeight).toBe(PREVIEW_VH);
    expect(box.frameHeight * box.scale).toBeCloseTo(card, 6);
    expect(PREVIEW_VW * box.scale).toBeLessThan(564 - 2 * PREVIEW_INSET);
  });

  it("a box dragged past 16:9 keeps the width fit and shows air above and below", () => {
    // Akshil, 2026-09-16: the aspect is locked on both axes. Past the natural
    // height the width is the tighter fit, so the scale stays the panel's and
    // the extra height is padding — the frame never grows past 1280×720.
    // 716 wide is 640 inside its 38px gutters, so the scale is a round 0.5.
    const natural = previewBox(716, 2000, null);
    expect(natural.frameHeight).toBe(PREVIEW_VH);
    expect(natural.scale).toBeCloseTo(0.5, 6);
    expect(PREVIEW_PAD_Y).toBe(12);
    const taller = previewBox(716, 2000, 600 + 2 * PREVIEW_PAD_Y);
    expect(taller.scale).toBe(natural.scale);
    expect(taller.height).toBe(624);
    expect(taller.frameHeight).toBe(PREVIEW_VH);
    // 360 drawn in a 600px card — 240px of air, split by the CSS.
    expect(taller.frameHeight * taller.scale).toBeCloseTo(360, 6);
  });

  it("a box dragged short shrinks the whole 16:9 frame to the card's height", () => {
    // 696 wide is 620 inside its gutters; a 200px box is a 176px card, and the
    // frame is 16:9 at 176 tall — narrower than the card, padded either side.
    const short = previewBox(696, 2000, 200);
    expect(short.height).toBe(200);
    expect(short.frameHeight).toBe(PREVIEW_VH);
    expect(short.frameHeight * short.scale).toBeCloseTo(200 - 2 * PREVIEW_PAD_Y, 6);
    expect(PREVIEW_VW * short.scale).toBeLessThan(696 - 2 * PREVIEW_INSET);
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
