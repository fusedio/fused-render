// The Share sheet's GitHub card on the ladder (docs/CONTRIB_LOOP.md §19.3):
// its three states, one per rung.
//
// `GithubLadderCard` is rendered on its own, not through `<ShareAppModal>`:
// the sheet's chrome is Base UI's Dialog, which portals through a real
// `ReactDOM.createPortal` that react-test-renderer cannot host
// (ShareFileModal.test.tsx records the same finding and takes the same way
// round it). The card is the part §19.3 changes; the sheet only decides
// whether to show it (an own app's ladder) and where.
//
// Mocked at the `fetch` layer with a route-keyed stub, not via `mock.module`,
// for the reason ShareFileModal.test.tsx gives: `mock.module` replaces a
// module for the whole bun process and contaminates unrelated suites.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import type { LadderAnswer } from "@platform/lib/contrib";

// Dynamic: a static import is hoisted above installDomShim(), and the module
// pulls router.ts in, which reads `location` at init.
const { GithubLadderCard, communityTabUrl } = await import("@platform/ui/ShareAppModal");

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

const DIR = "/Users/me/Fused/local/Weather Board";

/** Route-keyed fetch stub (ShareFileModal.test.tsx's): a URL path → JSON
 *  body; anything unlisted is a loud 404. Records every path asked. */
function stubFetch(routes: Record<string, unknown>) {
  const asked: string[] = [];
  globalThis.fetch = (async (url: string) => {
    const path = url.split("?")[0]!;
    asked.push(path);
    const body = routes[path];
    if (body === undefined) return new Response(JSON.stringify({ error: "unmocked" }), { status: 404 });
    return new Response(JSON.stringify(body), { status: 200 });
  }) as unknown as typeof fetch;
  return asked;
}

const IDLE = { state: "idle", detail: "", error: null, started_at: null, finished_at: null };
const GITHUB_ROUTES = {
  "/api/github/status": {
    found: true,
    path: "/opt/homebrew/bin/gh",
    source: "path",
    version: "2.62.0",
    signed_in: true,
    account: "octocat",
    checked_at: 1,
    stale: false,
  },
  "/api/github/install": IDLE,
  "/api/github/publish": { ...IDLE, root: "" },
  "/api/github/login": { in_flight: false, started_at: null, error: null, code: null, url: null },
};

function ladder(over: Partial<LadderAnswer>): LadderAnswer {
  return {
    ok: true,
    rung: 1,
    kind: "scratch",
    mine: true,
    github: {
      installed: true,
      signed_in: true,
      login: "octocat",
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
}

async function mount(l: LadderAnswer): Promise<ReactTestRenderer> {
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(
      createElement(GithubLadderCard, {
        dir: DIR,
        ladder: l,
        reload: async () => l,
        primary: true,
        locked: false,
        onClose: () => {},
      }),
    );
  });
  // Let the open's reads (status + the three records) land.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  return r;
}

/** Every string rendered under `node`, joined — the card's visible text. */
function textOf(node: ReactTestInstance): string {
  return node.children.map((c) => (typeof c === "string" ? c : textOf(c))).join("");
}

function buttons(r: ReactTestRenderer): ReactTestInstance[] {
  return r.root.findAll((n) => n.type === "button");
}

function buttonLabelled(r: ReactTestRenderer, label: string): ReactTestInstance | undefined {
  return buttons(r).find((b) => textOf(b).trim() === label);
}

test("rung 1, every check green: three ticked rows, the repo name slugged, Public, one enabled Put it on GitHub", async () => {
  const asked = stubFetch(GITHUB_ROUTES);
  const r = await mount(ladder({ rung: 1 }));
  const text = textOf(r.root);

  const rows = r.root.findAll((n) => n.type === "div" && n.props["data-check"] !== undefined);
  expect(rows.map((n) => n.props["data-check"])).toEqual(["done", "done", "done"]);
  expect(text).toContain("GitHub CLI");
  expect(text).toContain("Installed · 2.62.0");
  expect(text).toContain("Signed in as octocat");
  expect(text).toContain("github.com/octocat/");
  expect(text).toContain("Public is needed for the Community.");

  const nameInput = r.root.find((n) => n.type === "input" && n.props["aria-label"] === "Repository name on GitHub");
  expect(nameInput.props.value).toBe("weather-board");

  // Nothing to install or sign in to: the only action is the publish.
  expect(buttonLabelled(r, "Install")).toBeUndefined();
  expect(buttonLabelled(r, "Sign in")).toBeUndefined();
  const put = buttonLabelled(r, "Put it on GitHub");
  expect(put).toBeDefined();
  expect(put!.props.disabled).toBeFalsy();

  // Rung 1 reads the status and the three records at open — and never the
  // Git panel's deep link.
  expect(asked).toContain("/api/github/status");
  expect(asked).toContain("/api/github/publish");
  expect(text).not.toContain("Open Community");
  await act(async () => r.unmount());
});

