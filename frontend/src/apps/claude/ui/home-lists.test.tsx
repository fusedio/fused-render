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
import { readFileSync } from "node:fs";

import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import type { Task } from "@platform/lib/api";
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
type ListsProps = import("./Lists").ListsProps;
const { listTabKey, nextTab, rememberedTab, resetRememberedTab } = await import(
  "./lists-visibility"
);
const { sessionTitle: rowsSessionTitle, taskInPane, taskPane } =
  await import("./list-rows");
const { resetSessionSeeds, sessionSeed } = await import("./useRecentTasks");
const { sessionTitle: protoSessionTitle } = await import("../protocol/history");
const { MARKER_JOIN } = await import("../protocol/wire");

const mounted: Array<ReturnType<typeof create>> = [];
function mount(
  el: React.ReactElement,
  opts?: Parameters<typeof create>[1],
) {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el, opts);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  // The selected tab is PAGE-scoped by design (T:18260-18265), so it outlives
  // every renderer in this file and has to be put back by hand.
  resetRememberedTab();
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

/** ONE PAST CHAT, as the row now takes it: a task about this pane's own file,
 *  with a session to open. The shapes the Recent list draws are `/api/tasks`'s,
 *  not the `sessions` action's (.claude-design/design.md §B). */
function chat(id: string, over: Partial<Task> = {}): Task {
  return {
    key: id,
    task_id: id.toUpperCase(),
    project: "/repo",
    target: "/repo/x.py",
    session_id: id,
    title: "hello",
    title_source: "message",
    description: "",
    status: "done",
    unread: 0,
    message_count: 1,
    last_active: Date.now() / 1000,
    ...over,
  } as Task;
}

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
        settled: true,
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
        settled: true,
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
      recent={[chat("s1")]}
      artifacts={ART}
      snaps={{
        timeline: timeline([]),
        failed: false,
        error: "",
        reload: () => {},
        adopt: () => {},
        settled: true,
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

// ---- the tab bar's keyboard, its memory, and the gutters under it ----------

/** Two filled lists — the shape every test below needs, and the smallest one
 *  that earns a bar at all. */
const TABBED: Omit<ListsProps, "onOpen"> = {
  file: "/repo/x.py",
  agentDir: "/tpl",
  recent: [chat("s1")],
  artifacts: ART,
  snaps: {
    timeline: timeline([]),
    failed: false,
    error: "",
    reload: () => {},
    adopt: () => {},
    settled: true,
  },
};

/** The rendered tabs, with the props the keyboard walk is driven through. */
function tabs(r: ReturnType<typeof create>) {
  return r.root
    .findAll(
      (n) =>
        typeof n.type === "string" &&
        !!(n.props as { "data-list-tab"?: string })["data-list-tab"],
    )
    .map((n) => ({
      name: (n.props as { "data-list-tab": string })["data-list-tab"],
      selected:
        (n.props as { "aria-selected"?: unknown })["aria-selected"] === true ||
        (n.props as { "aria-selected"?: unknown })["aria-selected"] === "true",
      keydown: (n.props as { onKeyDown(ev: unknown): void }).onKeyDown,
    }));
}

/** Which tab the handler moved the caret to, newest last. */
const focusedTabs: string[] = [];

function arrow(
  r: ReturnType<typeof create>,
  from: string,
  key: "ArrowRight" | "ArrowLeft",
): { prevented: boolean; stopped: boolean } {
  const tab = tabs(r).find((t) => t.name === from);
  if (!tab) throw new Error("no tab " + from);
  let prevented = false;
  let stopped = false;
  act(() =>
    tab.keydown({
      key,
      // Base UI's own tab handler runs alongside ours and reads the node back
      // off the event, so the synthetic press has to carry a stand-in for it.
      // Base UI's own tab handler runs alongside ours and reads the node back
      // off the event; ours walks from the node to the BAR (see Lists.tsx on
      // why it is not a ref), so the stand-in has to answer both.
      currentTarget: {
        getAttribute: () => null,
        closest: () => null,
        parentElement: {
          querySelector: (sel: string) => {
            const m = /data-list-tab="([^"]+)"/.exec(sel);
            return m ? { focus: () => focusedTabs.push(m[1]) } : null;
          },
        },
      },
      target: null,
      preventDefault: () => {
        prevented = true;
      },
      stopPropagation: () => {
        stopped = true;
      },
    }),
  );
  return { prevented, stopped };
}

