import { describe, expect, test } from "bun:test";
import { chatUrl } from "@platform/lib/queue";
import {
  adoptedRun,
  buildHostUrl,
  chatHoldKey,
  classifyNav,
  encodeRight,
  encodeTabsParam,
  isPlainPath,
  isSpaUrl,
  liftRightTabs,
  neighborAfterClose,
  panelLayout,
  normalizeOpenUrl,
  pageUrlOf,
  parseHostUrl,
  parseRight,
  placeOfUrl,
  planOpen,
  rightOnlyView,
  rightSession,
  sameRight,
  samePage,
  splitArrival,
  tabKind,
  tabLabel,
  type RightView,
} from "./workspace-lib";

const FILE = "/explorer/view/Users/me/a.html";
const FOLDER = "/explorer/view/Users/me/dir";

describe("samePage", () => {
  test("pathname only: query and hash do not make another page", () => {
    expect(samePage(FILE + "?_mode=code", FILE)).toBe(true);
    expect(samePage(FILE + "#x", FILE + "?a=1")).toBe(true);
    expect(samePage(FILE, FOLDER)).toBe(false);
  });
  test("/ is Home", () => {
    expect(samePage("/", "/home")).toBe(true);
    expect(samePage("/?a=1", "/home")).toBe(true);
  });
  test("decoded pathnames compare equal", () => {
    expect(samePage("/explorer/view/Users/me/a%20b", "/explorer/view/Users/me/a b")).toBe(true);
  });
  test("where the query is the page, it counts", () => {
    expect(samePage("/render?path=/a.html", "/render?path=/b.html")).toBe(false);
    expect(samePage("/render?path=/a.html", "/render?path=/a.html")).toBe(true);
    expect(samePage("/explorer/view/_panel?_layout=(a)", "/explorer/view/_panel?_layout=(b)")).toBe(false);
  });
});

describe("planOpen", () => {
  const tabs = ["/home", FOLDER, "/tasks"];
  test("a page already open is activated, never opened twice", () => {
    expect(planOpen(tabs, 2, FOLDER)).toEqual({ kind: "activate", index: 1, navigate: false });
    expect(planOpen(tabs, 1, "/home", true)).toEqual({ kind: "activate", index: 0, navigate: false });
    expect(planOpen(tabs, 1, "/")).toEqual({ kind: "activate", index: 0, navigate: false });
  });
  test("a request with its own params navigates the tab it activates", () => {
    expect(planOpen(tabs, 0, FOLDER + "?sort=name")).toEqual({ kind: "activate", index: 1, navigate: true });
    expect(planOpen(tabs, 0, FOLDER + "#top")).toEqual({ kind: "activate", index: 1, navigate: true });
  });
  test("…but not when the tab is already exactly there", () => {
    const t = ["/home", FOLDER + "?sort=name"];
    expect(planOpen(t, 0, FOLDER + "?sort=name")).toEqual({ kind: "activate", index: 1, navigate: false });
  });
  test("Shift opens a new tab at the end", () => {
    expect(planOpen(tabs, 2, FILE, true)).toEqual({ kind: "append" });
  });
  test("from Home, a new tab; from any other tab, that tab goes there", () => {
    expect(planOpen(tabs, 0, FILE)).toEqual({ kind: "append" });
    expect(planOpen(tabs, 2, FILE)).toEqual({ kind: "navigateActive" });
  });
});

describe("normalizeOpenUrl", () => {
  const base = "http://localhost:1777/home";
  test("same origin reduces to path+query+hash", () => {
    expect(normalizeOpenUrl("http://localhost:1777/tasks?a=1#h", base)).toEqual({ kind: "same", url: "/tasks?a=1#h" });
    expect(normalizeOpenUrl("/apps/x", base)).toEqual({ kind: "same", url: "/apps/x" });
  });
  test("a foreign web link goes outside; anything else nowhere", () => {
    expect(normalizeOpenUrl("https://example.com/x", base)).toEqual({ kind: "foreign", href: "https://example.com/x" });
    expect(normalizeOpenUrl("javascript:alert(1)", base)).toBeNull();
  });
});

describe("neighborAfterClose", () => {
  test("the right neighbour, else the left, Home at worst", () => {
    // [Home, a, b, c] close a (1) → b slides into 1.
    expect(neighborAfterClose(1, 4)).toBe(1);
    // close c (3), the last → b (2).
    expect(neighborAfterClose(3, 4)).toBe(2);
    // [Home, a] close a → Home.
    expect(neighborAfterClose(1, 2)).toBe(0);
  });
});

