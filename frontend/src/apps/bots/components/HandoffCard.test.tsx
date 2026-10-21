import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

const { HandoffCard } = await import("./Thread");
import type { BotEvent } from "../lib/api";

const relayEv = (relay: Record<string, unknown>): BotEvent => ({
  seq: 9, ts: 1, role: "question", text: "Scout needs your OK: About to $ gh api x. It runs a command on this Mac. Approve?",
  handoff: { id: "h1", target: "t1", target_name: "Scout", state: "blocked" },
  relay: { bot: "t1", seq: 4, kind: "approval", options: ["Approve", "Deny"], ...relay },
}) as unknown as BotEvent;
const html = (e: BotEvent) => renderToStaticMarkup(createElement(HandoffCard, { e, state: "blocked", targetLive: true, targetWaiting: 4 }));

describe("relayed approval card", () => {
  test("with the target's text + detail: the command alone as the body, the why as the sub", () => {
    const out = html(relayEv({ text: "About to $ gh api x. It runs a command on this Mac. Approve?", detail: "$ gh api x" }));
    expect(out).toContain("Scout needs your OK");
    expect(out).toContain("gh api x");
    expect(out).not.toContain("About to");
    expect(out).not.toContain("$ gh api");
    expect(out).toContain('class="gate-sub">It runs a command on this Mac.<');
  });
  test("without them: the whole text, as before", () => {
    const out = html(relayEv({}));
    expect(out).toContain("About to $ gh api x. It runs a command on this Mac. Approve?");
    expect(out).not.toContain("gate-sub");
  });
});