test("ARROWS SELECT, not just focus, and they wrap over the shown tabs", () => {
  focusedTabs.length = 0;
  const r = mount(<Lists {...TABBED} onOpen={() => {}} />);
  expect(tabs(r).find((t) => t.selected)?.name).toBe("recent");

  // T:18332-18336 calls `selectListTab(next.name)` AND `focus()`. Base UI moves
  // focus on its own but does not activate on it, so the panel used to stay put.
  expect(arrow(r, "recent", "ArrowRight").prevented).toBe(true);
  expect(tabs(r).find((t) => t.selected)?.name).toBe("artifacts");
  expect(focusedTabs).toEqual(["artifacts"]);

  // WRAPPING: with two of three shown the pair still toggles (T:18330-18331).
  arrow(r, "artifacts", "ArrowRight");
  expect(tabs(r).find((t) => t.selected)?.name).toBe("recent");
  expect(focusedTabs).toEqual(["artifacts", "recent"]);

  // And the other way, from the same place.
  arrow(r, "recent", "ArrowLeft");
  expect(tabs(r).find((t) => t.selected)?.name).toBe("artifacts");
});

test("A HIDDEN TAB IS NOT A STOP: the empty snapshots list is walked past", () => {
  // `nextTab` is the function that knows which tabs are on the bar, and the
  // handler defers to it rather than to the DOM's own tab order.
  const counts = { recent: 2, artifacts: 1, snaps: 0, snapsFailed: false };
  expect(nextTab(counts, "recent", 1)).toBe("artifacts");
  expect(nextTab(counts, "artifacts", 1)).toBe("recent");
  // One tab is no walk at all — and the handler must then leave the key alone,
  // or the column loses its arrow-scroll.
  const lone = { recent: 2, artifacts: 0, snaps: 0, snapsFailed: false };
  expect(nextTab(lone, "recent", 1)).toBe(null);
});

test("THE SELECTED TAB SURVIVES ENTER-AND-BACK (T:18260-18265)", () => {
  const first = mount(<Lists {...TABBED} onOpen={() => {}} />);
  arrow(first, "recent", "ArrowRight");
  expect(tabs(first).find((t) => t.selected)?.name).toBe("artifacts");
  // `Lists` unmounts on the way INTO a chat — which is why component state
  // could never hold this.
  act(() => first.unmount());
  mounted.splice(mounted.indexOf(first), 1);
  expect(rememberedTab(listTabKey("/tpl", "/repo/x.py"))).toBe("artifacts");

  // Back.
  const back = mount(<Lists {...TABBED} onOpen={() => {}} />);
  expect(tabs(back).find((t) => t.selected)?.name).toBe("artifacts");
});

// THE MEMORY IS PER TARGET, not one variable for the document (batch review
// F3). P4-06's own premise is that native renders the cards wall, Peek and the
// split pane in ONE document, so a module-level `let` meant picking "Artifacts"
// in one tile changed what a DIFFERENT tile showed on its next landing — and it
// survived a target change too, which T's `listTab` (one page = one target)
// could not.
test("THE TAB MEMORY IS KEYED ON THE TARGET: another file answers for itself", () => {
  const a = mount(<Lists {...TABBED} onOpen={() => {}} />);
  arrow(a, "recent", "ArrowRight");
  expect(tabs(a).find((t) => t.selected)?.name).toBe("artifacts");

  // A SECOND TILE, same document, a different file. It has never been touched,
  // so it lands on "Recent chats" — the tile above must not have moved it.
  const b = mount(<Lists {...TABBED} file="/repo/other.py" onOpen={() => {}} />);
  expect(tabs(b).find((t) => t.selected)?.name).toBe("recent");
  // And the first tile is undisturbed by the second one mounting.
  expect(tabs(a).find((t) => t.selected)?.name).toBe("artifacts");

  // Each key holds its own answer.
  expect(rememberedTab(listTabKey("/tpl", "/repo/x.py"))).toBe("artifacts");
  expect(rememberedTab(listTabKey("/tpl", "/repo/other.py"))).toBe("recent");
  // The agent dir is half the key as well, so the same file under a second
  // template folder is a second memory.
  expect(rememberedTab(listTabKey("/tpl2", "/repo/x.py"))).toBe("recent");
});

test("a target CHANGE under one mount reads that target's own tab back", () => {
  const r = mount(<Lists {...TABBED} onOpen={() => {}} />);
  arrow(r, "recent", "ArrowRight");
  expect(tabs(r).find((t) => t.selected)?.name).toBe("artifacts");

  // T's variable could not do this: its scope is one page = one target, so a
  // target switch had nothing to carry across. Ours had one variable and
  // carried the wrong answer over.
  act(() => r.update(<Lists {...TABBED} file="/repo/other.py" onOpen={() => {}} />));
  expect(tabs(r).find((t) => t.selected)?.name).toBe("recent");
  // Back to the first target and its own selection returns.
  act(() => r.update(<Lists {...TABBED} onOpen={() => {}} />));
  expect(tabs(r).find((t) => t.selected)?.name).toBe("artifacts");
});