test("rung 1 without the CLI: the CLI row offers Install, Sign in waits on it, the publish is disabled", async () => {
  stubFetch({
    ...GITHUB_ROUTES,
    "/api/github/status": { ...GITHUB_ROUTES["/api/github/status"], found: false, signed_in: false, account: null, version: null },
  });
  const r = await mount(
    ladder({ rung: 1, github: { ...ladder({}).github, installed: false, signed_in: false, login: null } }),
  );
  const rows = r.root.findAll((n) => n.type === "div" && n.props["data-check"] !== undefined);
  expect(rows.map((n) => n.props["data-check"])).toEqual(["todo", "todo", "todo"]);
  expect(buttonLabelled(r, "Install")!.props.disabled).toBeFalsy();
  expect(buttonLabelled(r, "Sign in")!.props.disabled).toBeTruthy();
  expect(buttonLabelled(r, "Put it on GitHub")!.props.disabled).toBeTruthy();
  expect(textOf(r.root)).toContain("github.com/you/");
  await act(async () => r.unmount());
});

test("rung 1 with a refusal ahead (nested): the server's sentence up front, the publish disabled", async () => {
  stubFetch(GITHUB_ROUTES);
  const nested = "This app lives inside a bigger project on GitHub, so it can't have a repository of its own.";
  const r = await mount(ladder({ rung: 1, github: { ...ladder({}).github, blocked: nested } }));
  expect(textOf(r.root)).toContain(nested);
  expect(buttonLabelled(r, "Put it on GitHub")!.props.disabled).toBeTruthy();
  await act(async () => r.unmount());
});

test("rung 2 with Doctor's verdict unknown (clear null): no 'fix first' line", async () => {
  stubFetch(GITHUB_ROUTES);
  const r = await mount(
    ladder({
      rung: 2,
      github: { ...ladder({}).github, repo: "octocat/weather-board", visibility: "public", link: "https://x" },
      doctor: { clear: null, failing: null },
    }),
  );
  const strip = r.root.find((n) => n.type === "div" && n.props["data-strip"] === "next");
  expect(textOf(strip)).not.toContain("Doctor has");
  await act(async () => r.unmount());
});

test("rung 2, not listed: the install link with Copy and Open, and the Next strip to Community", async () => {
  const asked = stubFetch(GITHUB_ROUTES);
  const link = "https://udf.ai/install/github/octocat/weather-board";
  const r = await mount(
    ladder({
      rung: 2,
      kind: "own",
      github: { ...ladder({}).github, repo: "octocat/weather-board", visibility: "public", link },
    }),
  );
  const text = textOf(r.root);

  expect(text).toContain("On GitHub as octocat/weather-board · public.");
  const linkInput = r.root.find((n) => n.type === "input" && n.props["aria-label"] === "GitHub link");
  expect(linkInput.props.value).toBe(link);
  expect(buttonLabelled(r, "Copy link")).toBeDefined();
  expect(r.root.findAll((n) => n.type === "a" && n.props.href === link).length).toBe(1);

  const strip = r.root.find((n) => n.type === "div" && n.props["data-strip"] === "next");
  expect(textOf(strip)).toContain("Next: list it in the Community");
  expect(textOf(strip)).toContain("Open Community");
  expect(textOf(strip)).not.toContain("Doctor has");
  expect(text).not.toContain("Put it on GitHub");
  // Rung 2 asks nothing of the setup records.
  expect(asked).not.toContain("/api/github/status");
  await act(async () => r.unmount());
});

test("rung 2 with Doctor failing: the strip says how many checks are left", async () => {
  stubFetch(GITHUB_ROUTES);
  const r = await mount(
    ladder({
      rung: 2,
      github: { ...ladder({}).github, repo: "octocat/weather-board", visibility: "public", link: "https://x" },
      doctor: { clear: false, failing: 2 },
    }),
  );
  const strip = r.root.find((n) => n.type === "div" && n.props["data-strip"] === "next");
  expect(textOf(strip)).toContain("Doctor has 2 checks to fix first.");
  await act(async () => r.unmount());
});

test("rung 3, live: the strip names the live version, with Open Community", async () => {
  stubFetch(GITHUB_ROUTES);
  const r = await mount(
    ladder({
      rung: 3,
      kind: "own",
      github: { ...ladder({}).github, repo: "octocat/weather-board", visibility: "public", link: "https://x" },
      listing: {
        ok: true,
        state: "live",
        sentence: "Weather Board is in the Community.",
        listing: {
          listed: true,
          slug: "weather-board",
          approved: { commit: "a".repeat(40), at: "2026-10-20", version: "1.2.0" } as never,
          pr: null,
          review: { status: "live", reasons: [], new_powers: [], whats_new: null },
          update_available: false,
          doctor_clear: true,
          unpushed: false,
        },
      },
    }),
  );
  const strip = r.root.find((n) => n.type === "div" && n.props["data-strip"] === "listing");
  expect(textOf(strip)).toContain("Live · v1.2.0");
  expect(textOf(strip)).toContain("Open Community");
  expect(textOf(r.root)).not.toContain("Next: list it in the Community");
  await act(async () => r.unmount());
});

