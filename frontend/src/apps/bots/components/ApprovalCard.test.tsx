import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

const { ApprovalCard } = await import("./ApprovalCard");
import type { BotEvent } from "../lib/api";

const ev = (extra: Partial<BotEvent> = {}): BotEvent => ({ seq: 4, ts: 1, role: "approval", text: "About to run `ls`. Approve?", ...extra }) as BotEvent;
const html = (e: BotEvent, live = true, extra: { botName?: string; answer?: string | null } = {}) =>
  renderToStaticMarkup(createElement(ApprovalCard, { e, live, ...extra }));

describe("ApprovalCard", () => {
  test("pill, summary, then the raw text", () => {
    const out = html(ev({ severity: "high", risk_summary: "Deletes the build folder." }));
    expect(out).toContain('class="gate-pill high">High risk<');
    expect(out.indexOf("gate-pill")).toBeLessThan(out.indexOf("Deletes the build folder."));
    expect(out.indexOf("Deletes the build folder.")).toBeLessThan(out.indexOf("About to run"));
  });
  test("keeps the hooks the thread's delegated click handler reads", () => {
    const out = html(ev());
    expect(out).toContain("msg approval");
    expect(out).toContain('data-seq="4"');
    expect(out).toContain('data-approve="1"');
    expect(out).toContain('data-deny="1"');
  });
  test("no assessment: no pill, no summary, the card still works", () => {
    const out = html(ev());
    expect(out).not.toContain("gate-pill");
    expect(out).not.toContain("gate-sum");
    expect(out).toContain("data-approve");
    expect(out).toContain("data-deny");
  });
  test("buttons: Approve, Always approve, Deny", () => {
    const out = html(ev({ forever: { kind: "bash", key: "ls", label: "runs ls" } as never }));
    const a = out.indexOf("data-approve"), f = out.indexOf("data-forever"), d = out.indexOf("data-deny");
    expect(a).toBeGreaterThan(0);
    expect(f).toBeGreaterThan(a);
    expect(d).toBeGreaterThan(f);
    expect(out).toContain(">Always approve<");
    expect(out).toContain("Never ask again when this bot runs ls.");
    expect(out).not.toContain("ask again</button>");
  });
  test("no forever, no Always approve", () => {
    expect(html(ev())).not.toContain("data-forever");
  });
  test("the head names the bot: live and settled", () => {
    expect(html(ev(), true, { botName: "Scout" })).toContain("Scout wants your OK");
    expect(html(ev(), false, { botName: "Scout" })).toContain("Scout asked for your OK");
  });
  test("settled keeps the pill, summary and command; the buttons go", () => {
    const out = html(ev({ severity: "low", risk_summary: "Lists files." }), false);
    expect(out).toContain("msg approval settled");
    expect(out).toContain("Low risk");
    expect(out).toContain("Lists files.");
    expect(out).toContain("About to run");
    expect(out).not.toContain("<button");
  });
  test("settled shows the verdict from the answering line, nothing when unknown", () => {
    expect(html(ev(), false, { answer: "approve" })).toContain("✓ Approved");
    expect(html(ev(), false, { answer: "deny" })).toContain("✗ Denied");
    expect(html(ev(), false, { answer: "go to the page" })).not.toContain("gate-status");
    expect(html(ev(), false)).not.toContain("gate-status");
  });
});