// A LOCKED BLOCK LOCKS THE ARROWS TOO (P4-23, batch review F7). The rows already
// refused activation; the arrows still moved `aria-selected` and swapped the
// visible panel, which is the same navigation by the keyboard — "the keyboard's
// copy" is exactly what P4-23 was filed to guard.
test("A LOCKED BLOCK REFUSES THE ARROW WALK (P4-23)", () => {
  const r = mount(<Lists {...TABBED} onOpen={() => {}} disabled />);
  expect(tabs(r).find((t) => t.selected)?.name).toBe("recent");

  const press = arrow(r, "recent", "ArrowRight");
  expect(tabs(r).find((t) => t.selected)?.name).toBe("recent");
  // Refused BEFORE the key test, so a locked bar swallows nothing: the column
  // keeps its arrow-scroll while the mode holds the reader.
  expect(press.prevented).toBe(false);
  expect(press.stopped).toBe(false);
  // And nothing was written to the page's memory either.
  expect(rememberedTab(listTabKey("/tpl", "/repo/x.py"))).toBe("recent");

  // The lock lifting gives the gesture straight back.
  act(() => r.update(<Lists {...TABBED} onOpen={() => {}} />));
  arrow(r, "recent", "ArrowRight");
  expect(tabs(r).find((t) => t.selected)?.name).toBe("artifacts");
});

// AND BASE UI'S OWN HANDLER IS TOLD TO STAY OUT (batch review F8). Its
// roving-focus arrow handler is bound on the SAME tab and does not promise to
// honour `defaultPrevented`, so without `stopPropagation` the press moved focus
// twice — ours to `next`, theirs one further — and the selected tab came apart
// from the focused one. The co-bound handler below stands in for Base UI's: it
// is what a real listener on the same node would do, and the contract pinned is
// that ours stops the event reaching it. (Base UI's real handler is browser
// verified; nothing in this runtime can mount it.)
test("THE ARROW STOPS PROPAGATING so Base UI does not walk it a second time", () => {
  focusedTabs.length = 0;
  const r = mount(<Lists {...TABBED} onOpen={() => {}} />);
  const press = arrow(r, "recent", "ArrowRight");
  expect(press.prevented).toBe(true);
  expect(press.stopped).toBe(true);
  // One move, not two.
  expect(focusedTabs).toEqual(["artifacts"]);

  // What `stopped` buys, spelled as the sibling listener it is for: a handler on
  // the same node only runs while the event is still propagating.
  const sibling: string[] = [];
  let propagating = true;
  const ours = tabs(r).find((t) => t.name === "artifacts")!.keydown;
  act(() =>
    ours({
      key: "ArrowRight",
      currentTarget: {
        getAttribute: () => null,
        closest: () => null,
        parentElement: {
          querySelector: (sel: string) => {
            const m = /data-list-tab="([^"]+)"/.exec(sel);
            return m ? { focus: () => focusedTabs.push(m[1]) } : null;
          },
        },
      },
      target: null,
      preventDefault: () => {},
      stopPropagation: () => {
        propagating = false;
      },
    }),
  );
  if (propagating) sibling.push("baseui-moved-focus-again");
  expect(sibling).toEqual([]);
});

test("a remembered tab whose list has since emptied falls back, never blank", () => {
  const first = mount(<Lists {...TABBED} onOpen={() => {}} />);
  arrow(first, "recent", "ArrowRight");
  act(() => first.unmount());
  mounted.splice(mounted.indexOf(first), 1);

  // Same page, a target with no published pages: "Artifacts" is remembered but
  // has no tab, so `computeLists` falls the selection back (T:18293-18317).
  const bare = mount(
    <Lists
      {...TABBED}
      artifacts={[]}
      snaps={{
        timeline: timeline([ver({ id: "v1" })]),
        failed: false,
        error: "",
        reload: () => {},
        adopt: () => {},
        settled: true,
      }}
      onOpen={() => {}}
    />,
  );
  const names = tabs(bare).map((t) => t.name);
  expect(names).not.toContain("artifacts");
  expect(tabs(bare).find((t) => t.selected)?.name).toBe(names[0]);
});

