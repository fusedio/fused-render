// The legacy-url rewrite runs in two places: module init (full page load) and
// navigateUrl (a stored bookmark/recents url clicked in-app, where no page
// load happens). A miss in the second path is invisible until an upgraded
// user clicks a pre-rename bookmark and lands on an unrecognized route.
import { describe, expect, test } from "bun:test";

import { installDomShim } from "./testDomShim";

// router.ts reads `location` at module scope; bun has no DOM. See
// testDomShim.ts for why this is the one shared stub every suite in the run
// installs, rather than a stub hand-rolled per file.
installDomShim();

const { navigate, navigateToJobPage, isJobPageRoute, rewriteLegacyUrl, withPreviewFlag } =
  await import("./router");
const { setResolvedSnapshot } = await import("./snapshot-param");
const setSnapshotAppDir = (appDir: string | null) =>
  setResolvedSnapshot(appDir ? { sha: "abc1234", dir: "/cache/k/abc1234", app_dir: appDir } : null);

// The url navigate() pushed, with the page sitting on `search` when it ran.
// navigate reads location.search live (the framing flag is carried FORWARD, not
// re-read from a boot-time constant), so the shim's search is the whole input.
//
// `fromDir` is the PROVENANCE of the page the hop is made from — the `{ fsDir }`
// hint the navigation that landed here stashed (navHintIsDir). It decides whether
// `_side` is the current page's to hand on, so a test that carries pane state has
// to say which kind of page it is standing on; `undefined` is the honest shape of a
// fresh load or a typed url, where nothing stashed a hint.
function pushedFrom(search: string, run: () => void, fromDir?: boolean): string {
  const loc = globalThis.location as { search: string };
  const hist = globalThis.history as {
    pushState: (state: unknown, title: string, url: string) => void;
    state: unknown;
  };
  const prevSearch = loc.search;
  const prevPush = hist.pushState;
  const prevState = hist.state;
  let pushed = "";
  loc.search = search;
  hist.state = typeof fromDir === "boolean" ? { fsDir: fromDir } : null;
  hist.pushState = (_state, _title, url) => {
    pushed = url;
  };
  try {
    run();
  } finally {
    hist.pushState = prevPush;
    hist.state = prevState;
    loc.search = prevSearch;
  }
  return pushed;
}

