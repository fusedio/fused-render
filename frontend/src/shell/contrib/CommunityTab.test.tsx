// CommunityTab (CONTRIB_LOOP.md §19.4/§19.7): the own app's ladder and a
// copy's other-side view, rendered from hand-built answers. The page's ladder
// read is handed in (`ladder` prop), the contrib state is a plain
// AppContribState, and fetch is stubbed directly (no mock.module, which is
// process-wide in bun): the listing route answers per test, every other read
// answers `{ok:false}`.
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { ReactElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";
import type { AppContrib, AppContribState, LadderAnswer, LadderState, ListingAnswer } from "@platform/lib/contrib";

installDomShim();
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { CommunityTab, communityBadge, communityLine, copyCardOf } = await import("@shell/contrib/CommunityTab");

const realFetch = globalThis.fetch;
let listingAnswer: ListingAnswer = { ok: false };
const stubFetch = (async (url: unknown) => {
  const u = String(url);
  const body = u.startsWith("/api/contrib/listing?") ? listingAnswer : { ok: false };
  return { ok: true, status: 200, json: async () => body };
}) as unknown as typeof fetch;

let r: ReactTestRenderer | null = null;
beforeEach(() => {
  listingAnswer = { ok: false };
  globalThis.fetch = stubFetch;
});
afterEach(() => {
  act(() => r?.unmount());
  r = null;
  globalThis.fetch = realFetch;
});

const noop = async () => {};

function ladderOf(over: Partial<LadderAnswer>): LadderState {
  const data: LadderAnswer = {
    ok: true,
    rung: 1,
    kind: "scratch",
    mine: true,
    github: {
      installed: true,
      signed_in: true,
      login: "vasu",
      repo: null,
      visibility: null,
      unpushed: 0,
      link: null,
    },
    listing: null,
    doctor: { clear: true, failing: 0 },
    flags: { sources: true, marketplace: true, github_link: true },
    ...over,
  };
  return { data, refresh: noop };
}

function contribOf(data: AppContrib | null): AppContribState {
  return { data, settled: true, answered: true, refresh: noop };
}

const FLAGS = { sources: true, changes: true, inbox: true, loop: true, remix: true };

async function mount(el: ReactElement) {
  await act(async () => {
    r = create(el);
  });
  // Let the listing read land.
  await act(async () => {
    await new Promise((res) => setTimeout(res, 0));
  });
  return r!;
}
const text = (t: ReactTestRenderer) => JSON.stringify(t.toJSON());
type Node = ReactTestRenderer["root"];
/** A rendered node's text, children's included. */
const textOf = (n: Node | string): string =>
  typeof n === "string" ? n : n.children.map((c) => textOf(c as Node | string)).join("");
const rungs = (t: ReactTestRenderer) =>
  t.root.findAll((n) => n.type === "section" && typeof n.props["data-rung"] === "number");
const current = (t: ReactTestRenderer) => rungs(t).filter((n) => n.props["aria-current"] === "step");

test("own app on rung 1: three rungs, the first current, its button is Put it on GitHub", async () => {
  const t = await mount(
    <CommunityTab dir="/apps/sine" slug="sine" contrib={contribOf(null)} ladder={ladderOf({})} variant="page" />,
  );
  expect(rungs(t).map((n) => n.props["data-rung"])).toEqual([1, 2, 3]);
  expect(current(t).map((n) => n.props["data-rung"])).toEqual([1]);
  const out = text(t);
  expect(out).toContain("On this Mac");
  expect(out).toContain("Only you have it.");
  expect(out).toContain("Put it on GitHub");
  expect(out).toContain("On GitHub");
  expect(out).toContain("In the Marketplace");
});

test("own app on rung 2: the repo sentence, and List it in the Marketplace waits on Doctor", async () => {
  listingAnswer = {
    ok: true,
    state: "not-listed",
    sentence: "sine isn't in the Marketplace yet.",
    listing: null,
    actions: [{ id: "open-doctor", label: "Open App Doctor", primary: true, dialog: "doctor" }],
  };
  const t = await mount(
    <CommunityTab
      dir="/apps/sine"
      slug="sine"
      contrib={contribOf(null)}
      ladder={ladderOf({
        rung: 2,
        kind: "own",
        github: {
          installed: true,
          signed_in: true,
          login: "vasu",
          repo: "vasu/sine",
          visibility: "public",
          unpushed: 2,
          link: "https://udf.ai/x",
        },
        doctor: { clear: false, failing: 3 },
      })}
      variant="page"
    />,
  );
  expect(current(t).map((n) => n.props["data-rung"])).toEqual([2]);
  const out = text(t);
  expect(out).toContain("vasu/sine");
  expect(out).toContain("Public · 2 edits not pushed yet.");
  expect(out).toContain("List it in the Marketplace");
  expect(out).toContain("Turns on when Doctor is green");
  expect(out).toContain("Fix with Claude");
  expect(out).toContain("Copy install link");
  // The List button is there but off until Doctor is green.
  const list = t.root.findAll((n) => n.type === "button" && textOf(n).includes("List it in the Marketplace"));
  expect(list.length).toBeGreaterThan(0);
  expect(list.every((b) => b.props.disabled === true)).toBe(true);
});

test("marketplace switch off: two rungs, and rung 2's next action is Copy install link", async () => {
  const t = await mount(
    <CommunityTab
      dir="/apps/sine"
      slug="sine"
      contrib={contribOf(null)}
      ladder={ladderOf({
        rung: 2,
        kind: "own",
        github: {
          installed: true,
          signed_in: true,
          login: "vasu",
          repo: "vasu/sine",
          visibility: "public",
          unpushed: 0,
          link: "https://udf.ai/x",
        },
        flags: { sources: true, marketplace: false, github_link: true },
      })}
      variant="page"
    />,
  );
  expect(rungs(t).map((n) => n.props["data-rung"])).toEqual([1, 2]);
  const out = text(t);
  expect(out).not.toContain("In the Marketplace");
  expect(out).not.toContain("List it in the Marketplace");
  expect(out).toContain("Copy install link");
});

const COPY: AppContrib = {
  flags: FLAGS,
  copy: {
    kind: "github",
    state: "changes",
    repo: "sina/sine",
    author: "sina",
    remix_of: null,
    changes: 3,
    dirty: true,
    unproposed: 3,
    needs_merge: false,
    merge_job: null,
    event: null,
    original_update: false,
    can_push: false,
  } as AppContrib["copy"],
  proposal: null,
  inbox: null,
  contributors: null,
  github: { signed_in: true, login: "bob" },
  share_url: null,
  panel: {
    kind: "github",
    repo: "sina/sine",
    who: "sina",
    last_checked: null,
    state: "edits",
    sentence: "You changed 3 files of sina's app.",
    files: ["index.html"],
    unproposed: 3,
    unpublished: 0,
    github_new: 0,
  },
  actions: [
    { id: "propose", label: "Propose to sina", primary: true, dialog: "propose" },
    { id: "discard", label: "Discard my changes", primary: false, danger: true },
    { id: "check", label: "Check now", primary: false },
  ],
  activity: [],
};

test("a copy of someone else's app: It comes from / Your changes / Your proposal", async () => {
  const t = await mount(
    <CommunityTab
      dir="/apps/sine"
      slug="sine"
      contrib={contribOf(COPY)}
      ladder={ladderOf({ mine: false, kind: "github" })}
      variant="page"
    />,
  );
  expect(rungs(t).map((n) => n.props["data-rung"])).toEqual([1, 2, 3]);
  const out = text(t);
  expect(out).toContain("It comes from");
  expect(out).toContain("Your changes");
  expect(out).toContain("Your proposal");
  expect(out).toContain("You changed 3 files of sina's app.");
  expect(out).toContain("3 files changed");
  // The server's buttons, split by subject: Propose and Discard under Your
  // changes, Check now in the source card's foot.
  const changes = rungs(t).find((n) => n.props["data-rung"] === 2)!;
  expect(changes.findAll((n) => n.type === "button").map(textOf)).toContain("Propose to sina");
  const source = rungs(t).find((n) => n.props["data-rung"] === 1)!;
  expect(source.findAll((n) => n.type === "button").map(textOf)).toContain("Check now");
  // No ladder words on the other side.
  expect(out).not.toContain("Put it on GitHub");
});

test("copyCardOf: unknown ids stay with It comes from", () => {
  expect(copyCardOf("propose")).toBe("changes");
  expect(copyCardOf("update-proposal")).toBe("proposal");
  expect(copyCardOf("bring-in")).toBe("source");
  expect(copyCardOf("something-new")).toBe("source");
});

test("badge and Overview line: waiting proposals on an own app, a dot for a copy's event", () => {
  const own = {
    ...COPY,
    copy: { ...COPY.copy, kind: "own" as const },
    inbox: { items: [], count: 2 },
    panel: { ...COPY.panel!, state: "published", sentence: "Published." },
  };
  const ladder = ladderOf({ rung: 2 }).data;
  expect(communityBadge(contribOf(own), ladder)).toBe(2);
  expect(communityLine(contribOf(own), ladder)).toBe("2 proposals waiting for you");
  const copy = { ...COPY, copy: { ...COPY.copy, event: { kind: "merged-back", at: 1, who: "sina" } } } as AppContrib;
  expect(communityBadge(contribOf(copy), ladderOf({ mine: false }).data)).toBe("dot");
  expect(communityLine(contribOf(copy), null)).toBe("You changed 3 files of sina's app.");
  // An own app that is all caught up: nothing to say.
  const quiet = { ...own, inbox: { items: [], count: 0 } };
  expect(communityLine(contribOf(quiet), ladder)).toBeNull();
});
