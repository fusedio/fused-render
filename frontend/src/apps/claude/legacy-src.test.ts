// THE LEGACY PARITY GUARD, ADDRESS HALF. Every builder is pinned against the
// literal string its old inline expression produced, path encoding and parameter
// ORDER included (00 §1a/§1b).
//
// The ELEMENT half — that the flag off actually renders a `<ChatFrame>` with
// that src, its class and its `legacyFrameRef`, that a host-given `legacy` node
// wins outright, and that neither branch renders before the flag has answered —
// is `ChatMount.render.test.tsx`. A string test cannot see a lost className.
//
// These literals are the specification. If one of them has to change, the flag
// off is no longer parity and the change belongs in a PR that says so.
import { describe, expect, it } from "bun:test";

import {
  canvasChatSrc,
  cardFrameSrc,
  contentModeSrc,
  listingPaneSrc,
  peekFrameSrc,
  sideFrameSrc,
} from "./legacy-src";

const TPL = "/w/proj/.fused/claude/template.html";
const DIR = "/w/proj";
const FILE = "/w/proj/app.py";
const SESSION = "0f1e2d3c-4b5a";
/** A path with a space and a `#`, so the encoding is actually exercised. */
const ODD = "/w/my proj/#1";

describe("site 1 — the tasks cards wall", () => {
  it("is path + _file + chat_only + compact + session_id, in that order", () => {
    expect(cardFrameSrc(TPL, DIR, SESSION)).toBe(
      "/render?path=%2Fw%2Fproj%2F.fused%2Fclaude%2Ftemplate.html" +
        "&_file=%2Fw%2Fproj" +
        "&chat_only=1&compact=1" +
        "&session_id=0f1e2d3c-4b5a",
    );
  });
  it("encodes every component", () => {
    expect(cardFrameSrc(ODD, ODD, "a b")).toBe(
      "/render?path=%2Fw%2Fmy%20proj%2F%231" +
        "&_file=%2Fw%2Fmy%20proj%2F%231" +
        "&chat_only=1&compact=1" +
        "&session_id=a%20b",
    );
  });
});

describe("site 2 — the tasks card popup", () => {
  it("swaps compact for peek and keeps everything else", () => {
    expect(peekFrameSrc(TPL, DIR, SESSION)).toBe(
      "/render?path=%2Fw%2Fproj%2F.fused%2Fclaude%2Ftemplate.html" +
        "&_file=%2Fw%2Fproj" +
        "&chat_only=1&peek=1" +
        "&session_id=0f1e2d3c-4b5a",
    );
  });
  it("differs from the wall's src in exactly the one param", () => {
    expect(peekFrameSrc(TPL, DIR, SESSION)).toBe(
      cardFrameSrc(TPL, DIR, SESSION).replace("compact=1", "peek=1"),
    );
  });
});

describe("site 3 — the explorer file sidebar", () => {
  it("puts _remote before chat_only and the thumb flags last", () => {
    expect(sideFrameSrc(TPL, FILE, "&_remote=1", "&_preview=1&_nofocus=1")).toBe(
      "/render?path=%2Fw%2Fproj%2F.fused%2Fclaude%2Ftemplate.html" +
        "&_file=%2Fw%2Fproj%2Fapp.py" +
        "&_remote=1&chat_only=1&_preview=1&_nofocus=1",
    );
  });
  it("omits both fragments when the host has neither", () => {
    expect(sideFrameSrc(TPL, FILE, "", "")).toBe(
      "/render?path=%2Fw%2Fproj%2F.fused%2Fclaude%2Ftemplate.html" +
        "&_file=%2Fw%2Fproj%2Fapp.py" +
        "&chat_only=1",
    );
  });
});

describe("site 4 — the explorer folder listing pane", () => {
  it("is path + _file + chat_only + _noopen, with no _preview", () => {
    expect(listingPaneSrc(TPL, DIR, "&chat_only=1")).toBe(
      "/render?path=%2Fw%2Fproj%2F.fused%2Fclaude%2Ftemplate.html" +
        "&_file=%2Fw%2Fproj&chat_only=1&_noopen=1",
    );
    expect(listingPaneSrc(TPL, DIR, "&chat_only=1")).not.toContain("_preview");
  });
  it("drops chat_only for a companion that keeps its own pane", () => {
    expect(listingPaneSrc(TPL, DIR, "")).toBe(
      "/render?path=%2Fw%2Fproj%2F.fused%2Fclaude%2Ftemplate.html" +
        "&_file=%2Fw%2Fproj&_noopen=1",
    );
  });
});

describe("site 5 — the canvases workspace right pane", () => {
  it("is path + _file + chat_only, and carries no run or session", () => {
    expect(canvasChatSrc(TPL, DIR)).toBe(
      "/render?path=%2Fw%2Fproj%2F.fused%2Fclaude%2Ftemplate.html" +
        "&_file=%2Fw%2Fproj&chat_only=1",
    );
    expect(canvasChatSrc(TPL, DIR)).not.toContain("run=");
    expect(canvasChatSrc(TPL, DIR)).not.toContain("session_id=");
  });
});

describe("site 6 — the explorer content pane (_mode=claude)", () => {
  it("carries NO chat_only: the template renders its own split", () => {
    expect(contentModeSrc(TPL, FILE, "", "")).toBe(
      "/render?path=%2Fw%2Fproj%2F.fused%2Fclaude%2Ftemplate.html" +
        "&_file=%2Fw%2Fproj%2Fapp.py",
    );
    expect(contentModeSrc(TPL, FILE, "", "")).not.toContain("chat_only");
  });
  it("appends _remote then the thumb flags", () => {
    expect(contentModeSrc(TPL, FILE, "&_remote=1", "&_preview=1&_nofocus=1")).toBe(
      "/render?path=%2Fw%2Fproj%2F.fused%2Fclaude%2Ftemplate.html" +
        "&_file=%2Fw%2Fproj%2Fapp.py&_remote=1&_preview=1&_nofocus=1",
    );
  });
});