test("rung 3, in review: the strip says so", async () => {
  stubFetch(GITHUB_ROUTES);
  const r = await mount(
    ladder({
      rung: 3,
      github: { ...ladder({}).github, repo: "octocat/weather-board", visibility: "public", link: null },
      listing: {
        ok: true,
        state: "in-review",
        sentence: "Fused is reviewing Weather Board.",
        listing: {
          listed: false,
          slug: "weather-board",
          approved: null,
          pr: { number: 7, url: "https://github.com/x/pull/7", head_sha: "b", state: "open" },
          review: { status: "in-review", reasons: [], new_powers: [], whats_new: null },
          update_available: false,
          doctor_clear: true,
          unpushed: false,
        },
      },
    }),
  );
  const strip = r.root.find((n) => n.type === "div" && n.props["data-strip"] === "listing");
  expect(textOf(strip)).toContain("In review");
  expect(textOf(strip)).toContain("Fused is reviewing Weather Board.");
  await act(async () => r.unmount());
});

const UNPUSHED_RUNG_2 = () =>
  ladder({
    rung: 2,
    kind: "own",
    github: { ...ladder({}).github, repo: "octocat/weather-board", visibility: "public", unpushed: 3, link: null },
  });

test("rung 2 with unpushed edits: Publish changes runs the Source panel's own-app action (publish-now)", async () => {
  const asked = stubFetch({
    ...GITHUB_ROUTES,
    "/api/contrib/app": {
      ok: true,
      actions: [
        { id: "publish-now", label: "Publish now", primary: true },
        { id: "check", label: "Check now", primary: false },
      ],
    },
  });
  const r = await mount(UNPUSHED_RUNG_2());
  expect(asked).toContain("/api/contrib/app");
  expect(textOf(r.root)).toContain("Your latest edits aren't on GitHub yet.");
  expect(buttonLabelled(r, "Publish changes")).toBeDefined();
  expect(buttonLabelled(r, "Show changes")).toBeUndefined();
  await act(async () => r.unmount());
});

test("rung 2 with unpushed edits and no publish action offered: Show changes (the git view)", async () => {
  stubFetch({ ...GITHUB_ROUTES, "/api/contrib/app": { ok: true, actions: [] } });
  const r = await mount(UNPUSHED_RUNG_2());
  expect(textOf(r.root)).toContain("Your latest edits aren't on GitHub yet.");
  expect(buttonLabelled(r, "Show changes")).toBeDefined();
  expect(buttonLabelled(r, "Publish changes")).toBeUndefined();
  await act(async () => r.unmount());
});

test("with the Community off, rung 2 has no Next strip", async () => {
  stubFetch(GITHUB_ROUTES);
  const r = await mount(
    ladder({
      rung: 2,
      github: { ...ladder({}).github, repo: "octocat/weather-board", visibility: "public", link: "https://x" },
      flags: { sources: true, marketplace: false, github_link: true },
    }),
  );
  expect(r.root.findAll((n) => n.type === "div" && n.props["data-strip"] !== undefined)).toHaveLength(0);
  expect(buttonLabelled(r, "Copy install link")).toBeDefined();
  await act(async () => r.unmount());
});

test("with the GitHub link switch off, rung 2 hides only the install link; the Next strip stays", async () => {
  stubFetch(GITHUB_ROUTES);
  const r = await mount(
    ladder({
      rung: 2,
      github: { ...ladder({}).github, repo: "octocat/weather-board", visibility: "public", link: "https://x" },
      flags: { sources: false, marketplace: true, github_link: false },
    }),
  );
  expect(r.root.findAll((n) => n.type === "input" && n.props["aria-label"] === "GitHub link")).toHaveLength(0);
  expect(buttonLabelled(r, "Copy link")).toBeUndefined();
  expect(textOf(r.root)).toContain("On GitHub as octocat/weather-board");
  expect(r.root.findAll((n) => n.type === "div" && n.props["data-strip"] === "next")).toHaveLength(1);
  await act(async () => r.unmount());
});

test("Open Community goes to the app page's community tab", () => {
  expect(communityTabUrl("/Users/me/Fused/local/Weather Board/")).toBe(
    "/apps/Users/me/Fused/local/Weather%20Board?_tab=community",
  );
});
