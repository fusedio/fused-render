import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const { GateCard } = await import("./GateCard");
import type { GateCardProps } from "./GateCard";

const base: GateCardProps = {
  head: "Bot wants your OK",
  body: "rm -rf build",
  bodyWhat: "command",
  resolved: false,
  actions: [
    { key: "a", text: "Approve", kind: "primary", attrs: { "data-approve": "1" } },
    { key: "d", text: "Deny", kind: "default", attrs: { "data-deny": "1" } },
  ],
};
const html = (over: Partial<GateCardProps> = {}) => renderToStaticMarkup(createElement(GateCard, { ...base, ...over }));

describe("GateCard", () => {
  test("pill and summary come before the body", () => {
    const out = html({ severity: "high", riskSummary: "Deletes the build folder." });
    expect(out).toContain('class="gate-pill high">High risk<');
    expect(out.indexOf("gate-pill")).toBeLessThan(out.indexOf("Deletes the build folder."));
    expect(out.indexOf("Deletes the build folder.")).toBeLessThan(out.indexOf("rm -rf build"));
  });
  test("pending shows the placeholder, unless resolved or already assessed", () => {
    expect(html({ riskPending: true })).toContain("Assessing risk…");
    expect(html({ riskPending: true, resolved: true })).not.toContain("Assessing risk…");
    expect(html({ riskPending: true, severity: "low" })).not.toContain("Assessing risk…");
  });
  test("an unknown severity draws no pill", () => {
    const out = html({ severity: "critical" });
    expect(out).not.toContain("gate-pill");
    expect(out).not.toContain("gate-risk");
  });
  test("details are a collapsed disclosure below the body", () => {
    const out = html({ details: '{"timeout": 600000}' });
    expect(out).toContain("<details");
    expect(out).not.toContain("<details open");
    expect(out).toContain("<summary>Details</summary>");
    expect(out.indexOf("rm -rf build")).toBeLessThan(out.indexOf("<details"));
    expect(out).toContain("timeout");
  });
  test("no details, no disclosure", () => {
    expect(html()).not.toContain("<details");
  });
  test("actions keep the caller's order and carry their attrs", () => {
    const out = html({
      actions: [
        { key: "d", text: "Deny", kind: "default", attrs: { "data-deny": "1" } },
        { key: "l", text: "Decide for me", kind: "link", title: "hint" },
        { key: "a", text: "Approve", kind: "primary", attrs: { "data-approve": "1" }, disabled: true },
      ],
    });
    expect(out.indexOf("Deny")).toBeLessThan(out.indexOf("Decide for me"));
    expect(out.indexOf("Decide for me")).toBeLessThan(out.indexOf("Approve"));
    expect(out).toContain('data-deny="1"');
    expect(out).toContain('data-approve="1"');
    expect(out).toContain('title="hint"');
    expect(out).toMatch(/gate-btn is-link/);
    expect(out).toMatch(/gate-btn is-primary"[^>]*disabled/);
  });
  test("resolved hides the actions and keeps the status", () => {
    const out = html({ resolved: true, status: { tone: "allow", text: "✓ Allowed" } });
    expect(out).not.toContain("<button");
    expect(out).not.toContain("gate-actions");
    expect(out).toContain("is-resolved");
    expect(out).toContain('class="gate-status allow">✓ Allowed<');
  });
  test("touch marks the card for the 44px row", () => {
    expect(html({ touch: true })).toContain("is-touch");
    expect(html()).not.toContain("is-touch");
  });
  test("className and rootProps land on the root", () => {
    const out = html({ className: "msg approval", rootProps: { "data-seq": 4, title: "t" } });
    expect(out).toMatch(/^<div[^>]*data-seq="4"/);
    expect(out).toContain("gate-card");
    expect(out).toContain("msg approval");
  });
});
