// Which chats are on screen elsewhere (peek-session): a pane chat says what it
// shows while mounted and takes it back on unmount — the favourite's card
// reads only that, never the URL.
import { describe, expect, it } from "bun:test";
import { announcePaneSession, paneSessions } from "./peek-session";

describe("announcePaneSession", () => {
  it("lists a session while a mount holds it, deduplicated, and drops it with the last mount", () => {
    expect(paneSessions()).toEqual([]);
    const a = announcePaneSession("s1");
    const b = announcePaneSession("s1");
    const c = announcePaneSession("s0");
    expect(paneSessions()).toEqual(["s0", "s1"]);
    const snap = paneSessions();
    a();
    // One mount still on s1: nothing changed, the same snapshot.
    expect(paneSessions()).toBe(snap);
    b();
    c();
    expect(paneSessions()).toEqual([]);
  });
});
