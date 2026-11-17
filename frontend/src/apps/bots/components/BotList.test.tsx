import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import type { Bot } from "../lib/api";

const { Row } = await import("./BotList");
const { getState } = await import("../state/store");
const { setExpertUnread } = await import("../lib/expertUnread");

const bot = (p: Partial<Bot>): Bot => ({ id: "x1", name: "Canvas expert", status: "idle", seq: 0, kind: "expert", ...p }) as Bot;
const html = (b: Bot) => renderToStaticMarkup(createElement(Row, { b, S: getState() }));

describe("the bots list: an expert's unread dot", () => {
  test("a dot beside the EXPERT badge while it has new activity, gone once seen; never on another bot", () => {
    expect(html(bot({}))).toContain(">EXPERT<");
    expect(html(bot({}))).not.toContain("xunread");
    setExpertUnread("x1", 2);
    const out = html(bot({}));
    expect(out).toContain('class="xunread"');
    expect(out).toContain("2 new things in its activity");
    expect(out.indexOf(">EXPERT<")).toBeLessThan(out.indexOf("xunread"));
    expect(html(bot({ kind: "bot" }))).not.toContain("xunread");
    setExpertUnread("x1", 0);
    expect(html(bot({}))).not.toContain("xunread");
  });
});