describe("host URL codec", () => {
  test("Home alone carries no _tabs", () => {
    expect(encodeTabsParam(["/home"], 0)).toBeNull();
    expect(buildHostUrl(["/home"], 0)).toBe("/home");
  });
  test("the active tab's own url, then every tab with the active slot a dot", () => {
    const url = buildHostUrl(["/home", FOLDER + "?sort=name", "/tasks"], 1);
    expect(url).toBe(FOLDER + "?sort=name&_tabs=%2Fhome,.,%2Ftasks");
    expect(parseHostUrl(url)).toEqual({ tabs: ["/home", FOLDER + "?sort=name", "/tasks"], active: 1, right: null });
  });
  test("a layout query with literal & survives byte for byte", () => {
    const panel = "/explorer/view/_panel?_layout=(a&b)";
    const url = buildHostUrl(["/home", panel], 1);
    expect(url.startsWith(panel + "&_tabs=")).toBe(true);
    expect(parseHostUrl(url).tabs[1]).toBe(panel);
  });
  test("a non-SPA or embed active tab keeps the host on Home and is named with !", () => {
    const render = "/render?path=/a/b.html";
    const url = buildHostUrl(["/home", render], 1);
    expect(url).toBe("/home?_tabs=%2Fhome,!" + encodeURIComponent(render));
    expect(parseHostUrl(url)).toEqual({ tabs: ["/home", render], active: 1, right: null });
    expect(pageUrlOf(url)).toBe(render);
    const embed = "/explorer/embed/a";
    expect(buildHostUrl(["/home", embed], 1).startsWith("/home?")).toBe(true);
  });
  test("_right is carried through, opaque, after _tabs", () => {
    const url = buildHostUrl(["/home", "/tasks"], 1, "chat:abc");
    expect(url).toBe("/tasks?_tabs=%2Fhome,.&_right=chat:abc");
    expect(parseHostUrl(url).right).toBe("chat:abc");
    expect(buildHostUrl(["/home"], 0, "x")).toBe("/home?_right=x");
    expect(pageUrlOf(url)).toBe("/tasks");
  });
  test("a list without Home gets it at 0", () => {
    expect(parseHostUrl(FILE)).toEqual({ tabs: ["/home", FILE], active: 1, right: null });
    expect(parseHostUrl("/")).toEqual({ tabs: ["/home"], active: 0, right: null });
    expect(parseHostUrl(FILE + "?_tabs=" + encodeURIComponent("/tasks") + ",.,%2Fhome")).toEqual({
      tabs: ["/home", "/tasks", FILE],
      active: 2,
      right: null,
    });
  });
  test("junk degrades: foreign items dropped, a missing active slot is the location", () => {
    const url = "/tasks?_tabs=" + encodeURIComponent("https://evil.example/") + ",%E0%A4%A,%2Fhome";
    expect(parseHostUrl(url)).toEqual({ tabs: ["/home", "/tasks"], active: 1, right: null });
  });
});

describe("isSpaUrl", () => {
  test("shell routes and the explorer, not /render or the wizard", () => {
    expect(isSpaUrl("/home")).toBe(true);
    expect(isSpaUrl("/apps/Users/me/app")).toBe(true);
    expect(isSpaUrl(FILE)).toBe(true);
    expect(isSpaUrl("/render?path=/x")).toBe(false);
    expect(isSpaUrl("/onboarding")).toBe(false);
  });
});

describe("tabLabel", () => {
  test("the frame's title minus the brand, else the leaf", () => {
    expect(tabLabel(FILE, "a.html – Fused", "Fused")).toBe("a.html");
    expect(tabLabel(FILE, "", "Fused")).toBe("a.html");
    expect(tabLabel("/home?x=1", "Something", "Fused")).toBe("Home");
    expect(tabLabel("/render?path=/a/app.html", null, "Fused")).toBe("app.html");
  });
});

