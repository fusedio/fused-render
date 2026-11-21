import { describe, expect, test } from "bun:test";

import { jumpMode, jumpTo } from "./jumpTarget";

const port = (scrollTop: number) => ({ clientHeight: 500, scrollHeight: 3000, scrollTop });

describe("the button turns toward your last message", () => {
  test("reading older stuff: down to your message", () => {
    expect(jumpMode(900, port(0))).toBe("down-msg");
  });
  test("your message on screen, not at the end: down to the end", () => {
    expect(jumpMode(40, port(1000))).toBe("down-end");
  });
  test("its top scrolled off, mid-reply or at the end: up to it", () => {
    expect(jumpMode(-300, port(1500))).toBe("up-msg");
    expect(jumpMode(-300, port(2500))).toBe("up-msg");
  });
  test("on it at the end: nothing; no message of yours: the bots button", () => {
    expect(jumpMode(40, port(2500))).toBe("none");
    expect(jumpMode(null, port(2500))).toBe("none");
    expect(jumpMode(null, port(0))).toBe("down-end");
  });
  test("never points down at a message the port cannot scroll to", () => {
    expect(jumpMode(900, port(2500))).toBe("none");
  });
  test("the pill always goes to the end", () => {
    expect(jumpTo("up-msg", 2)).toBe("end");
    expect(jumpTo("down-msg", 1)).toBe("end");
    expect(jumpTo("up-msg", 0)).toBe("msg");
    expect(jumpTo("down-end", 0)).toBe("end");
  });
});
