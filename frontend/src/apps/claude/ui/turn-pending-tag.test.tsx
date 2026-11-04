// The tag on a queued / not-sent user bubble. A queued bubble says nothing in
// words except the ONE hint on the newest, like the CLI.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

const { Turn } = await import("./Turn");

import type { UserTurn } from "../protocol/controller-api";

const html = (turn: UserTurn, sendNowHint?: boolean) =>
  renderToStaticMarkup(
    createElement(Turn, { turn, onPullPending: () => {}, ...(sendNowHint ? { sendNowHint } : {}) }),
  );
const queued: UserTurn = { role: "user", key: "k1", text: "hello", pending: "queued" };

describe("Turn pending tag", () => {
  test("the newest queued bubble reads (Ctrl+X to send now), never the word queued", () => {
    const out = html(queued, true);
    expect(out).toContain("turn-pending");
    expect(out).toContain("(Ctrl+X to send now)");
    expect(out).not.toContain(">queued<");
  });
  test("other queued bubbles show no text tag but stay dimmed", () => {
    const out = html(queued);
    expect(out).not.toContain("turn-pending");
    expect(out).toContain("is-pending");
  });
  test("not sent is unchanged", () => {
    expect(html({ ...queued, pending: "notSent" })).toContain("not sent");
  });
});
