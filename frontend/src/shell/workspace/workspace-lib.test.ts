import { describe, expect, test } from "bun:test";
import {
  buildHostUrl,
  encodeTabsParam,
  isSpaUrl,
  neighborAfterClose,
  normalizeOpenUrl,
  pageUrlOf,
  parseHostUrl,
  planOpen,
  samePage,
  tabKind,
  tabLabel,
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