test("A MODE HOLDS THE READER: a locked block refuses the rows (P4-23)", () => {
  // T draws the ways out inert in CSS (`body.annlock #recentlist .chat-row` —
  // T:1467-1469, native's `.chat-root.annlock` rows in `ann.css`) AND guards
  // the opener in script, "the keyboard's copy" (T:18181). Native had both
  // halves and never handed `Home` the prop, so the script half was fed
  // `undefined` for the life of the view.
  const opened: string[] = [];
  const locked = mount(
    <Lists {...TABBED} onOpen={(id) => opened.push(id)} disabled />,
  );
  const row = taskRow(locked);
  act(() => (row.props as { onClick?(): void }).onClick?.());
  act(() =>
    (row.props as { onKeyDown(ev: { key: string }): void }).onKeyDown({
      key: "Enter",
    }),
  );
  expect(opened).toEqual([]);
  // AND NO LINK EITHER: a stretched `<a href>` would take ⌘-click, middle click
  // and "Open in new tab" out of the lock's reach entirely.
  expect(locked.root.findAll((n) => n.type === "a").length).toBe(0);

  // And unlocked it opens on either gesture — so the assertion above is about
  // the lock and not about a row that never worked.
  const free = mount(<Lists {...TABBED} onOpen={(id) => opened.push(id)} />);
  act(() => (taskRow(free).props as { onClick(): void }).onClick());
  expect(opened).toEqual(["s1"]);
});

/** The one task row the fixtures draw. `.tasks-row` is the Tasks page's own row
 *  (ScheduleTaskViews), which is what the Recent list renders now. */
function taskRow(r: ReturnType<typeof create>) {
  return r.root.find(
    (n) =>
      typeof n.type === "string" &&
      /(^| )tasks-row( |$)/.test(
        String((n.props as { className?: string }).className || ""),
      ),
  );
}

/** Every row on screen, not just the first — for the tests that care how
 *  MANY the list drew rather than which one. */
function taskRows(r: ReturnType<typeof create>) {
  return r.root.findAll(
    (n) =>
      typeof n.type === "string" &&
      /(^| )tasks-row( |$)/.test(
        String((n.props as { className?: string }).className || ""),
      ),
  );
}

test("A CHAT ABOUT THIS PANE opens in place; one about another file hops", () => {
  // `RecentRow`'s own split, kept (T:18183-18197): same file ⇒ `onOpen`, other
  // file ⇒ the host is sent to that file with the session attached.
  const opened: string[] = [];
  const hops: string[] = [];
  const r = mount(
    <Lists
      {...TABBED}
      recent={[chat("s1"), chat("s2", { key: "s2", target: "/repo/other.py" })]}
      onOpen={(id) => opened.push(id)}
      onNavigate={(u) => hops.push(u)}
    />,
  );
  const rows = r.root.findAll(
    (n) =>
      typeof n.type === "string" &&
      /(^| )tasks-row( |$)/.test(
        String((n.props as { className?: string }).className || ""),
      ),
  );
  expect(rows.length).toBe(2);
  // The in-place row is the button; the hopping one wears a real stretched link
  // so ⌘-click and middle click are the browser's own.
  act(() => (rows[0].props as { onClick(): void }).onClick());
  expect(opened).toEqual(["s1"]);
  const link = r.root.find((n) => n.type === "a");
  expect(String((link.props as { href: string }).href)).toContain(
    "session_id=s2",
  );
  act(() =>
    (link.props as { onClick(e: unknown): void }).onClick({
      preventDefault() {},
      button: 0,
    }),
  );
  expect(hops.length).toBe(1);
  expect(hops[0]).toContain("other.py");
});

test("OPENING A CHAT CLEARS ITS RING, and says so to the server", async () => {
  // The same write `performOpen` makes for the Tasks page's own row and the
  // Board card: opening the thread is what clears it, and where it opens is not
  // the badge's business. Planted locally FIRST, because the press is leaving.
  const calls: Array<{ url: string; body: unknown }> = [];
  const realFetch = globalThis.fetch;
  (globalThis as { fetch: unknown }).fetch = async (
    input: unknown,
    init?: { body?: unknown },
  ) => {
    calls.push({ url: String(input), body: JSON.parse(String(init?.body ?? "{}")) });
    return {
      ok: true,
      status: 200,
      json: async () => ({ ok: true, unread: 0 }),
    } as unknown as Response;
  };
  try {
    const r = mount(
      <Lists {...TABBED} recent={[chat("s1", { unread: 3 })]} onOpen={() => {}} />,
    );
    const ring = () =>
      r.root.find(
        (n) =>
          typeof n.type === "string" &&
          String((n.props as { className?: string }).className || "").includes(
            "schedule-ring",
          ),
      );
    expect(String(ring().props.className)).toContain("schedule-ring--unread");
    await act(async () => {
      (taskRow(r).props as { onClick(): void }).onClick();
    });
    // The row asks the server for ONE thing on a press. It also reads
    // `/api/prefs` on mount (TaskRowItem -> useTaskCardTitleMode), which is a
    // fact about the row and not about this press, so the claim is filtered to
    // the write rather than widened to "whatever the row happens to fetch".
    const reads = calls.filter((c) => c.url === "/api/tasks/read");
    expect(reads.map((c) => c.url)).toEqual(["/api/tasks/read"]);
    expect(reads[0].body).toEqual({ key: "s1", all: true });
    // …and the ring is hollow on the row's own press, not on the next listing.
    expect(String(ring().props.className)).not.toContain("schedule-ring--unread");
  } finally {
    (globalThis as { fetch: unknown }).fetch = realFetch;
  }
});

