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
/** Every URL asked this test. */
let calls: string[] = [];
const stubFetch = (async (url: unknown) => {
  const u = String(url);
  calls.push(u);
  const body = u.startsWith("/api/contrib/listing?") ? listingAnswer : { ok: false };
  return { ok: true, status: 200, json: async () => body };
}) as unknown as typeof fetch;

let r: ReactTestRenderer | null = null;
beforeEach(() => {
  listingAnswer = { ok: false };
  calls = [];
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
  expect(out).toContain("In the Community");
});

test("own app on rung 2: the repo sentence, and List it in the Community waits on Doctor", async () => {
  listingAnswer = {
    ok: true,
    state: "not-listed",
    sentence: "sine isn't in the Community yet.",
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
  expect(out).toContain("List it in the Community");
  expect(out).toContain("Turns on when Doctor is green");
  // The Listing card: Edit details on the title line, Last published Never.
  expect(out).toContain("Edit details");
  expect(out).not.toContain("Name, description, category");
  expect(out).toContain("Last published");
  expect(out).toContain("Never");
  expect(out).toContain("Fix with Claude");
  expect(out).toContain("Copy install link");
  // The List button is there but off until Doctor is green.
  const list = t.root.findAll((n) => n.type === "button" && textOf(n).includes("List it in the Community"));
  expect(list.length).toBeGreaterThan(0);
  expect(list.every((b) => b.props.disabled === true)).toBe(true);
});

test("listed under an older id than the app's own: one line says how to move it", async () => {
  listingAnswer = {
    ok: true,
    state: "live",
    sentence: "sine is in the Community.",
    listing: {
      listed: true,
      slug: "sine-starter",
      app_id: "sine-0a1b2c3d",
      slug_is_app_id: false,
      approved: { commit: "abc", at: null },
      pr: null,
      review: null,
      update_available: false,
      doctor_clear: true,
      unpushed: false,
    },
    actions: [],
  };
  const gh = {
    installed: true,
    signed_in: true,
    login: "vasu",
    repo: "vasu/sine",
    visibility: "public" as const,
    unpushed: 0,
    link: "https://udf.ai/x",
  };
  const t = await mount(
    <CommunityTab
      dir="/apps/sine"
      slug="sine"
      contrib={contribOf(null)}
      ladder={ladderOf({ rung: 3, kind: "own", github: gh })}
      variant="page"
    />,
  );
  expect(text(t)).toContain(
    "Listed under the old id sine-starter. Remove it, then publish again to move it to sine-0a1b2c3d.",
  );
});

test("own app with unpushed edits: one Publish now, List it waits on it", async () => {
  listingAnswer = {
    ok: true,
    state: "not-listed",
    sentence: "sine isn't in the Community yet. Your latest changes aren't on GitHub yet; the Community lists what is on GitHub.",
    listing: null,
    actions: [{ id: "publish-now", label: "Publish now", primary: true }],
  };
  const own: AppContrib = {
    ...COPY,
    copy: { ...COPY.copy, kind: "own", repo: "vasu/sine", author: "vasu", can_push: true },
    panel: {
      ...COPY.panel!,
      kind: "own",
      repo: "vasu/sine",
      who: "vasu",
      state: "unpublished",
      sentence: "1 edit not on GitHub yet.",
      files: [],
      unproposed: 0,
      unpublished: 1,
    },
    actions: [
      { id: "publish-now", label: "Publish now", primary: true },
      { id: "check", label: "Check now", primary: false },
    ],
  };
  const t = await mount(
    <CommunityTab
      dir="/apps/sine"
      slug="sine"
      contrib={contribOf(own)}
      ladder={ladderOf({
        rung: 2,
        kind: "own",
        github: {
          installed: true,
          signed_in: true,
          login: "vasu",
          repo: "vasu/sine",
          visibility: "public",
          unpushed: 1,
          link: "https://udf.ai/x",
        },
      })}
      variant="page"
    />,
  );
  const buttons = t.root.findAll((n) => n.type === "button").map(textOf);
  expect(buttons.filter((b) => b === "Publish now")).toHaveLength(1);
  expect(buttons.filter((b) => b.includes("List it in the Community"))).toHaveLength(1);
  const list = t.root.findAll((n) => n.type === "button" && textOf(n).includes("List it in the Community"));
  expect(list[0].props.disabled).toBe(true);
  expect(text(t)).toContain("Turns on once your edits are on GitHub.");
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
  expect(out).not.toContain("In the Community");
  expect(out).not.toContain("List it in the Community");
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

test("Activity names someone else's step and links to where it happened (§16.16)", async () => {
  const withLines = {
    ...COPY,
    activity: [
      {
        id: "2",
        at: 1_700_000_100,
        kind: "proposal-accepted",
        text: "sina accepted your proposal “Red”.",
        undo: false,
        actor: "sina",
        link: "https://github.com/sina/sine/pull/4",
      },
      { id: "1", at: 1_700_000_000, kind: "update", text: "Got sina's latest.", undo: false },
    ],
  } as AppContrib;
  const t = await mount(
    <CommunityTab
      dir="/apps/sine"
      slug="sine"
      contrib={contribOf(withLines)}
      ladder={ladderOf({ mine: false, kind: "github" })}
      variant="page"
    />,
  );
  const lines = t.root.findAll((n) => n.type === "li" && String(n.props.className).includes("contrib-activity-line"));
  expect(lines.map(textOf)).toEqual([
    expect.stringContaining("sina accepted your proposal “Red”."),
    expect.stringContaining("Got sina's latest."),
  ]);
  expect(String(lines[0].props.className)).toContain("is-someone");
  expect(String(lines[1].props.className)).not.toContain("is-someone");
  const links = lines[0].findAll((n) => n.type === "a").map((n) => n.props.href);
  expect(links).toEqual(["https://github.com/sina/sine/pull/4"]);
  expect(lines[1].findAll((n) => n.type === "a")).toHaveLength(0);
});

test("kept mounted behind another tab, a copy's new event is not marked seen until the tab is shown", async () => {
  const withEvent = { ...COPY, copy: { ...COPY.copy, event: { kind: "merged-back", at: 1, who: "sina" } } } as AppContrib;
  const el = (hidden: boolean) => (
    <CommunityTab
      dir="/apps/sine"
      slug="sine"
      contrib={contribOf(withEvent)}
      ladder={ladderOf({ mine: false, kind: "github" })}
      variant="page"
      hidden={hidden}
    />
  );
  const t = await mount(el(true));
  expect(calls.filter((u) => u.startsWith("/api/contrib/ack"))).toHaveLength(0);
  await act(async () => {
    t.update(el(false));
  });
  expect(calls.filter((u) => u.startsWith("/api/contrib/ack"))).toHaveLength(1);
});

test("the tab tells its host while one of its actions runs (the sheet stays open meanwhile)", async () => {
  const said: boolean[] = [];
  const t = await mount(
    <CommunityTab
      dir="/apps/sine"
      slug="sine"
      contrib={contribOf(COPY)}
      ladder={ladderOf({ mine: false, kind: "github" })}
      variant="sheet"
      onBusy={(b) => said.push(b)}
    />,
  );
  const check = t.root.findAll((n) => n.type === "button" && textOf(n) === "Check now")[0]!;
  await act(async () => {
    check.props.onClick();
    await new Promise((res) => setTimeout(res, 0));
  });
  expect(said[0]).toBe(false);
  expect(said).toContain(true);
  expect(said[said.length - 1]).toBe(false);
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
