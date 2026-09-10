// THE LANDING PAGE'S OTHER TWO LISTS, which shipped as placeholders and had to
// be ported for real (Akshil, 2026-09-08, #3: "snapshots and artifacts lists
// are EMPTY in native — legacy shows them").
//
// Two things are pinned here, and they are the two the placeholders could not
// have: the rows really render from the shapes `artifacts.py` and
// `file_history.py` hand back, and the labels a row wears are the template's
// own — `artLabel`'s fallback chain, `snapDeltaLabel`'s two shapes, the
// per-session run grouping that stops a second chain's "v2" reading as a
// duplicate row.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import type { Artifact } from "../protocol/artifacts";
import type { SnapshotVersion, SnapshotsTimeline } from "../protocol/types";

// DYNAMIC, after the shim above has run: `Lists` reaches
// `@platform/lib/router` through the recent rows' link builder, and that module
// reads `location` at import time (see testDomShim's own note). A static import
// is hoisted above the shim call and the suite then only passes when some other
// file in the run happened to install it first.
const { artLabel, artLocalPath, artOpenHref } = await import(
  "../protocol/artifacts"
);
const { snapAgo, snapDeltaLabel, snapRuns, snapVersionLabel } = await import(
  "../protocol/snapshots"
);
const { Lists } = await import("./Lists");

