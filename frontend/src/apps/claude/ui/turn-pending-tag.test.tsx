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
  test("a live follow-up (no pending) gets the tag only when it is the hinted one, no dimming, click sends now", () => {
    const plain: UserTurn = { role: "user", key: "k2", text: "go on" };
    expect(html(plain)).not.toContain("turn-pending");
    let sent = 0;
    const el = createElement(Turn, { turn: plain, sendNowHint: true, onSendNow: () => (sent += 1) });
    const out = renderToStaticMarkup(el);
    expect(out).toContain("(Ctrl+X to send now)");
    expect(out).toContain("turn-pending");
    expect(out).not.toContain("is-pending");
    expect(out).not.toContain(">queued<");
  });
  test("not sent is unchanged", () => {
    expect(html({ ...queued, pending: "notSent" })).toContain("not sent");
  });
});