describe("a tab that has not loaded yet", () => {
  test("a shell page says its own name, not a path segment", () => {
    expect(tabLabel("/marketplace", "", "Fused")).toBe("Community");
    expect(tabLabel("/marketplace/installed", null, "Fused")).toBe("Community");
    expect(tabLabel("/ai-models/playground?model=x", "", "Fused")).toBe("AI Models");
    expect(tabLabel("/preferences?tab=integrations", "", "Fused")).toBe("Preferences");
    // An app page and a canvas are named by their own last segment.
    expect(tabLabel("/apps/Users/me/My%20App", "", "Fused")).toBe("My App");
    expect(tabLabel("/canvases/prod/sales", "", "Fused")).toBe("sales");
    // A title, once read, still wins.
    expect(tabLabel("/marketplace/app/x", "Poster maker – Fused", "Fused")).toBe("Poster maker");
  });
  test("its icon is its kind's", () => {
    expect(tabKind("/")).toBe("home");
    expect(tabKind("/home?q=a")).toBe("home");
    expect(tabKind("/ai-models/local")).toBe("models");
    expect(tabKind("/marketplace")).toBe("community");
    expect(tabKind("/preferences")).toBe("settings");
    expect(tabKind("/apps")).toBe("app");
    expect(tabKind("/apps/Users/me/app")).toBe("app");
    expect(tabKind("/canvases/prod/sales")).toBe("canvas");
    expect(tabKind("/explorer/view/Users/me/Downloads?_side=off")).toBe("folder");
    expect(tabKind(FILE)).toBe("file");
    expect(tabKind("/explorer/view/Users/me/.config")).toBe("folder");
    expect(tabKind("/explorer/view/_tab?_layout=(/a,/b)")).toBe("page");
    expect(tabKind("/render?path=/a/app.html")).toBe("page");
  });
});

