import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { BUILD_RE, _setBuildsState, _setRowsOnly, adoptBuilds, getBuilds, buildFilterCss, buildPrompt, chipFor, isBuild, mine, setBuildsRoot, slugOf, updatePrompt } from "../builds/builds";
import { observeStatus } from "../builds/tasks";
import type { TaskRow } from "../builds/tasks";
import { agoShort, anyAppOpen, appEmbed, appEmbedBase, appFromText, appOpenUrl, appStateParams, applyAppParams, clearAppParams, holdAppParams } from "./apps";

const ROOT = "/Users/me/Fused/app";

describe("appFromText", () => {
  test("plain link", () => {
    expect(appFromText(`ready: http://127.0.0.1:8765/render?path=${encodeURIComponent(ROOT + "/invoice-tracker/index.html")} .`, ROOT))
      .toEqual({ name: "Invoice tracker", dir: ROOT + "/invoice-tracker", params: "" });
  });
  test("link with params keeps the app's state, drops _* and path", () => {
    const t = `/render?path=${encodeURIComponent(ROOT + "/my_map/index.html")}&tab=2&q=a+b&_preview=1&path=x`;
    expect(appFromText(t, ROOT)).toEqual({ name: "My map", dir: ROOT + "/my_map", params: "tab=2&q=a+b" });
  });
  test("folder link without index.html, trailing slash", () => {
    expect(appFromText(`see (/render?path=${ROOT}/notes/)`, ROOT)).toEqual({ name: "Notes", dir: ROOT + "/notes", params: "" });
  });
  test("a file deeper in the app still names the app folder", () => {
    expect(appFromText(`/render?path=${encodeURIComponent(ROOT + "/notes/sub/page.html")}`, ROOT)?.dir).toBe(ROOT + "/notes");
  });
  test("outside the root, the root itself, no root, no link → null", () => {
    expect(appFromText(`/render?path=${encodeURIComponent("/Users/me/other/x/index.html")}`, ROOT)).toBeNull();
    expect(appFromText(`/render?path=${encodeURIComponent(ROOT + "-old/x/index.html")}`, ROOT)).toBeNull();
    expect(appFromText(`/render?path=${encodeURIComponent(ROOT + "/index.html")}`, ROOT)).toBeNull();
    expect(appFromText(`/render?path=${encodeURIComponent(ROOT + "/x/index.html")}`, "")).toBeNull();
    expect(appFromText("no link here", ROOT)).toBeNull();
  });
});

describe("app URLs", () => {
  test("/render carries the path as one value and appends params with &", () => {
    expect(appEmbedBase(ROOT + "/a b")).toBe(`/render?path=${encodeURIComponent(ROOT + "/a b/index.html")}`);
    expect(appEmbedBase(ROOT + "/a", "tab=2")).toBe(`/render?path=${encodeURIComponent(ROOT + "/a/index.html")}&tab=2`);
    expect(appEmbed(ROOT + "/a")).toBe(`/render?path=${encodeURIComponent(ROOT + "/a/index.html")}&_preview=1&_nofocus=1&_noopen=1`);
    expect(appOpenUrl(ROOT + "/a", "tab=2")).toBe(`/render?path=${encodeURIComponent(ROOT + "/a/index.html")}&tab=2`);
  });
  test("agoShort", () => {
    const now = Date.now() / 1000;
    expect(agoShort(now)).toBe("just now");
    expect(agoShort(now - 300)).toBe("5 min ago");
    expect(agoShort(now - 7200)).toBe("2 h ago");
    expect(agoShort(now - 3 * 86400)).toBe("3 d ago");
  });
});