test("A LATER BURST OF THE SAME SIZE RE-LIGHTS THE RING", async () => {
  // The optimistic clear is a VALUE comparison — "the listing still says N, and
  // N is what I just cleared" — because the borrowed row has no read set to diff
  // against. Left standing after the listing settles to 0 it goes on matching,
  // so three new messages behind a cleared three would read as the same clear
  // and the ring would stay hollow over unread work. Zero is the server
  // agreeing, and that is where the guess is dropped.
  const realFetch = globalThis.fetch;
  (globalThis as { fetch: unknown }).fetch = async () =>
    ({ ok: true, status: 200, json: async () => ({ ok: true, unread: 0 }) }) as unknown as Response;
  try {
    const r = mount(
      <Lists {...TABBED} recent={[chat("s1", { unread: 3 })]} onOpen={() => {}} />,
    );
    const lit = () =>
      String(
        r.root.find(
          (n) =>
            typeof n.type === "string" &&
            String((n.props as { className?: string }).className || "").includes(
              "schedule-ring",
            ),
        ).props.className,
      ).includes("schedule-ring--unread");
    const listing = async (unread: number) => {
      await act(async () => {
        r.update(
          <Lists {...TABBED} recent={[chat("s1", { unread })]} onOpen={() => {}} />,
        );
      });
    };

    expect(lit()).toBe(true);
    await act(async () => {
      (taskRow(r).props as { onClick(): void }).onClick();
    });
    // Hollow on the press itself, while the listing still says three.
    expect(lit()).toBe(false);
    // The server agrees, and the guess is no longer load-bearing.
    await listing(0);
    expect(lit()).toBe(false);
    // Three NEW messages, the same count as the clear. A stale `chatCleared`
    // would swallow exactly this.
    await listing(3);
    expect(lit()).toBe(true);
  } finally {
    (globalThis as { fetch: unknown }).fetch = realFetch;
  }
});

test("THE PANE'S OWN FILTER: folder panes match by project, file panes by target", () => {
  // .claude-design/design.md §B. One test either way — see `taskInPane`.
  const here = chat("s1");
  expect(taskInPane(here, "/repo/x.py")).toBe(true);
  expect(taskInPane(here, "/repo")).toBe(true);
  expect(taskInPane(here, "/repo/")).toBe(true);
  expect(taskInPane(here, "/repo/other.py")).toBe(false);
  expect(taskInPane(here, null)).toBe(false);
  // …and a task about the FOLDER itself belongs to the folder pane only.
  const folder = chat("s2", { target: "/repo" });
  expect(taskInPane(folder, "/repo")).toBe(true);
  expect(taskInPane(folder, "/repo/x.py")).toBe(false);
  // `taskPane` is "" for this pane's own chat and for a folder-scoped task,
  // which is what makes the press an in-place open rather than a hop.
  expect(taskPane(here, "/repo/x.py")).toBe("");
  expect(taskPane(folder, "/repo")).toBe("");
  expect(taskPane(here, "/repo")).toBe("/repo/x.py");
});

// ---- the gutters (P4-12) and the property WKWebView ignores (P4-13) --------

const HOME_CSS = await Bun.file(
  new URL("../styles/home.css", import.meta.url).pathname,
).text();

test("two panels standing alone get T's 28px between them and 32px below", () => {
  // T:3357 (`#snaps` 28px top), T:3382 (32px bottom), T:3527 (`#artifacts` no
  // top gutter of its own — the gap belongs to the PAIR).
  expect(HOME_CSS).toContain(".c-lists .c-list-panel + .c-list-panel");
  const pair = /\.c-lists \.c-list-panel \+ \.c-list-panel \{([^}]*)\}/.exec(
    HOME_CSS,
  );
  expect(pair?.[1]).toContain("margin-top: 28px");
  const last =
    /\.c-lists:not\(\.is-tabbed\) \.c-list-panel:last-child \{([^}]*)\}/.exec(
      HOME_CSS,
    );
  expect(last?.[1]).toContain("padding-bottom: 32px");
});

