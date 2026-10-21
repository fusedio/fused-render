import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

const { ApprovalCard } = await import("./ApprovalCard");
import type { BotEvent } from "../lib/api";

const ev = (extra: Partial<BotEvent> = {}): BotEvent => ({ seq: 4, ts: 1, role: "approval", text: "About to run `ls`. Approve?", ...extra }) as BotEvent;
const html = (e: BotEvent, live = true) => renderToStaticMarkup(createElement(ApprovalCard, { e, live }));

describe("ApprovalCard", () => {
  test("pill, summary, then the raw text", () => {
    const out = html(ev({ severity: "high", risk_summary: "Deletes the build folder." }));
    expect(out).toContain('class="riskpill high">High risk<');
    expect(out.indexOf("riskpill")).toBeLessThan(out.indexOf("Deletes the build folder."));
    expect(out.indexOf("Deletes the build folder.")).toBeLessThan(out.indexOf("About to run"));
    expect(out).toContain("riskraw");
  });
  test("no assessment: no pill, no summary, the card still works", () => {
    const out = html(ev());
    expect(out).not.toContain("riskpill");
    expect(out).not.toContain("risksum");
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
    expect(out).not.toContain("ask again</button>");
  });
  test("no forever, no Always approve", () => {
    expect(html(ev())).not.toContain("data-forever");
  });
  test("settled keeps the pill and summary", () => {
    const out = html(ev({ severity: "low", risk_summary: "Lists files." }), false);
    expect(out).toContain("msg approval settled");
    expect(out).toContain("Low risk");
    expect(out).toContain("Lists files.");
  });
});