describe("host params bridge", () => {
  const g = globalThis as Record<string, unknown>;
  const saved: Record<string, unknown> = {};
  let events: string[] = [];
  beforeEach(() => {
    for (const k of ["location", "history", "window"]) saved[k] = g[k];
    const loc = { href: "http://127.0.0.1:8765/?bot=b1", search: "?bot=b1", origin: "http://127.0.0.1:8765" };
    g.location = loc;
    g.history = { state: null, replaceState: (_s: unknown, _t: string, url: string) => { const u = new URL(url); loc.href = u.href; loc.search = u.search; } };
    events = [];
    g.window = { dispatchEvent: (e: Event) => { events.push(e.type); return true; } };
  });
  afterEach(() => { for (const k of Object.keys(saved)) g[k] = saved[k]; });

  test("applyAppParams writes app keys onto this page's URL, never host/path/_ keys", () => {
    applyAppParams("tab=2&q=x&bot=evil&path=/etc&_preview=1");
    const q = new URLSearchParams((g.location as { search: string }).search);
    expect(q.get("bot")).toBe("b1");
    expect(q.get("tab")).toBe("2");
    expect(q.get("q")).toBe("x");
    expect(q.has("path")).toBe(false);
    expect(q.has("_preview")).toBe(false);
    // No event of its own: the shell's main.tsx wraps replaceState to dispatch `fused:urlchange` (a second one here
    // would run every listener twice).
    expect(events).toEqual([]);
  });
  test("round trip: appStateParams reads back exactly the app's params", () => {
    applyAppParams("tab=2&q=a b");
    expect(appStateParams()).toBe("tab=2&q=a+b");
    expect(appStateParams("?bot=b1&_x=1&path=/p&sort=asc")).toBe("sort=asc");
  });
  test("no change, no event", () => {
    applyAppParams("");
    expect(events).toEqual([]);
  });
  test("opening another app REPLACES the previous app's keys instead of merging them", () => {
    applyAppParams("tab=2&q=x");
    applyAppParams("sort=asc");
    const q = new URLSearchParams((g.location as { search: string }).search);
    expect(q.get("bot")).toBe("b1");
    expect(q.get("sort")).toBe("asc");
    expect(q.has("tab")).toBe(false);
    expect(q.has("q")).toBe(false);
  });
  test("a hold keeps the params until the LAST holder releases", () => {
    const a = holdAppParams(); applyAppParams("tab=2");
    const b = holdAppParams();
    expect(anyAppOpen()).toBe(true);
    a();
    expect((g.location as { search: string }).search).toBe("?bot=b1&tab=2");
    b();
    expect((g.location as { search: string }).search).toBe("?bot=b1");
    expect(anyAppOpen()).toBe(false);
    b();  // releasing twice is a no-op
  });
  test("clearAppParams drops every app key and keeps the host's", () => {
    applyAppParams("tab=2&q=x");
    clearAppParams();
    expect((g.location as { search: string }).search).toBe("?bot=b1");
  });
});