describe("parseRight / encodeRight", () => {
  const views: RightView[] = [
    { kind: "tasks", peek: null },
    { kind: "tasks", peek: "pending:abc" },
    { kind: "bots", url: "/bots" },
    { kind: "bots", url: "/bots/b1?tab=log&x=a:b" },
    { kind: "chat", file: "/Users/me/a b:c.html", query: "" },
    { kind: "chat", file: "/Users/me/dir", query: "session_id=s1&msg=m%3A1" },
    { kind: "git", path: "/Users/me/dir" },
    { kind: "mode", mode: "code", file: "/Users/me/a.html" },
  ];
  test("round-trips every view, `:` always escaped in arguments", () => {
    for (const v of views) {
      const raw = encodeRight(v);
      expect(parseRight(raw)).toEqual(v);
      // Query-safe as a param value: nothing that would end it.
      expect(raw).not.toMatch(/[&#?= ]/);
    }
  });
  test("short spellings", () => {
    expect(encodeRight({ kind: "tasks", peek: null })).toBe("tasks");
    expect(encodeRight({ kind: "bots", url: "/bots" })).toBe("bots");
    expect(encodeRight({ kind: "chat", file: "/a", query: "" })).toBe("chat:%2Fa:");
  });
  test("junk is closed", () => {
    for (const raw of [null, "", "nope", "chat", "chat:%2Fa", "chat::x", "git:", "mode:code", "bots:%2Fhome", "tasks:%E0", "tasks:a:b"]) {
      expect(parseRight(raw)).toBeNull();
    }
  });
  test("a leading ? on a chat query is dropped", () => {
    expect(parseRight("chat:%2Fa:%3Fsession_id%3Ds")).toEqual({ kind: "chat", file: "/a", query: "session_id=s" });
  });
  test("sameRight and rightSession", () => {
    expect(sameRight({ kind: "tasks", peek: null }, { kind: "tasks", peek: null })).toBe(true);
    expect(sameRight({ kind: "tasks", peek: null }, null)).toBe(false);
    expect(sameRight(null, null)).toBe(true);
    // A chat is its file and conversation; its own params are not another chat.
    expect(sameRight({ kind: "chat", file: "/a", query: "session_id=s&split=1" }, { kind: "chat", file: "/a", query: "session_id=s" })).toBe(true);
    expect(sameRight({ kind: "chat", file: "/a", query: "session_id=s" }, { kind: "chat", file: "/a", query: "" })).toBe(false);
    expect(sameRight({ kind: "chat", file: "/a", query: "" }, { kind: "chat", file: "/b", query: "" })).toBe(false);
    // A run the opened view names is another chat to show (a canvas fix).
    expect(sameRight({ kind: "chat", file: "/a", query: "" }, { kind: "chat", file: "/a", query: "run=r1" })).toBe(false);
    expect(sameRight({ kind: "chat", file: "/a", query: "run=r1" }, { kind: "chat", file: "/a", query: "run=r1&split=1" })).toBe(true);
    expect(rightSession({ kind: "chat", file: "/a", query: "session_id=s1" })).toBe("s1");
    expect(rightSession({ kind: "chat", file: "/a", query: "" })).toBeNull();
  });
});

describe("classifyNav", () => {
  test("a chat door goes right, with only the door params", () => {
    expect(classifyNav("/explorer/view/Users/me/dir?_side=claude&sort=name&session_id=s1&queued=")).toEqual({
      side: "right",
      view: { kind: "chat", file: "/Users/me/dir", query: "session_id=s1" },
    });
    expect(classifyNav("/explorer/view/Users/me/a%20b.html?_side=claude&backend=codex&session_id=")).toEqual({
      side: "right",
      view: { kind: "chat", file: "/Users/me/a b.html", query: "backend=codex" },
    });
    expect(classifyNav("/explorer/view/Users/me/a.html?_mode=claude&session_id=s2")).toEqual({
      side: "right",
      view: { kind: "chat", file: "/Users/me/a.html", query: "session_id=s2" },
    });
  });
  test("git companion goes right", () => {
    expect(classifyNav("/explorer/view/Users/me/dir?_side=git")).toEqual({ side: "right", view: { kind: "git", path: "/Users/me/dir" } });
    expect(classifyNav("/explorer/view/Users/me/a.py?_mode=git")).toEqual({ side: "right", view: { kind: "git", path: "/Users/me/a.py" } });
  });
  test("tasks and bots go right", () => {
    expect(classifyNav("/tasks")).toEqual({ side: "right", view: { kind: "tasks", peek: null } });
    expect(classifyNav("/tasks?peek=k1")).toEqual({ side: "right", view: { kind: "tasks", peek: "k1" } });
    expect(classifyNav("/bots")).toEqual({ side: "right", view: { kind: "bots", url: "/bots" } });
    expect(classifyNav("/bots/b1?tab=x")).toEqual({ side: "right", view: { kind: "bots", url: "/bots/b1?tab=x" } });
  });
  test("a canvas task door asks the host by session", () => {
    expect(classifyNav("/canvases/prod/my_canvas?session_id=s9")).toEqual({ side: "session", sessionId: "s9" });
    expect(classifyNav("/canvases/prod/my_canvas")).toEqual({ side: "left" });
    expect(classifyNav("/canvases")).toEqual({ side: "left" });
  });
  test("everything else is left", () => {
    for (const u of [FILE, FOLDER, FOLDER + "?_side=off", FILE + "?_mode=code", "/home", "/apps/x", "/botsy", "/render?path=/a"]) {
      expect(classifyNav(u)).toEqual({ side: "left" });
    }
  });
});

describe("splitArrival", () => {
  test("a chat pane on a folder: right chat, the tab keeps the folder", () => {
    expect(splitArrival(FOLDER + "?sort=name&_side=claude&session_id=s1")).toEqual({
      keep: FOLDER + "?sort=name",
      right: { kind: "chat", file: "/Users/me/dir", query: "session_id=s1" },
    });
  });
  test("a file on _mode=git: right git, the tab on its default mode", () => {
    expect(splitArrival(FILE + "?_mode=git")).toEqual({ keep: FILE, right: { kind: "git", path: "/Users/me/a.html" } });
  });
  test("no pane: kept as is", () => {
    expect(splitArrival(FILE + "?_mode=code")).toEqual({ keep: FILE + "?_mode=code", right: null });
    expect(splitArrival(FOLDER + "?_side=off")).toEqual({ keep: FOLDER + "?_side=off", right: null });
    expect(splitArrival("/tasks?peek=k")).toEqual({ keep: "/tasks?peek=k", right: null });
  });
});

describe("placeOfUrl (an unloaded tab's rail place, WS-27)", () => {
  test("an app page is its folder", () => {
    expect(placeOfUrl("/apps/Users/me/my%20app?_tab=tasks")).toEqual({ kind: "folder", dir: "/Users/me/my app", word: "App" });
  });
  test("a canvas page is its canvas, its clone not known", () => {
    expect(placeOfUrl("/canvases/prod/demo_1?node=x")).toEqual({ kind: "canvas", env: "prod", name: "demo_1", dir: null });
    expect(placeOfUrl("/canvases/nope/demo")).toBeNull();
  });
  test("an explorer folder, or a file in its parent folder", () => {
    expect(placeOfUrl(FOLDER + "?sort=name")).toEqual({ kind: "folder", dir: "/Users/me/dir" });
    expect(placeOfUrl(FILE)).toEqual({ kind: "file", file: "/Users/me/a.html", dir: "/Users/me" });
    expect(placeOfUrl("/explorer/view/Users/me/b%20c.py")).toEqual({ kind: "file", file: "/Users/me/b c.py", dir: "/Users/me" });
  });
  test("anything else has no place", () => {
    for (const u of ["/home", "/tasks", "/ai-models", "/marketplace", "/preferences", "/explorer", "/explorer/view/_panel", "/explorer/view/s3:/bucket/k", "/render?path=/a"]) {
      expect(placeOfUrl(u)).toBeNull();
    }
  });
});

describe("rightOnlyView (WS-34)", () => {
  test("Tasks and Bots are panel views, never tabs", () => {
    expect(rightOnlyView("/tasks")).toEqual({ kind: "tasks", peek: null });
    expect(rightOnlyView("/tasks?peek=abc")).toEqual({ kind: "tasks", peek: "abc" });
    expect(rightOnlyView("/bots")).toEqual({ kind: "bots", url: "/bots" });
    expect(rightOnlyView("/bots/x?tab=1")).toEqual({ kind: "bots", url: "/bots/x?tab=1" });
  });
  test("a chat door, a canvas with a session and any other page keep their tab", () => {
    expect(rightOnlyView(FOLDER + "?_side=claude&session_id=s1")).toBeNull();
    expect(rightOnlyView("/canvases/prod/c1?session_id=s1")).toBeNull();
    expect(rightOnlyView("/home")).toBeNull();
    expect(rightOnlyView("/botsy")).toBeNull();
  });
});

describe("liftRightTabs (WS-34)", () => {
  test("a host booting on /tasks?peek= is Home plus the Tasks view", () => {
    const out = liftRightTabs(parseHostUrl("/tasks?peek=k1"));
    expect(out.tabs).toEqual(["/home"]);
    expect(out.active).toBe(0);
    expect(parseRight(out.right)).toEqual({ kind: "tasks", peek: "k1" });
  });
  test("a deep link into Bots", () => {
    const out = liftRightTabs(parseHostUrl("/bots/x"));
    expect(out.tabs).toEqual(["/home"]);
    expect(parseRight(out.right)).toEqual({ kind: "bots", url: "/bots/x" });
  });
  test("a _tabs item naming one is dropped; the active tab keeps its place", () => {
    const host = buildHostUrl(["/home", "/tasks", FOLDER], 2);
    const out = liftRightTabs(parseHostUrl(host));
    expect(out.tabs).toEqual(["/home", FOLDER]);
    expect(out.active).toBe(1);
    expect(out.kept).toEqual([0, 2]);
    expect(parseRight(out.right)).toEqual({ kind: "tasks", peek: null });
  });
  test("an explicit _right wins; the active lifted one beats an earlier one", () => {
    const named = liftRightTabs(parseHostUrl(buildHostUrl(["/home", "/tasks"], 1, "git:%2Fa")));
    expect(named.right).toBe("git:%2Fa");
    expect(named.active).toBe(0);
    const both = liftRightTabs(parseHostUrl(buildHostUrl(["/home", "/tasks", "/bots"], 2)));
    expect(both.tabs).toEqual(["/home"]);
    expect(parseRight(both.right)).toEqual({ kind: "bots", url: "/bots" });
  });
  test("a strip with none is untouched", () => {
    const parsed = parseHostUrl(buildHostUrl(["/home", FILE], 1, "tasks"));
    expect(liftRightTabs(parsed)).toEqual({ ...parsed, kept: [0, 1] });
  });
});

test("a canvas fix's door opens the clone's chat on its run (WS-35)", () => {
  expect(classifyNav(chatUrl("/Users/me/clone", "") + "&run=r1")).toEqual({
    side: "right",
    view: { kind: "chat", file: "/Users/me/clone", query: "run=r1" },
  });
});

test("a mode view carries the tab's revision (absent = live)", () => {
  const live: RightView = { kind: "mode", mode: "code", file: "/a/b.py" };
  const snap: RightView = { kind: "mode", mode: "code", file: "/a/b.py", rev: "abc123" };
  expect(encodeRight(live)).toBe("mode:code:%2Fa%2Fb.py");
  expect(encodeRight(snap)).toBe("mode:code:%2Fa%2Fb.py:abc123");
  expect(parseRight(encodeRight(snap))).toEqual(snap);
  expect(parseRight(encodeRight(live))).toEqual(live);
  expect(sameRight(live, snap)).toBe(false);
  expect(parseRight("mode:code:%2Fa:r:x")).toBeNull();
});

describe("panelLayout (WS-17)", () => {
  test("side by side while the tabs keep 320 and the panel 380", () => {
    expect(panelLayout(1200, 48, 320, 380)).toEqual({ overlay: false, width: 380 });
    expect(panelLayout(748, 48, 320, 380).overlay).toBe(false);
  });
  test("a narrower row makes it a drawer at its floor, or the room there is", () => {
    expect(panelLayout(747, 48, 320, 380)).toEqual({ overlay: true, width: 380 });
    expect(panelLayout(300, 48, 320, 380)).toEqual({ overlay: true, width: 252 });
  });
});

describe("chatHoldKey (WS-33)", () => {
  const rows = [
    { key: "k-run", session_id: "s1" },
    { key: "pending:e9", session_id: null },
  ];
  test("a session's row key, else the session id", () => {
    expect(chatHoldKey("s1", null, rows)).toBe("k-run");
    expect(chatHoldKey("s2", null, rows)).toBe("s2");
  });
  test("a chat that has not run is its pending row", () => {
    expect(chatHoldKey(null, "e9", rows)).toBe("pending:e9");
  });
  test("a Claude home holds nothing", () => {
    expect(chatHoldKey(null, null, rows)).toBeNull();
  });
});

describe("what a restored tab may be", () => {
  test("a path this origin serves, as a browser would resolve it", () => {
    expect(isPlainPath("/explorer/view/Users/me/a b.html?x=1")).toBe(true);
    expect(isPlainPath("//evil.example/x")).toBe(false);
    // The URL parser reads a backslash as a slash and drops tabs / newlines.
    expect(isPlainPath("/\\evil.example/x")).toBe(false);
    expect(isPlainPath("/\t/evil.example/x")).toBe(false);
    expect(isPlainPath("/\n/evil.example/x")).toBe(false);
    expect(isPlainPath("https://evil.example/")).toBe(false);
  });
  test("such an item in _tabs is dropped, not framed", () => {
    const bad = encodeURIComponent("/\\evil.example/page");
    expect(parseHostUrl("/home?_tabs=.," + bad).tabs).toEqual(["/home"]);
  });
  test("an embed spelling, however it is written, is never the host's own address", () => {
    // `embed=%31` decodes to embed=1 at boot (router IS_QUERY_EMBED).
    expect(buildHostUrl(["/home", "/tasks?embed=%31"], 1)).toMatch(/^\/home\?_tabs=/);
    expect(buildHostUrl(["/home", "/tasks?embed=1"], 1)).toMatch(/^\/home\?_tabs=/);
    expect(buildHostUrl(["/home", "/tasks?embedded=1"], 1)).toMatch(/^\/tasks\?embedded=1&_tabs=/);
  });
});

describe("a run that became its session (the canvas fix chat)", () => {
  const chat = (query: string): RightView => ({ kind: "chat", file: "/clone", query });
  test("the chat's own write from run to session is the pair", () => {
    expect(adoptedRun("run=r1", "session_id=s1")).toEqual(["r1", "s1"]);
    expect(adoptedRun("run=r1&split=1", "session_id=s1&split=1")).toEqual(["r1", "s1"]);
    expect(adoptedRun("run=r1", "run=r1")).toBeNull();
    expect(adoptedRun("session_id=s0&run=r1", "session_id=s1")).toBeNull();
    expect(adoptedRun("", "session_id=s1")).toBeNull();
  });
  test("a door on that run is the chat on screen, not a new view", () => {
    const adopted = (run: string) => (run === "r1" ? "s1" : undefined);
    expect(sameRight(chat("session_id=s1"), chat("run=r1"), adopted)).toBe(true);
    expect(sameRight(chat("run=r1"), chat("run=r1"), adopted)).toBe(true);
    // Another session on the same file, or a run nobody adopted: a new view.
    expect(sameRight(chat("session_id=s2"), chat("run=r1"), adopted)).toBe(false);
    expect(sameRight(chat("session_id=s1"), chat("run=r9"), adopted)).toBe(false);
    expect(sameRight(chat("session_id=s1"), chat("run=r1"))).toBe(false);
    expect(sameRight(chat(""), chat("run=r1"), adopted)).toBe(false);
  });
});