const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement) {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

type Json = ReactTestRendererJSON;
function all(root: Json | null, cls: string): Json[] {
  const out: Json[] = [];
  const walk = (n: Json | string | null) => {
    if (!n || typeof n === "string") return;
    const c = String((n.props as { className?: string }).className ?? "");
    if (c.split(/\s+/).includes(cls)) out.push(n);
    for (const k of n.children ?? []) walk(k as Json);
  };
  walk(root);
  return out;
}
function text(n: Json | string | null): string {
  if (!n) return "";
  if (typeof n === "string") return n;
  return (n.children ?? []).map((k) => text(k as Json)).join("");
}

// ---- artifacts -------------------------------------------------------------

test("an artifact row is named by its title, then its basename, then its url", () => {
  expect(artLabel({ remote_url: "u", title: "Update Signals" })).toBe(
    "Update Signals",
  );
  // A publish that let the page's own <title> name it echoes back no title, and
  // an untitled row is worse than a filename (T:18495).
  expect(artLabel({ remote_url: "u", file_path: "/a/b/report.html" })).toBe(
    "report.html",
  );
  expect(artLabel({ remote_url: "https://x/y" })).toBe("https://x/y");
});

test("the row's own press opens the LOCAL file, and only when it is really there", () => {
  const there: Artifact = {
    remote_url: "u",
    file_path: "/a/b c.html",
    exists: true,
  };
  expect(artLocalPath(there)).toBe("/a/b c.html");
  expect(artOpenHref("/a/b c.html")).toBe("/explorer/view/a/b%20c.html");
  // `exists: null` is a mount-backed path the server refuses to stat, where the
  // hosted page is the one door that cannot hang (T:18521-18526).
  expect(artLocalPath({ remote_url: "u", file_path: "/a", exists: null })).toBe(
    null,
  );
  expect(artLocalPath({ remote_url: "u", exists: true })).toBe(null);
});

test("only a DRIVE-LETTER path has its backslashes rewritten", () => {
  expect(artOpenHref("C:\\x\\y.html")).toBe("/explorer/view/C%3A/x/y.html");
  // A backslash is a legal POSIX filename char and must round-trip (T:18485).
  expect(artOpenHref("/a/we\\ird.html")).toBe("/explorer/view/a/we%5Cird.html");
});

const ART: Artifact[] = [
  {
    remote_url: "https://claude.ai/code/artifact/1",
    file_path: "/repo/out.html",
    title: "Update Signals",
    favicon: "🔔",
    exists: true,
    updated_at: Date.now() / 1000 - 3600,
  },
  {
    remote_url: "https://claude.ai/code/artifact/2",
    file_path: "/repo/second.html",
    exists: false,
    created_at: Date.now() / 1000 - 90000,
  },
];

test("the Artifacts list draws a row per published page, favicon and all", () => {
  const r = mount(
    <Lists file="/repo/x.md" recent={[]} artifacts={ART} onOpen={() => {}} />,
  );
  const json = r.toJSON() as Json;
  const rows = all(json, "c-art-row");
  expect(rows.length).toBe(2);
  expect(text(all(json, "c-art-title")[0])).toBe("Update Signals");
  expect(text(all(json, "c-art-ic")[0])).toBe("🔔");
  // The globe is the ONE place the hosted page opens (T:18535).
  expect(
    (all(json, "c-art-go")[0].props as { href: string }).href,
  ).toBe("https://claude.ai/code/artifact/1");
  // An untitled row falls back to the basename, and the "◻" stands in for a
  // publish that stated no favicon.
  expect(text(all(json, "c-art-title")[1])).toBe("second.html");
  expect(text(all(json, "c-art-ic")[1])).toBe("◻");
});

test("with one list there is no tab bar, and the count sits on the heading", () => {
  const r = mount(
    <Lists file="/repo/x.md" recent={[]} artifacts={ART} onOpen={() => {}} />,
  );
  const json = r.toJSON() as Json;
  // A tab bar with one tab is a label pretending to be a control (T:3311).
  expect(all(json, "c-list-tab").length).toBe(0);
  expect(text(all(json, "c-head")[0])).toBe("Artifacts· 2");
});

// ---- snapshots -------------------------------------------------------------

function ver(over: Partial<SnapshotVersion>): SnapshotVersion {
  return {
    id: "s1@v1",
    session: "s1",
    version: 1,
    existed: true,
    path: null,
    mtime: Date.now() / 1000 - 600,
    size: 10,
    lines: 3,
    differs: true,
    added: 2,
    removed: 1,
    exact: true,
    ...over,
  };
}

function timeline(versions: SnapshotVersion[]): SnapshotsTimeline {
  return {
    file: "/repo/x.py",
    hash: "abc",
    available: true,
    writable: true,
    writable_reason: "",
    current: { exists: true, size: 10, lines: 3 },
    versions,
    position: versions[0]?.id ?? null,
    revert: null,
    offer: true,
    offer_reason: "",
    at_earliest: false,
    unconfirmed: false,
    blocking: [],
    enriched: false,
    unique_current: false,
    skipped: [],
    note: "",
  };
}

test("an INEXACT delta renders as ONE signed term, never as a pair", () => {
  // `_delta`'s cheap branch is a NET, so at most one side can be non-zero and a
  // pair would print a "+0" that is arithmetic rather than measurement
  // (T:18672-18709).
  expect(snapDeltaLabel(ver({ exact: false, added: 43, removed: 0 }))).toEqual([
    { text: "~+43", tone: "plus" },
  ]);
  expect(snapDeltaLabel(ver({ exact: false, added: 0, removed: 43 }))).toEqual([
    { text: "~−43", tone: "minus" },
  ]);
  // Same line COUNT, different bytes: the net has nothing to report, so the
  // honest thing is the one fact it does establish.
  expect(snapDeltaLabel(ver({ exact: false, added: 0, removed: 0 }))).toEqual([
    { text: "changed", tone: "plain" },
  ]);
});

test("an EXACT delta is a pair, and the other three answers are their own words", () => {
  expect(snapDeltaLabel(ver({ added: 5, removed: 2 })).map((p) => p.text)).toEqual(
    ["+5", " ", "−2"],
  );
  expect(snapDeltaLabel(ver({ existed: false }))[0].text).toBe("did not exist");
  expect(snapDeltaLabel(ver({ differs: false }))[0].text).toBe("on disk now");
  expect(
    snapDeltaLabel(ver({ added: null as unknown as number }))[0].text,
  ).toBe("binary");
});

test("a checkpoint with no usable number wears a dash, never an invented v0", () => {
  expect(snapVersionLabel(ver({ version: 3 }))).toBe("v3");
  expect(snapVersionLabel(ver({ version: 0 }))).toBe("—");
});

test("snapAgo says 'time unknown' rather than inventing a moment", () => {
  expect(snapAgo(0)).toBe("time unknown");
  const now = Date.now();
  expect(snapAgo(now / 1000 - 30, now)).toBe("just now");
  expect(snapAgo(now / 1000 - 7200, now)).toBe("2h ago");
});

test("runs are CONTIGUOUS, so a session that came back gets two headings", () => {
  const runs = snapRuns([
    ver({ id: "a@v2", session: "a", version: 2 }),
    ver({ id: "b@v1", session: "b", version: 1 }),
    ver({ id: "a@v1", session: "a", version: 1 }),
  ]);
  // NOT a group-by: `_locate` walks the merged timeline positionally, so this
  // may only insert boundaries (T:18827-18845).
  expect(runs.map((r) => [r.session, r.versions.length])).toEqual([
    ["a", 1],
    ["b", 1],
    ["a", 1],
  ]);
});

test("the snapshots panel draws one box per run, the position marked", () => {
  const t = timeline([
    ver({ id: "a@v2", session: "a", version: 2, differs: false }),
    ver({ id: "a@v1", session: "a", version: 1 }),
    ver({ id: "b@v1", session: "b", version: 1 }),
  ]);
  const r = mount(
    <Lists
      file="/repo/x.py"
      agentDir="/tpl"
      recent={[]}
      artifacts={[]}
      snaps={{
        timeline: t,
        failed: false,
        error: "",
        reload: () => {},
        adopt: () => {},
      }}
      onOpen={() => {}}
    />,
  );
  const json = r.toJSON() as Json;
  expect(all(json, "c-snap-runbox").length).toBe(2);
  expect(all(json, "c-snap-row").length).toBe(3);
  // The dot answers exactly one question: where is disk right now (T:18779).
  expect(all(json, "c-snap-dot").map((n) => text(n))).toEqual(["●", "○", "○"]);
  expect(all(json, "c-snap-row")[0].props.className).toContain("is-here");
  // A chain with no name in the session list says only what is certain, and
  // puts the id beside the count (T:18869-18880).
  expect(text(all(json, "c-snap-run-sub")[1])).toBe("b · 1 checkpoint");
});

test("a FAILED snapshots read keeps its place in the block, holding the retry", () => {
  let reloaded = 0;
  const r = mount(
    <Lists
      file="/repo/x.py"
      agentDir="/tpl"
      recent={[]}
      artifacts={[]}
      snaps={{
        timeline: null,
        failed: true,
        error: "store unreadable",
        reload: () => {
          reloaded += 1;
        },
        adopt: () => {},
      }}
      onOpen={() => {}}
    />,
  );
  const json = r.toJSON() as Json;
  // The panel is the one absence that stays, because the retry beside its label
  // is the only way back from it (T:19168-19174).
  const retry = all(json, "c-snapsretry")[0];
  expect(text(retry)).toBe("try again");
  act(() => (retry.props as { onClick: () => void }).onClick());
  expect(reloaded).toBe(1);
  expect(text(all(json, "c-snapsnote")[0])).toContain("store unreadable");
});

test("two filled lists earn the tab bar; an empty one earns no tab", () => {
  const r = mount(
    <Lists
      file="/repo/x.py"
      agentDir="/tpl"
      recent={[
        {
          id: "s1",
          preview: "hello",
          last_used: Date.now() / 1000,
        } as never,
      ]}
      artifacts={ART}
      snaps={{
        timeline: timeline([]),
        failed: false,
        error: "",
        reload: () => {},
        adopt: () => {},
      }}
      onOpen={() => {}}
    />,
  );
  const json = r.toJSON() as Json;
  expect(all(json, "c-list-tab").map((n) => text(n))).toEqual([
    "Recent chats",
    "Artifacts",
  ]);
});
