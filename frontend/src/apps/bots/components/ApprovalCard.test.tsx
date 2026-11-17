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
  test("a real card: the command alone as the body, the why as the sub", () => {
    const out = html(ev({ text: "About to $ gh api x. It runs a command on this Mac. Approve?", detail: "$ gh api x" }));
    expect(out).toContain("gh api x");
    expect(out).not.toContain("About to");
    expect(out).not.toContain("$ gh api");
    expect(out).toContain("It runs a command on this Mac.");
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

describe("ApprovalCard: an expert's skill proposal", () => {
  const diff = "--- a/skills/canvas.md\n+++ b/skills/canvas.md\n@@ -1 +1,2 @@\n # Canvas\n+- reference upstream UDFs by name";
  const prop = (extra: Partial<BotEvent> = {}) => ev({ text: "Update the skill “Canvas” with 2 lessons.", detail: diff, expert_proposal: "p1",
    score_before: { score: 11, total: 14 }, score_after: { score: 12, total: 14 }, ...extra });
  test("title, the diff as the body, the scores line", () => {
    const out = html(prop());
    expect(out).toContain("Proposed skill change");
    expect(out).toContain("reference upstream UDFs by name");
    expect(out).toContain("+++ b/skills/canvas.md");
    expect(out).toContain("Checks: 11/14 → 12/14");
    expect(out).toContain("Update the skill “Canvas” with 2 lessons.");
    expect(out).not.toContain("wants your OK");
  });
  test("Approve and Reject on the thread's hooks; never Always approve", () => {
    const out = html(prop({ forever: { kind: "builtin", key: "x", label: "does x" } }));
    expect(out).toContain('data-approve="1"');
    expect(out).toContain('data-deny="1"');
    expect(out).toContain(">Approve<");
    expect(out).toContain(">Reject<");
    expect(out).not.toContain("data-forever");
    expect(out).not.toContain("Always approve");
    expect(out).toContain("msg approval proposal");
    expect(out).toContain('data-seq="4"');
  });
  test("no scores line unless both runs exist", () => {
    expect(html(prop({ score_before: null, score_after: null }))).not.toContain("Checks:");
    expect(html(prop({ score_after: null }))).not.toContain("Checks:");
  });
  test("warnings: a short Heads up line, kept out of the summary; none for a pure addition", () => {
    const text = "I'd like to update the skill “Canvas”.\nWarning: this change removes the section “Keys”.\nApprove?";
    const out = html(prop({ text, score_before: null, score_after: null, expert_warnings: ["removes the section “Keys”", "removes 1 code block"] } as Partial<BotEvent>));
    expect(out).toContain("Heads up: this change removes the section “Keys” · removes 1 code block");
    expect(out).not.toContain("Warning: this change");
    const fromText = html(prop({ text, score_before: null, score_after: null }));
    expect(fromText).toContain("Heads up: this change removes the section “Keys”");
    expect(html(prop({ score_before: null, score_after: null, expert_warnings: [] } as Partial<BotEvent>))).not.toContain("Heads up");
  });
  test("settled: no buttons, the verdict says Rejected or Approved", () => {
    expect(html(prop(), false, { answer: "deny" })).toContain("✗ Rejected");
    expect(html(prop(), false, { answer: "approve" })).toContain("✓ Approved");
    expect(html(prop(), false)).not.toContain("<button");
  });
});