describe("builds", () => {
  test("slugOf", () => {
    expect(slugOf("Invoice tracker")).toBe("invoice-tracker");
    expect(slugOf("  --Héllo, World!! 2026 ")).toBe("h-llo-world-2026");
    expect(slugOf("")).toBe("app");
    expect(slugOf("!!!")).toBe("app");
    expect(slugOf("x".repeat(60))).toHaveLength(40);
  });

  test("BUILD_RE reads name and folder from the new, update and old prompt forms", () => {
    expect(BUILD_RE.exec(buildPrompt("Invoice tracker", ROOT + "/invoice-tracker", "x"))?.slice(1)).toEqual(["Invoice tracker", ROOT + "/invoice-tracker"]);
    expect(BUILD_RE.exec(updatePrompt("Notes", ROOT + "/notes", "x"))?.slice(1)).toEqual(["Notes", ROOT + "/notes"]);
    expect(BUILD_RE.exec(`Create a new fused-render app named "Old" in the folder ${ROOT}/old.`)?.slice(1)).toEqual(["Old", ROOT + "/old."]);
    expect(BUILD_RE.exec("Fix the login bug")).toBeNull();
  });

  const row = (p: Partial<TaskRow>): TaskRow => ({ key: "k", status: "done", ...p });
  test("isBuild: recorded, or marked by title or a message body", () => {
    _setBuildsState([{ entryId: "e1", name: "A", dir: "", createdAt: 0 }], []);
    expect(isBuild(row({ entry_id: "e1", title: "anything" }))).toBe(true);
    expect(isBuild(row({ entry_id: "e2", title: "Build \"X\" · new fused-render app in /d" }))).toBe(true);
    expect(isBuild(row({ entry_id: "e3", title: "Task · X", messages: [{ body: "Create a new fused-render app named \"Y\"" }] }))).toBe(true);
    expect(isBuild(row({ entry_id: "e4", title: "Unrelated" }))).toBe(false);
  });

  test("adoptBuilds: marked rows are recorded with the parsed name/dir; new nearby rows only while the panel is open", () => {
    const marked = row({ key: "s1", entry_id: "m1", title: `Build "Map" · new fused-render app in ${ROOT}/map`, started: 100, project: ROOT + "/map" });
    const other = row({ key: "s2", entry_id: "o1", title: "Unrelated", project: "/elsewhere" });
    _setBuildsState([], [marked, other]);
    expect(adoptBuilds(false)).toBe(true);
    expect(mine().map((t) => t.key)).toEqual(["s1"]);
    expect(getBuilds()).toEqual([{ entryId: "m1", name: "Map", dir: ROOT + "/map", createdAt: 100000 }]);
    // A row that appears while the panel is open is adopted when it sits under the root (or names no folder).
    const near = row({ key: "s3", entry_id: "n1", title: "Task", project: "" });
    const far = row({ key: "s4", entry_id: "f1", title: "Far", project: "/elsewhere/x" });
    setBuildsRoot(ROOT);
    _setBuildsState([], [marked, other]);
    adoptBuilds(true);  // first look: nothing is "new" yet
    _setRowsOnly([marked, other, near, far]);  // a feed tick: same builds, same "seen" set
    adoptBuilds(true);
    expect(mine().map((t) => t.key).sort()).toEqual(["s1", "s3"]);
  });

  test("chip numbers", () => {
    expect(chipFor([])).toEqual({ n: "", live: false, warn: false, title: "Tasks · Claude tasks that create fused apps" });
    expect(chipFor([row({ status: "in_progress" }), row({ status: "queued" })])).toEqual({ n: "2", live: true, warn: false, title: "2 tasks running" });
    expect(chipFor([row({ status: "in_progress" }), row({ status: "needs_attention" })])).toEqual({ n: "2", live: false, warn: true, title: "1 task needs your attention" });
  });

  test("filter stylesheet keeps only build rows", () => {
    const css = buildFilterCss(["s1", 'pending:"x']);
    expect(css).toContain('.tasks-node:not(:has(.tasks-row[data-peek-key="s1"])):not(:has(.tasks-row[data-peek-key="pending:x"])) { display: none !important; }');
    expect(buildFilterCss([])).toContain(".tasks-node { display: none !important; }");
    expect(buildFilterCss([])).toContain('.tasks-list-frame::before { content: "No builds yet. Start one with New build."');
  });

  test("buildPrompt is OpenBot's text", () => {
    expect(buildPrompt("Invoice tracker", "/A/app/invoice-tracker", "  Track invoices.\n")).toBe(`Build "Invoice tracker" · new fused-render app in /A/app/invoice-tracker

You are running inside /A/app/invoice-tracker, an empty folder made for this app. Build the app there.
Rules:
- Invoke the fused-render-authoring skill before writing any code and follow its contract.
- Exactly one entry page, /A/app/invoice-tracker/index.html, with <meta name="fused-app" /> and <meta name="fused-api-version" content="1" /> near the top of <head>.
- Plain HTML/CSS/JS, no build step, no network at runtime. Python beside the page via fused.runPython only when it adds value, with a pyproject.toml in that folder.
- Every .py beside the page exposes ONE top-level annotated main(**params), returns JSON-native values, takes no argv/stdin and finishes under 60 s, and gets a section in the app's SKILL.md (beside index.html): what it does, what it changes, args, return shape, one example call. Bots read that SKILL.md to call these files directly (their \`py\` action). Keep SKILL.md in step with every .py you add, change or remove. The authoring skill's "App SKILL.md" section has the exact format.
- Follow the shell theme (data-fused-theme="shell") and gate the _preview=1 mode.
- ALL UI state MUST live in the URL through fused.params (selected tab, filters, search text, sort, open item, map view, toggles, any value the user picks): read it on load, write it on every change, never keep view state only in JS variables or localStorage. The shell's Copy state button copies the page URL, so a copied link must reopen the app exactly as the user sees it.
- Add a short README.md describing the app. Do not touch anything outside /A/app/invoice-tracker.
- When done, reply with a two-line summary and the folder path.

What the app should do:
Track invoices.`);
  });
});

describe("build handle: done the _watch_build way", () => {
  const run = (statuses: string[]) => {
    let s = { seenRunning: false, stable: "" };
    return statuses.map((st) => { const o = observeStatus(s, st); s = o.state; return o.settled; });
  };
  test("never settles on a quiet row before the task was seen running", () => {
    expect(run(["done", "done", "done"])).toEqual([false, false, false]);
  });
  test("settles on the second done in a row after running", () => {
    expect(run(["queued", "in_progress", "done", "done"])).toEqual([false, false, false, true]);
  });
  test("a flicker back to running resets the count", () => {
    expect(run(["in_progress", "done", "in_progress", "done", "done"])).toEqual([false, false, false, false, true]);
  });
  test("archived counts as settled too", () => {
    expect(run(["needs_attention", "archived", "archived"])).toEqual([false, false, true]);
  });
});
