// A queued line is not a transcript bubble (it lives in ui/QueuedStrip); only a
// not-sent line wears a tag on its bubble.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

const { Turn } = await import("./Turn");

import type { UserTurn } from "../protocol/controller-api";

const html = (turn: UserTurn) => renderToStaticMarkup(createElement(Turn, { turn, onPullPending: () => {} }));
const base: UserTurn = { role: "user", key: "k1", text: "hello" };

describe("Turn pending tag", () => {
  test("a not-sent bubble says so and is a door back into the box", () => {
    const out = html({ ...base, pending: "notSent" });
    expect(out).toContain("not sent");
    expect(out).toContain("is-pending");
  });
  test("a plain bubble carries no tag", () => {
    const out = html(base);
    expect(out).not.toContain("turn-pending");
    expect(out).not.toContain("is-pending");
  });
});
