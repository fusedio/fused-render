import { describe, expect, it } from "bun:test";
import {
  buildHostUrl,
  encodeTabsParam,
  hostLocation,
  isSpaUrl,
  moveItem,
  neighborAfterClose,
  parseHostUrl,
  samePage,
  stepIndex,
  stripTabMarker,
  tabLabel,
  tabSrc,
} from "./tabs-lib";

describe("tabSrc", () => {
  it("maps the explorer view prefix onto the embed prefix", () => {
    expect(tabSrc("/explorer/view/a/b.html?x=1")).toBe("/explorer/embed/a/b.html?x=1");
  });
  it("leaves an embed url alone", () => {
    expect(tabSrc("/explorer/embed/a/b.html")).toBe("/explorer/embed/a/b.html");
  });
  it("sends / and /home to Home with the marker", () => {
    expect(tabSrc("/")).toBe("/?embed=1");
    expect(tabSrc("/home")).toBe("/home?embed=1");
  });
  it("adds the marker to every other route, keeping the query byte-identical", () => {
    expect(tabSrc("/tasks")).toBe("/tasks?embed=1");
    expect(tabSrc("/apps/x?_tab=files&q=a%20b")).toBe("/apps/x?_tab=files&q=a%20b&embed=1");
    expect(tabSrc("/bots/abc#h")).toBe("/bots/abc?embed=1#h");
  });
  it("is idempotent", () => {
    expect(tabSrc("/tasks?embed=1")).toBe("/tasks?embed=1");
    expect(tabSrc(tabSrc("/explorer/view/a"))).toBe("/explorer/embed/a");
  });
});

describe("stripTabMarker", () => {
  it("removes only embed=1", () => {
    expect(stripTabMarker("/tasks?embed=1")).toBe("/tasks");
    expect(stripTabMarker("/apps/x?a=1&embed=1&b=2")).toBe("/apps/x?a=1&b=2");
    expect(stripTabMarker("/apps/x?embedded=1")).toBe("/apps/x?embedded=1");
  });
  it("keeps the embed prefix", () => {
    expect(stripTabMarker("/explorer/embed/a.html")).toBe("/explorer/embed/a.html");
  });
  it("round-trips tabSrc on a non-explorer route", () => {
    expect(stripTabMarker(tabSrc("/apps/x?_tab=files"))).toBe("/apps/x?_tab=files");
  });
});

describe("hostLocation", () => {
  it("never leaves the host on an embed url", () => {
    expect(hostLocation("/explorer/embed/a/b.html?x=1")).toBe("/explorer/view/a/b.html?x=1");
    expect(hostLocation("/tasks?embed=1")).toBe("/tasks");
    expect(hostLocation("/apps/x")).toBe("/apps/x");
  });
});

describe("samePage", () => {
  it("compares pathnames in the frame's spelling", () => {
    expect(samePage("/explorer/view/a", "/explorer/embed/a?sort=name")).toBe(true);
    expect(samePage("/", "/home?embed=1")).toBe(true);
    expect(samePage("/tasks", "/apps/x")).toBe(false);
  });
});

describe("_tabs codec", () => {
  it("has no param for one tab", () => {
    expect(encodeTabsParam(["/home"], 0)).toBeNull();
    expect(buildHostUrl(["/home"], 0)).toBe("/home");
  });
  it("encodes the contract example", () => {
    expect(buildHostUrl(["/home", "/apps/map", "/tasks"], 1)).toBe(
      "/apps/map?_tabs=%2Fhome,.,%2Ftasks",
    );
  });
  it("appends after the active tab's own query", () => {
    expect(buildHostUrl(["/apps/map?z=1", "/explorer/view/a?x=1,2&y"], 0)).toBe(
      "/apps/map?z=1&_tabs=.,%2Fexplorer%2Fview%2Fa%3Fx%3D1%2C2%26y",
    );
  });
  it("parses the contract example", () => {
    expect(parseHostUrl("/apps/map?_tabs=%2Fhome,.,%2Ftasks")).toEqual({
      tabs: ["/home", "/apps/map", "/tasks"],
      active: 1,
    });
  });
  it("round-trips urls with commas, ampersands and spaces", () => {
    const urls = ["/explorer/view/a b?x=1,2&y=%2C", "/apps/map?z=1", "/tasks"];
    for (let i = 0; i < urls.length; i++) {
      expect(parseHostUrl(buildHostUrl(urls, i))).toEqual({ tabs: urls, active: i });
    }
  });
  it("treats a url without _tabs as one tab", () => {
    expect(parseHostUrl("/tasks?view=board")).toEqual({ tabs: ["/tasks?view=board"], active: 0 });
  });
  it("degrades on malformed input", () => {
    // No active slot: the location goes first.
    expect(parseHostUrl("/a?_tabs=%2Fb")).toEqual({ tabs: ["/a", "/b"], active: 0 });
    // Undecodable, empty, foreign and duplicate-active items are dropped.
    expect(parseHostUrl("/a?_tabs=%E0%A4,,.,https%3A%2F%2Fx,%2F%2Fevil,.,%2Fc")).toEqual({
      tabs: ["/a", "/c"],
      active: 0,
    });
  });
});