// The frozen-tree framing (`snapshot=1`) names what the PAGE is — a materialised
// snapshot embedded in a view's column — not what the destination looks like, so
// it has to survive every hop the framed listing makes. It is read once at boot
// (IS_SNAPSHOT), so the live session keeps behaving after a drop; a RELOAD or a
// copied url is where a dropped flag shows up, bringing back the breadcrumb into
// the snapshot cache's internals and a preview pane inside the preview pane.
describe("navigate carries the frozen-tree framing", () => {
  test("a folder hop inside a snapshot keeps snapshot=1", () => {
    const url = pushedFrom("?snapshot=1", () => navigate("/w/snap/docs", { isDir: true }));
    expect(url).toBe("/explorer/view/w/snap/docs?snapshot=1");
  });

  test("opening a FILE inside a snapshot keeps it too", () => {
    // The framed listing opens files as well as folders, and the breadcrumb the
    // flag suppresses is the same breadcrumb on a file view.
    const url = pushedFrom("?snapshot=1", () => navigate("/w/snap/a.md", { isDir: false }));
    expect(url).toBe("/explorer/view/w/snap/a.md?snapshot=1");
  });

  test("it rides alongside the sticky pane state and an explicit mode", () => {
    const url = pushedFrom(
      "?snapshot=1&_side=git&sort=size",
      () => navigate("/w/snap/docs", { isDir: true, mode: "claude" }),
      true, // standing on a FOLDER, so the pane state is this page's to hand on
    );
    expect(url).toContain("snapshot=1");
    expect(url).toContain("_side=git");
    expect(url).toContain("_mode=claude");
    // Ordinary view params are still dropped by a hop.
    expect(url).not.toContain("sort=size");
  });

  // `_side` is the PANE's state, and the pane only exists over a folder — so a
  // FOLDER-TO-FOLDER hop carries it and opening a file does not. The file view has a
  // `_side` of its own (Preview's companion sidebar), read the same way since D326
  // but describing a different column; each surface seeds its own from its own URL.
  test("the sticky pane state is folder-only", () => {
    expect(pushedFrom("?_side=git", () => navigate("/w/docs", { isDir: true }), true)).toBe(
      "/explorer/view/w/docs?_side=git",
    );
    expect(pushedFrom("?_side=git", () => navigate("/w/a.md", { isDir: false }), true)).toBe(
      "/explorer/view/w/a.md",
    );
  });

  // A FILE'S `_side` IS NOT THE FOLDER'S (D326). Both surfaces read the param the
  // same way now, and a closed sidebar is a value (`_side=off`) rather than an
  // absent param — so without a provenance check, closing a file's sidebar and then
  // taking the breadcrumb up landed on a folder with its pane SHUT, for a folder
  // that was open when you entered the file. The tell that this is a defect and not
  // just coupling: Back restores the folder's own url and its pane comes back, so
  // the two ways out of a file disagreed about what the folder looks like.
  test("a hop out of a FILE hands the folder nothing", () => {
    expect(pushedFrom("?_side=off", () => navigate("/w/docs", { isDir: true }), false)).toBe(
      "/explorer/view/w/docs",
    );
    // Not just the shut value — a companion named on a file is equally not the
    // pane's business.
    expect(pushedFrom("?_side=claude", () => navigate("/w/docs", { isDir: true }), false)).toBe(
      "/explorer/view/w/docs",
    );
  });

  test("a folder-to-folder hop still keeps the pane as the user left it", () => {
    // The whole point of the carry (FS-13): walking the tree does not silently
    // reopen or shut the pane.
    expect(pushedFrom("?_side=off", () => navigate("/w/docs", { isDir: true }), true)).toBe(
      "/explorer/view/w/docs?_side=off",
    );
  });

  test("unknown provenance hands nothing on", () => {
    // A fresh load, a typed url, or a caller that passed no hint: nothing stashed
    // `{ fsDir }`, so this page cannot claim the param is its own. Guessing wrong
    // in THIS direction reopens a pane at its documented default (an absent `_side`
    // means open); guessing the other way shuts a pane the user never shut, which
    // is the bug above. The cost is narrow and stated: shut the pane, hard-RELOAD,
    // then hop to a sibling folder, and the pane comes back open.
    expect(pushedFrom("?_side=off", () => navigate("/w/docs", { isDir: true }))).toBe(
      "/explorer/view/w/docs",
    );
  });

  test("an ordinary page invents no flag", () => {
    expect(pushedFrom("?sort=size", () => navigate("/w/docs", { isDir: true }))).toBe(
      "/explorer/view/w/docs",
    );
    // Only the exact "1" is the framing flag, same as IS_SNAPSHOT reads it.
    expect(pushedFrom("?snapshot=0", () => navigate("/w/docs", { isDir: true }))).toBe(
      "/explorer/view/w/docs",
    );
  });
});

// `_snapshot=<sha>` is the SECOND exception to the fresh-query rule, and unlike
// `snapshot=1` it is bounded: it names a commit, checked against the live app
// folder that commit was resolved against (getSnapshotAppDir(), set by
// whichever code last resolved /api/git/snapshot for this document — task 4).
describe("navigate carries a git snapshot only within its app folder", () => {
  test("an in-app hop keeps _snapshot", () => {
    setSnapshotAppDir("/w/myapp");
    try {
      const url = pushedFrom(
        "?_snapshot=abc1234",
        () => navigate("/w/myapp/sub/reader.py", { isDir: false }),
      );
      expect(url).toBe("/explorer/view/w/myapp/sub/reader.py?_snapshot=abc1234");
    } finally {
      setSnapshotAppDir(null);
    }
  });

  test("a hop out of the app folder drops it", () => {
    setSnapshotAppDir("/w/myapp");
    try {
      const url = pushedFrom(
        "?_snapshot=abc1234",
        () => navigate("/w", { isDir: true }),
      );
      expect(url).toBe("/explorer/view/w");
    } finally {
      setSnapshotAppDir(null);
    }
  });

  test("a hop to a different app folder drops it", () => {
    setSnapshotAppDir("/w/myapp");
    try {
      const url = pushedFrom(
        "?_snapshot=abc1234",
        () => navigate("/w/otherapp", { isDir: true }),
      );
      expect(url).toBe("/explorer/view/w/otherapp");
    } finally {
      setSnapshotAppDir(null);
    }
  });

  test("never invented where nothing has resolved an app folder yet", () => {
    // getSnapshotAppDir() defaults to null (nothing has resolved it in this
    // test run) — the param must not be invented off the URL alone.
    const url = pushedFrom(
      "?_snapshot=abc1234",
      () => navigate("/w/myapp/sub/reader.py", { isDir: false }),
    );
    expect(url).toBe("/explorer/view/w/myapp/sub/reader.py");
  });
});

