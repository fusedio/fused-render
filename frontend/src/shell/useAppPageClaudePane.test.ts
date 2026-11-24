// The app page's Claude pane rides the URL as `side=off` (useAppPageClaudePane):
// the two pure halves, so the word and its absence-means-open rule are pinned
// without a DOM.
import { describe, expect, it } from "bun:test";
import { chatDoorOnApp, claudePaneOpenFromSearch, opensOnThisPage, withClaudePaneParam } from "./useAppPageClaudePane";

describe("the app page's Claude pane on the URL", () => {
  it("is open unless the URL says side=off — the canvas page's own word", () => {
    expect(claudePaneOpenFromSearch("")).toBe(true);
    expect(claudePaneOpenFromSearch("?_tab=tasks")).toBe(true);
    expect(claudePaneOpenFromSearch("?side=off")).toBe(false);
    expect(claudePaneOpenFromSearch("?_tab=files&side=off&session_id=abc")).toBe(false);
    expect(claudePaneOpenFromSearch("?side=on")).toBe(true);
  });

  it("a silent URL defers to the shared hidden flag; side=off wins over an open flag", () => {
    expect(claudePaneOpenFromSearch("", true)).toBe(false);
    expect(claudePaneOpenFromSearch("?_tab=tasks", true)).toBe(false);
    expect(claudePaneOpenFromSearch("?side=off", false)).toBe(false);
    expect(claudePaneOpenFromSearch("?_tab=tasks", false)).toBe(true);
  });

  it("writes shut as side=off and open as no side param, leaving the rest of the query alone", () => {
    expect(withClaudePaneParam("/apps/x/y?_tab=tasks", false)).toBe("/apps/x/y?_tab=tasks&side=off");
    expect(withClaudePaneParam("/apps/x/y?_tab=tasks&side=off", true)).toBe("/apps/x/y?_tab=tasks");
    expect(withClaudePaneParam("/apps/x/y?side=off", true)).toBe("/apps/x/y");
    expect(withClaudePaneParam("/apps/x/y?side=off&session_id=s#h", false)).toBe("/apps/x/y?side=off&session_id=s#h");
  });
});

describe("chatDoorOnApp: which ring doors open in this page's own pane", () => {
  const DIR = "/Users/me/Fused/app/demo";
  it("a chat on the app folder, its entry page or a file under it, with a session", () => {
    expect(chatDoorOnApp("/explorer/view/Users/me/Fused/app/demo?_side=claude&session_id=s1", DIR))
      .toEqual({ params: { session_id: "s1" } });
    expect(chatDoorOnApp("/explorer/view/Users/me/Fused/app/demo/index.html?_side=claude&session_id=s1&backend=b", DIR))
      .toEqual({ params: { session_id: "s1", backend: "b" } });
    expect(chatDoorOnApp("/explorer/embed/Users/me/Fused/app/demo/src/x.py?session_id=s2", DIR))
      .toEqual({ params: { session_id: "s2" } });
    // A queued entry with no session yet is a door too.
    expect(chatDoorOnApp("/explorer/view/Users/me/Fused/app/demo?_side=claude&queued=q9", DIR))
      .toEqual({ params: { queued: "q9" } });
  });

  it("encoded segments and a trailing slash on the folder", () => {
    const dir = "/Users/me/Fused/app/a b 100%";
    expect(chatDoorOnApp("/explorer/view/Users/me/Fused/app/a%20b%20100%25/index.html?session_id=s", dir + "/"))
      .toEqual({ params: { session_id: "s" } });
  });

  it("a Windows drive path keeps no leading slash", () => {
    expect(chatDoorOnApp("/explorer/view/C:/Users/me/app?session_id=s", "C:/Users/me/app"))
      .toEqual({ params: { session_id: "s" } });
  });

  it("not ours: a sibling folder, a draft's Tasks page, no session, a bad escape", () => {
    expect(chatDoorOnApp("/explorer/view/Users/me/Fused/app/demo2?session_id=s", DIR)).toBeNull();
    expect(chatDoorOnApp("/tasks?draft=abc", DIR)).toBeNull();
    expect(chatDoorOnApp("/explorer/view/Users/me/Fused/app/demo?_side=claude", DIR)).toBeNull();
    expect(chatDoorOnApp("/explorer/view/Users/me/%E0%A4%A?session_id=s", DIR)).toBeNull();
    expect(chatDoorOnApp("/canvases/unstable/x?session_id=s", DIR)).toBeNull();
  });
});

describe("opensOnThisPage: the favourite card's Open wears ↗ only for a door out", () => {
  it("the same page: a canvas's chat on that canvas, a file's chat on that file", () => {
    expect(opensOnThisPage("/canvases/unstable/x?session_id=s", "/canvases/unstable/x")).toBe(true);
    expect(opensOnThisPage("/explorer/view/Users/me/a.py?_side=claude&session_id=s", "/explorer/view/Users/me/a.py")).toBe(true);
  });

  it("an app page: one of its own chats opens in its pane", () => {
    expect(opensOnThisPage("/explorer/view/Users/me/Fused/app/demo?_side=claude&session_id=s", "/apps/Users/me/Fused/app/demo"))
      .toBe(true);
  });

  it("elsewhere: another canvas, another file, another app, a page with no pane", () => {
    expect(opensOnThisPage("/canvases/unstable/x?session_id=s", "/canvases/unstable/y")).toBe(false);
    expect(opensOnThisPage("/explorer/view/Users/me/a.py?session_id=s", "/explorer/view/Users/me/b.py")).toBe(false);
    expect(opensOnThisPage("/explorer/view/Users/me/Fused/app/demo2?session_id=s", "/apps/Users/me/Fused/app/demo")).toBe(false);
    expect(opensOnThisPage("/canvases/unstable/x?session_id=s", "/tasks")).toBe(false);
  });
});
