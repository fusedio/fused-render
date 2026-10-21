import { describe, expect, test } from "bun:test";

import { approvalParts } from "./approvalParts";

describe("approvalParts", () => {
  test("exact match: detail is the body, the why is the sub", () => {
    const p = approvalParts({ text: "About to open the page. It loads a site. Approve?", detail: "open the page" });
    expect(p).toEqual({ body: "open the page", bodyWhat: "request", sub: "It loads a site." });
  });
  test("a $ command loses its prefix and is a command", () => {
    const p = approvalParts({ text: "About to $ ls -la. It runs a command on this Mac. Approve?", detail: "$ ls -la" });
    expect(p).toEqual({ body: "ls -la", bodyWhat: "command", sub: "It runs a command on this Mac." });
  });
  test("a non-$ preview is a request", () => {
    expect(approvalParts({ text: "About to click Save. Writes. Approve?", detail: "click Save" }).bodyWhat).toBe("request");
  });
  test("no detail: the text is the body, no sub", () => {
    expect(approvalParts({ text: "About to run `ls`. Approve?" })).toEqual({ body: "About to run `ls`. Approve?", bodyWhat: "request", sub: undefined });
  });
  test("text that does not match: no sub", () => {
    const p = approvalParts({ text: "Please confirm the thing", detail: "$ x" });
    expect(p.sub).toBeUndefined();
    expect(p.body).toBe("x");
  });
  test("an empty why: no sub", () => {
    expect(approvalParts({ text: "About to open. Approve?", detail: "open" }).sub).toBeUndefined();
  });
});