test("legacy view/embed prefixes gain the /explorer namespace", () => {
  expect(rewriteLegacyUrl("/view/Users/me/data.csv")).toBe("/explorer/view/Users/me/data.csv");
  expect(rewriteLegacyUrl("/embed/Users/me/map.html")).toBe("/explorer/embed/Users/me/map.html");
  // Saved view params (the whole point of a bookmark url) ride along.
  expect(rewriteLegacyUrl("/view/Users/me/d?sort=size&_mode=grid")).toBe(
    "/explorer/view/Users/me/d?sort=size&_mode=grid",
  );
});

test("legacy settings sentinels map to their plain routes", () => {
  expect(rewriteLegacyUrl("/view/_home")).toBe("/apps");
  expect(rewriteLegacyUrl("/view/_prefs")).toBe("/preferences");
  // The Fused account tab is gone (with the Deploy feature it existed for) —
  // an old bookmark just lands on Preferences' default tab now.
  expect(rewriteLegacyUrl("/view/_account")).toBe("/preferences");
});

// `?tab=engines` used to be redirected to /ai-models — the Inference engines
// tab moved pages, and a sentinel table keyed on pathname alone cannot say so.
// The rule is GONE with the `?tab=` scheme it pointed at: AI Models names its
// tabs in the path now (apps/ai_models/routes.ts), and the migration
// deliberately carries no alias for either the old query or the old
// destination. This test pins that the query is now passed through untouched,
// so a future reader finds the decision rather than a silent absence.
test("a Preferences tab query is carried, not rewritten", () => {
  expect(rewriteLegacyUrl("/preferences?tab=engines")).toBe("/preferences?tab=engines");
  // Through the OLDER shape too: the sentinel maps the path and leaves the
  // query alone.
  expect(rewriteLegacyUrl("/view/_prefs?tab=engines")).toBe("/preferences?tab=engines");
  expect(rewriteLegacyUrl("/preferences?tab=indexing")).toBe("/preferences?tab=indexing");
  expect(rewriteLegacyUrl("/preferences")).toBe("/preferences");
});

test("current urls pass through untouched", () => {
  expect(rewriteLegacyUrl("/explorer/view/Users/me/x")).toBe("/explorer/view/Users/me/x");
  expect(rewriteLegacyUrl("/apps/local/demo?_mode=claude")).toBe(
    "/apps/local/demo?_mode=claude",
  );
  // A file legitimately named view/ deeper in the path must not rewrite.
  expect(rewriteLegacyUrl("/explorer/view/w/view/f")).toBe("/explorer/view/w/view/f");
});

// The `_preview=1` thumbnail stamp (D301: GET /render records an app open by
// default; a card peek must say "I am a picture"). A miss here is invisible
// until the /apps hub's recency order rearranges itself as cards scroll by.
test("withPreviewFlag stamps the thumbnail param", () => {
  expect(withPreviewFlag("/explorer/embed/w/app/index.html")).toBe(
    "/explorer/embed/w/app/index.html?_preview=1",
  );
  // A bookmark's stored query (the saved view params) rides along.
  expect(withPreviewFlag("/explorer/embed/w/d?sort=size")).toBe(
    "/explorer/embed/w/d?sort=size&_preview=1",
  );
  // Idempotent: cards rebuild src every render; accumulating would reload.
  const once = withPreviewFlag("/render?path=x");
  expect(withPreviewFlag(once)).toBe(once);
});