describe("samePage query identity", () => {
  it("counts the whole layout for panel/tab sentinels", () => {
    expect(samePage("/explorer/view/_panel?_layout=(/a,/b)", "/explorer/embed/_panel?_layout=(/a,/b)")).toBe(true);
    expect(samePage("/explorer/view/_tab?_layout=(/a)", "/explorer/view/_tab?_layout=(/b)")).toBe(false);
  });
  it("counts only the project scope for /tasks", () => {
    expect(samePage("/tasks?project=%2Fa&view=board", "/tasks?project=%2Fa")).toBe(true);
    expect(samePage("/tasks?project=%2Fa", "/tasks")).toBe(false);
    expect(samePage("/tasks?view=board", "/tasks?embed=1")).toBe(true);
  });
  it("keeps pathname-only identity elsewhere", () => {
    expect(samePage("/apps/x?_tab=files", "/apps/x")).toBe(true);
  });
  it("leaves /onboarding out of the shell routes", () => {
    expect(isSpaUrl("/onboarding")).toBe(false);
  });
});

describe("non-shell pages (/render)", () => {
  const app = "/render?path=%2FUsers%2Fme%2Fx%2Findex.html&n=1";
  it("passes through tabSrc and stripTabMarker verbatim", () => {
    expect(tabSrc(app)).toBe(app);
    expect(stripTabMarker("/render?path=a&embed=1")).toBe("/render?path=a&embed=1");
  });
  it("is not an SPA url; shell routes and the explorer are", () => {
    expect(isSpaUrl(app)).toBe(false);
    expect(isSpaUrl("/bots/abc")).toBe(true);
    expect(isSpaUrl("/explorer/embed/a")).toBe(true);
  });
  it("is identified by its whole url", () => {
    expect(samePage(app, app)).toBe(true);
    expect(samePage(app, "/render?path=%2Fother")).toBe(false);
  });
  it("keeps the host on Home and names the active tab with !", () => {
    const url = buildHostUrl(["/tasks", app], 1);
    expect(url).toBe("/home?_tabs=%2Ftasks,!" + encodeURIComponent(app));
    expect(parseHostUrl(url)).toEqual({ tabs: ["/tasks", app], active: 1 });
  });
  it("writes _tabs even for a single non-SPA tab", () => {
    const url = buildHostUrl([app], 0);
    expect(url).toBe("/home?_tabs=!" + encodeURIComponent(app));
    expect(parseHostUrl(url)).toEqual({ tabs: [app], active: 0 });
  });
  it("labels by the file it renders", () => {
    expect(tabLabel(app, "", "Fused")).toBe("index.html");
  });
});

describe("tabLabel", () => {
  it("prefers the document title minus the brand", () => {
    expect(tabLabel("/tasks", "Tasks – Fused", "Fused")).toBe("Tasks");
    expect(tabLabel("/tasks", "Fused", "Fused")).toBe("tasks");
  });
  it("falls back to Home and the decoded basename", () => {
    expect(tabLabel("/home?embed=1", "", "Fused")).toBe("Home");
    expect(tabLabel("/explorer/embed/a/my%20file.html", null, "Fused")).toBe("my file.html");
  });
});

describe("strip helpers", () => {
  it("steps with wrap-around", () => {
    expect(stepIndex(0, -1, 3)).toBe(2);
    expect(stepIndex(2, 1, 3)).toBe(0);
  });
  it("picks the neighbor that slides into the closed slot", () => {
    expect(neighborAfterClose(1, 3)).toBe(1);
    expect(neighborAfterClose(2, 3)).toBe(1);
    expect(neighborAfterClose(0, 1)).toBe(-1);
  });
  it("moves an item", () => {
    expect(moveItem(["a", "b", "c"], 0, 2)).toEqual(["b", "c", "a"]);
    expect(moveItem(["a", "b", "c"], 2, 0)).toEqual(["c", "a", "b"]);
  });
});