test("NO flex-basis anywhere in home.css — WKWebView ignores it outright", () => {
  // T:2755-2769 records why, and `composer.css:302-307` spells the same intent
  // as `width: 100%` for the same reason. A DMG-only defect no browser check
  // can catch, so it is pinned as a rule assertion instead (P4-13 / B-33).
  // Comments stripped first: the rule's own note NAMES the property it refuses,
  // and a grep that cannot tell a declaration from an explanation would make
  // documenting the ban impossible.
  const decls = HOME_CSS.replace(/\/\*[\s\S]*?\*\//g, "");
  expect(decls).not.toContain("flex-basis");
  const note = /\.c-snap-note \{([^}]*)\}/.exec(HOME_CSS);
  expect(note?.[1]).toContain("width: 100%");
});

test("THE ROWS RENDER THE PROTOCOL'S TITLE, not a second copy of it (P4-04)", () => {
  // `ui/list-rows.ts` used to spell its own: `MARKER_JOIN = " · "` where both
  // T:10538 and `wire.ts:58` say `" + "`, invented marker words ("picture",
  // "comments") instead of the wire's, and an opener table with only the PROSE
  // openers — so a truncated preview leaked its literal tag as the row title.
  // That copy was what the live rows AND the snapshot run headings used, while
  // `protocol/history.ts`'s (which had all three right) was imported by nothing
  // but its own test (P4-04 / B-30).
  expect(rowsSessionTitle).toBe(protoSessionTitle);
  expect(MARKER_JOIN).toBe(" + ");
  // And the row a truncated pane-shot names is its marker's WORDS, never a tag.
  expect(rowsSessionTitle({ id: "s1", preview: "<pane-shot>\nThe user att" })).toBe(
    "pane screenshot",
  );
});

// ---- DRAFT ROWS ARE NOT INERT ROWS (Akshil, 2026-09-14) ---------------------
//
// Every `kind: "draft"` row used to fall out of `pressFor`'s first line — no
// `session_id`, therefore no press — which drew a lit, titled, Draft-chipped row
// that did nothing at all. A draft HAS somewhere to go; it is simply not a
// conversation, and where it goes is what `draft_kind` is for.

/** A never-sent chat, as `/api/tasks` emits it (`_new_chat_draft_row`): no
 *  session, a `file` that is the `new:<file>` key's own half, and the unsent
 *  line as its `draft`. */
function chatDraft(over: Partial<Task> = {}): Task {
  return {
    key: "new:/repo/x.py",
    task_id: "TASK-900",
    kind: "draft",
    state: "draft",
    draft_kind: "chat",
    draft_id: "",
    project: "/repo",
    target: "/repo/x.py",
    file: "/repo/x.py",
    session_id: "",
    title: "ship the thing",
    title_source: "draft",
    description: "",
    status: "draft",
    unread: 0,
    message_count: 0,
    draft: { preview: "ship the thing", updated_at: Date.now() / 1000, kind: "chat" },
    last_active: Date.now() / 1000,
    ...over,
  } as unknown as Task;
}

/** A draft row's press, as `Lists` reports it: the ROW, and nothing else. */
function pressDraft(recent: Task[], over: Record<string, unknown> = {}) {
  const pressed: Task[] = [];
  const hops: string[] = [];
  const r = mount(
    <Lists
      {...TABBED}
      recent={recent}
      onOpen={() => {}}
      onFillDraft={(t) => pressed.push(t)}
      onNavigate={(u) => hops.push(u)}
      {...over}
    />,
  );
  return { r, pressed, hops };
}

test("A DRAFT ROW FILLS THE COMPOSER AND GOES NOWHERE (Akshil, 2026-09-15)", () => {
  // The words are unsent words and the landing's own composer is directly above
  // this list. Both draft kinds used to navigate — a chat draft about another
  // file hopped the host, a task draft left the app for `/tasks?draft=` — and
  // neither does now: no href, so not even a ⌘-click has anywhere to go.
  const { r, pressed, hops } = pressDraft([chatDraft()]);
  const row = taskRow(r);
  expect(String((row.props as { className?: string }).className)).not.toContain(
    "is-inert",
  );
  act(() => (row.props as { onClick(): void }).onClick());
  expect(pressed.map((t) => t.key)).toEqual(["new:/repo/x.py"]);
  expect(hops).toEqual([]);
  expect(r.root.findAll((n) => n.type === "a").length).toBe(0);
});