// Job.page carries either a shell route or an absolute fs path —
// syntactically indistinguishable, since both start with "/".
// navigateToJobPage is the one place that turns either shape into an actual
// navigation, checking a small closed table of the routes a job can
// legitimately name before falling back to treating the value as an fs path.
function pushedJobPage(page: string): { url: string; state: unknown } {
  const hist = globalThis.history as {
    pushState: (state: unknown, title: string, url: string) => void;
  };
  const prevPush = hist.pushState;
  let url = "";
  let state: unknown;
  hist.pushState = (s, _t, u) => {
    url = u;
    state = s;
  };
  try {
    navigateToJobPage(page);
  } finally {
    hist.pushState = prevPush;
  }
  return { url, state };
}

describe("navigateToJobPage dispatches a Job.page value", () => {
  test("a known shell route goes through navigateUrl untouched", () => {
    expect(pushedJobPage("/ai-models/local").url).toBe("/ai-models/local");
    expect(pushedJobPage("/ai-models/benchmark").url).toBe("/ai-models/benchmark");
    expect(pushedJobPage("/claude-config").url).toBe("/claude-config");
    expect(pushedJobPage("/preferences").url).toBe("/preferences");
    expect(pushedJobPage("/preferences?tab=indexing").url).toBe("/preferences?tab=indexing");
  });

  test("anything else is an fs path — a directory unless it ends in .htm(l)", () => {
    const repoRoot = pushedJobPage("/Users/me/Work/widget");
    expect(repoRoot.url).toBe("/explorer/view/Users/me/Work/widget");
    expect(repoRoot.state).toEqual({ fsDir: true });

    const page = pushedJobPage("/Users/me/Work/widget/index.html");
    expect(page.url).toBe("/explorer/view/Users/me/Work/widget/index.html");
    expect(page.state).toEqual({ fsDir: false });
  });

  test("a rendered output file paints as a file, not a directory — its own extension is the tell, not just .htm(l)", () => {
    const png = pushedJobPage("/Users/me/Desktop/render.png");
    expect(png.state).toEqual({ fsDir: false });
    const video = pushedJobPage("/Users/me/Desktop/render.mp4");
    expect(video.state).toEqual({ fsDir: false });
    // A folder name that happens to contain a dot (a version suffix, say) is
    // still a directory: the test is on the BASENAME's extension, not on
    // whether "." appears anywhere in the path.
    const dotDir = pushedJobPage("/Users/me/Work/widget-v1.2/output");
    expect(dotDir.state).toEqual({ fsDir: true });
  });

  test("a dotted folder NAME itself paints as a directory, not a file — the extension list is closed", () => {
    // "any dot with no further '/' or '.' after it" would call each of these
    // a file; none of them is one — a domain-style repo root
    // (github_setup.py), a dotfile-style project directory (envinstall.py's
    // project_dir), and a versioned folder are all real, legitimate
    // directory names a producer can hand back as `page`.
    expect(pushedJobPage("/Users/me/Work/site.com").state).toEqual({ fsDir: true });
    expect(pushedJobPage("/Users/me/Work/app.v2").state).toEqual({ fsDir: true });
    expect(pushedJobPage("/Users/me/.config").state).toEqual({ fsDir: true });
  });
});

describe("isJobPageRoute", () => {
  test("is true for every known shell route", () => {
    expect(isJobPageRoute("/ai-models/local")).toBe(true);
    expect(isJobPageRoute("/ai-models/benchmark")).toBe(true);
    expect(isJobPageRoute("/claude-config")).toBe(true);
    expect(isJobPageRoute("/preferences")).toBe(true);
    expect(isJobPageRoute("/preferences?tab=indexing")).toBe(true);
    expect(isJobPageRoute("/tasks")).toBe(true);
  });

  test("is false for an fs path, even one that looks route-like", () => {
    expect(isJobPageRoute("/Users/me/Work/widget")).toBe(false);
    expect(isJobPageRoute("/Users/me/Work/widget/index.html")).toBe(false);
  });
});
