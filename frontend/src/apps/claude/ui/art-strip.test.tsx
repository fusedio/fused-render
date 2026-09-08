// The live chip strip: what a chip says, and the two rules that keep the strip
// still while a reply streams beside it — accumulate by url, and rebuild only
// when the CONTENT changed.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";

type Artifact = import("../protocol/artifacts").Artifact;
let rows: Artifact[] = [];
let reads = 0;
/** The transcript read, HANDED IN rather than module-mocked (see
 *  `sched-block.test.tsx` for why a global replacement is the wrong seam). */
const read = () => {
  reads += 1;
  return Promise.resolve(rows);
};

const { ArtStrip, useArtStrip } = await import("./ArtStrip");
type ArtStripStore = import("./ArtStrip").ArtStripStore;

const chips = (tree: ReactTestRenderer) =>
  tree.root.findAll((n) => typeof n.type === "string" && n.props.className === "art-chip");

test("EMPTY IS NOTHING AT ALL — no strip above an unpublished chat", () => {
  let tree: ReactTestRenderer;
  act(() => {
    tree = create(createElement(ArtStrip, { items: [] }));
  });
  expect(tree!.toJSON()).toBeNull();
});

test("a chip is the title, the favicon and the way out", () => {
  const items: Artifact[] = [
    { remote_url: "https://x.test/a", title: "Sales deck", favicon: "📊" },
    // No title: the local file's basename names it, because an untitled row is
    // worse than a filename.
    { remote_url: "https://x.test/b", file_path: "/proj/out/report.html" },
  ];
  let tree: ReactTestRenderer;
  act(() => {
    tree = create(createElement(ArtStrip, { items }));
  });
  const found = chips(tree!);
  expect(found.map((c) => c.props.href)).toEqual(["https://x.test/a", "https://x.test/b"]);
  expect(found.map((c) => c.props.title)).toEqual(["Open Sales deck", "Open report.html"]);
  // A new tab, and never with this document's opener.
  expect(found[0].props.target).toBe("_blank");
  expect(found[0].props.rel).toBe("noopener noreferrer");
  const glyphs = tree!.root
    .findAll((n) => typeof n.type === "string" && n.props.className === "art-ic")
    .map((n) => n.children.join(""));
  // `◻` is the fallback and it is TEXT, never markup.
  expect(glyphs).toEqual(["📊", "◻"]);
});

function store() {
  let api: ArtStripStore | null = null;
  const H = () => {
    api = useArtStrip("/tpl", "/proj", "s1", read);
    return null;
  };
  act(() => {
    create(createElement(H));
  });
  return () => api!;
}

const settle = async () => {
  for (let i = 0; i < 4; i++) await act(async () => {});
};

test("ACCUMULATES BY URL: a chip does not vanish because a later read raced", async () => {
  reads = 0;
  rows = [{ remote_url: "https://x.test/a", title: "A" }];
  const get = store();
  await act(async () => get().poll());
  await settle();
  expect(get().items.map((a) => a.remote_url)).toEqual(["https://x.test/a"]);
  // A read that answers with nothing is not a retraction.
  rows = [];
  await act(async () => get().poll());
  await settle();
  expect(get().items.map((a) => a.remote_url)).toEqual(["https://x.test/a"]);
  rows = [{ remote_url: "https://x.test/b", title: "B" }];
  await act(async () => get().poll());
  await settle();
  expect(get().items.map((a) => a.remote_url)).toEqual([
    "https://x.test/a",
    "https://x.test/b",
  ]);
});

test("CHANGE BY CONTENT, NOT BY COUNT: metadata arriving late still repaints", async () => {
  rows = [{ remote_url: "https://x.test/a" }];
  const get = store();
  await act(async () => get().poll());
  await settle();
  const first = get().items;
  // The same url, same count — an unchanged strip must not be rebuilt, because
  // this runs every few seconds beside a streaming reply and a rebuild drops the
  // user's mid-click on a chip.
  await act(async () => get().poll());
  await settle();
  expect(get().items).toBe(first);
  // The favicon/title join lands a poll or two after the frame-link.
  rows = [{ remote_url: "https://x.test/a", title: "A", favicon: "🌐" }];
  await act(async () => get().poll());
  await settle();
  expect(get().items).not.toBe(first);
  expect(get().items[0].title).toBe("A");
});

test("leaving for the landing page empties it; a no-op clear is not a render", async () => {
  rows = [{ remote_url: "https://x.test/a" }];
  const get = store();
  await act(async () => get().poll());
  await settle();
  const before = get().items;
  expect(before.length).toBe(1);
  await act(async () => get().clear());
  expect(get().items).toEqual([]);
  const empty = get().items;
  await act(async () => get().clear());
  expect(get().items).toBe(empty);
});

test("no session is no read: nothing has been written to look for", async () => {
  reads = 0;
  rows = [{ remote_url: "https://x.test/a" }];
  let api: ArtStripStore | null = null;
  const H = () => {
    api = useArtStrip("/tpl", "/proj", "", read);
    return null;
  };
  act(() => {
    create(createElement(H));
  });
  await act(async () => api!.poll());
  await settle();
  expect(reads).toBe(0);
});