test("…and a chat draft about ANOTHER file stays here too", () => {
  // THE ONE THAT USED TO HOP. Its words go into the box the reader is looking
  // at; which folder they were typed in is the host's problem, not a reason to
  // move the page (the landing's autosave then keeps them under `new:<file>`
  // for THIS folder, which is the accepted cost of not navigating).
  const other = chatDraft({
    key: "new:/repo/other.py",
    target: "/repo/other.py",
    file: "/repo/other.py",
  });
  const { r, pressed, hops } = pressDraft([other]);
  act(() => (taskRow(r).props as { onClick(): void }).onClick());
  expect(pressed.map((t) => t.file)).toEqual(["/repo/other.py"]);
  expect(hops).toEqual([]);
  expect(r.root.findAll((n) => n.type === "a").length).toBe(0);
});

test("A TASK DRAFT is the same press — it does not leave for the Tasks modal", () => {
  const form = chatDraft({
    key: "draft:d-7",
    draft_kind: "task",
    draft_id: "d-7",
    file: "/repo",
    target: "/repo",
    draft: null,
  } as Partial<Task>);
  const { r, pressed, hops } = pressDraft([form]);
  act(() => (taskRow(r).props as { onClick(): void }).onClick());
  expect(pressed.map((t) => t.draft_id)).toEqual(["d-7"]);
  expect(hops).toEqual([]);
  expect(r.root.findAll((n) => n.type === "a").length).toBe(0);
});

test("a LOCKED block still refuses every draft row", () => {
  // P4-23 is about the block, not about which kind of row is in it.
  const { r, pressed } = pressDraft([chatDraft()], { disabled: true });
  act(() => (taskRow(r).props as { onClick?(): void }).onClick?.());
  expect(pressed).toEqual([]);
  expect(r.root.findAll((n) => n.type === "a").length).toBe(0);
});

// ---- a press OPENS the record where it is; it never moves it ---------------
//
// design "one record", §1. The press used to be a MOVE: it read the source
// record whole, wrote the words into whichever composer the reader happened to
// be looking at, and deleted the source. That needed five mechanisms —
// read-whole-or-refuse, a guard against a second press landing mid-move, an
// ordering against the destination's own autosave, a join rule both sides had
// to predict, and an undo for every step that could fail. All five are gone,
// because the one thing the move existed to prevent (one sentence, two rows,
// two TASK numbers) cannot happen if nothing is ever copied.

test("a chat draft's press is its own chat, and this box's own draft is a focus request", () => {
  const { r, pressed } = pressDraft([chatDraft({ key: "new:/repo/other.py" })]);
  expect(pressed.map((t) => t.key)).toEqual([]);
  act(() => (taskRow(r).props as { onClick(): void }).onClick());
  expect(pressed.map((t) => t.key)).toEqual(["new:/repo/other.py"]);
  // The host decides where that goes (`ClaudeChat.onFillDraft` → `draftHref`);
  // what the LIST owes is handing the row over untouched, with no read and no
  // write of its own on the way.
});

test("the move's machinery is gone from the row module, not merely unused", () => {
  const src = readFileSync(new URL("./list-rows.ts", import.meta.url), "utf8");
  for (const gone of ["draftContentOf", "draftMovesOut", "joinIntoBox", "readChatDraft"]) {
    expect(src).not.toContain(`export function ${gone}`);
    expect(src).not.toContain(`export async function ${gone}`);
  }
  // …and what replaced them is one function that answers a URL.
  expect(src).toContain("export function draftHref(task: Task, file: string | null)");
});

test("A DRAFT ROW CARRIES THE DISCARD, and no other row does", () => {
  // design.md, PR C: the one action a draft row has. Same class as the List's
  // missing-folder trash, so it is the same button under the same hover rule.
  const { r } = pressDraft([chatDraft()]);
  const trash = r.root.findAll(
    (n) => typeof n.type === "string"
      && String((n.props as { className?: string }).className ?? "")
        .split(/\s+/).includes("tasks-act--delete"),
  );
  expect(trash.length).toBe(1);
  expect((trash[0].props as { title?: string }).title).toBe("Discard draft");

  const plain = mount(
    <Lists {...TABBED} recent={[chat("s1")]} onOpen={() => {}} onFillDraft={() => {}} />,
  );
  expect(plain.root.findAll(
    (n) => typeof n.type === "string"
      && String((n.props as { className?: string }).className ?? "")
        .split(/\s+/).includes("tasks-act--delete"),
  ).length).toBe(0);
});

test("pressing it deletes the draft, and never presses the row", async () => {
  // ONE REQUEST. The composer holding these words is no longer told anything
  // before the DELETE: the delete states the version it read, so a write still
  // on the wire from that box is refused rather than landing after it and
  // putting the draft back (design "one record", §2).
  const calls: Array<{ method: string; url: string }> = [];
  const realFetch = globalThis.fetch;
  (globalThis as { fetch: unknown }).fetch = async (input: unknown, init?: { method?: string }) => {
    calls.push({ method: init?.method ?? "GET", url: String(input) });
    return { ok: true, status: 200, json: async () => ({ ok: true }) } as unknown as Response;
  };
  try {
    const { r, pressed } = pressDraft([chatDraft()]);
    const trash = r.root.find(
      (n) => typeof n.type === "string"
        && String((n.props as { className?: string }).className ?? "")
          .split(/\s+/).includes("tasks-act--delete"),
    );
    let stopped = false;
    await act(async () => {
      (trash.props as { onClick(e: unknown): void }).onClick({
        stopPropagation: () => {
          stopped = true;
        },
      });
      // A macrotask, not a microtask drain: the handler's own `finally` lands
      // after the stubbed fetch resolves, and it sets state — so it has to be
      // inside this `act` or React says so.
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(stopped).toBe(true);
    expect(pressed).toEqual([]);
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      "DELETE /api/drafts/chat/new%3A/repo/x.py",
    ]);
  } finally {
    (globalThis as { fetch: unknown }).fetch = realFetch;
  }
});

test("a host that offers no fill leaves the row inert rather than lit and dead", () => {
  const r = mount(<Lists {...TABBED} recent={[chatDraft()]} onOpen={() => {}} />);
  expect((taskRow(r).props as { onClick?(): void }).onClick).toBeUndefined();
  expect(r.root.findAll((n) => n.type === "a").length).toBe(0);
});

test("EVERY press seeds the header's identity — the hop as much as the in-place open", () => {
  // Akshil QA, 2026-09-14: on a FOLDER pane every chat is about some file inside
  // it, so `taskPane` answers with a path for every row and the hop is the only
  // arm that ever runs. Seeding only the in-place arm meant the header on the
  // page that opens still waited out the whole 800-row listing — which is the
  // bug the seed was written for.
  resetSessionSeeds();
  const hops: string[] = [];
  const here = chat("s1");
  const there = chat("s2", { key: "s2", session_id: "s2", target: "/repo/other.py" });
  const r = mount(
    <Lists
      {...TABBED}
      recent={[here, there]}
      onOpen={() => {}}
      onNavigate={(u) => hops.push(u)}
    />,
  );
  const link = r.root.find((n) => n.type === "a");
  act(() =>
    (link.props as { onClick(e: unknown): void }).onClick({
      preventDefault() {},
      button: 0,
    }),
  );
  expect(hops.length).toBe(1);
  expect(sessionSeed("s2")?.key).toBe("s2");
  // …and the in-place arm still does too.
  const rows = r.root.findAll(
    (n) =>
      typeof n.type === "string" &&
      /(^| )tasks-row( |$)/.test(
        String((n.props as { className?: string }).className || ""),
      ),
  );
  act(() => (rows[0].props as { onClick(): void }).onClick());
  expect(sessionSeed("s1")?.key).toBe("s1");
  resetSessionSeeds();
});

// ---- EVERY HOST SHOWS UPCOMING, THE EXPLORER PANEL INCLUDED (design
// "drafts: one record", §6 — #1168 reverted) --------------------------------
//
// A draft is a task row with a TASK number on it, and a reader who opens the
// explorer's `?_side=claude` sidebar to talk about the file on screen is the
// reader most likely to have left half a sentence in that very folder's
// composer. Hiding the lane there hid the one row they could act on and made
// the panel disagree with List, Board and Cards about what exists. So there is
// no per-host filter left at all: `Lists` draws what it is given, and no host
// passes an opinion about lanes.

test("every host draws the Upcoming lane; no host can filter it out", () => {
  const rows = [
    chat("done1", { status: "done" }),
    chat("later1", { key: "later1", session_id: "later1", status: "upcoming" }),
    chatDraft(),
  ];
  const shown = mount(
    <Lists file="/repo/x.py" recent={rows} artifacts={[]} onOpen={() => {}} />,
  );
  expect(taskRows(shown).length).toBe(3);

  // The prop is GONE rather than merely unset by these hosts: the two explorer
  // panels and the two components between them carry no trace of it, so a
  // future embed cannot re-acquire the cut by copying a neighbour.
  for (const f of [
    "../ChatMount.tsx",
    "../ClaudeChat.tsx",
    "./Home.tsx",
    "./Lists.tsx",
    "../../explorer/Preview.tsx",
    "../../explorer/ListingPreviewPane.tsx",
  ]) {
    expect(readFileSync(new URL(f, import.meta.url), "utf8")).not.toContain(
      "hideUpcoming",
    );
  }
});
