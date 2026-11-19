// Settings › Instructions for an expert: the page as static markup (no act, no polls) and its pure words.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import type { ExpertInstructions } from "../lib/api";

const { InstructionsSection } = await import("./InstructionsSection");
const { instructionsNotes, instructionsUsage } = await import("../lib/expert");

const info: ExpertInstructions = { text: "You are an expert on <canvases>.\n\n## Your memory\n", path: "/x/Canvas Reference/instructions.md",
  limit: 50000, legacy: false, shipped: "", follows_shipped: false };
const noop = () => {};
const page = (p: Partial<Parameters<typeof InstructionsSection>[0]>) =>
  renderToStaticMarkup(createElement(InstructionsSection, { info, draft: info.text, onChange: noop, error: "", saving: false, ...p }));

describe("InstructionsSection", () => {
  test("the whole file in one editor that fills the section, the plain words, the count against the limit", () => {
    const out = page({});
    expect(out).toContain('<label for="bminstrfile" class="text-sm font-medium">Instructions</label>');
    expect(out).toContain("What this expert is told at the start of every chat and session.");
    expect(out).toMatch(/<textarea[^>]*id="bminstrfile"[^>]*>You are an expert on &lt;canvases&gt;\.\n\n## Your memory\n<\/textarea>/);
    expect(out).toMatch(/<textarea[^>]*class="[^"]*flex-1[^"]*resize-none/);
    expect(out).toContain('id="xinstrcount">48 of 50,000 characters<');
    expect(out).not.toContain('role="alert"');
    expect(out).not.toContain("came with the app");
  });
  test("loading: the editor is off until the text is read; a failed read says why", () => {
    const loading = page({ info: null, draft: "" });
    expect(loading).toMatch(/<textarea[^>]*disabled=""[^>]*placeholder="Loading…"/);
    expect(loading).not.toContain("xinstrcount");
    const failed = page({ info: null, draft: "", error: "The instructions could not be read: no such expert: x1" });
    expect(failed).toContain('<p role="alert"');
    expect(failed).toContain("could not be read: no such expert: x1");
    expect(failed).not.toContain("Loading…");
  });
  test("a refused save: its sentence is shown and the draft stays in the editor", () => {
    const out = page({ draft: "my long draft", error: "These instructions are 50,001 characters; the limit is 50,000. Shorten them, or move reference material into a skill." });
    expect(out).toContain('id="xinstrerr">These instructions are 50,001 characters; the limit is 50,000.');
    expect(out).toContain(">my long draft</textarea>");
  });
  test("over the limit the count turns into a warning before any save", () => {
    const out = page({ info: { ...info, limit: 10 }, draft: "eleven char" });
    expect(out).toContain('class="shrink-0 text-destructive" id="xinstrcount">11 of 10 characters. Shorten it to save.<');
    expect(out).toMatch(/<textarea[^>]*aria-invalid="true"/);
  });
  test("a shipped expert says whether its instructions still follow the app", () => {
    expect(page({ info: { ...info, shipped: "canvas-reference", follows_shipped: true } }))
      .toContain("This expert came with the app. Its instructions follow app updates until you save a change here.");
    expect(page({ info: { ...info, shipped: "canvas-reference", follows_shipped: false } }))
      .toContain("You changed these instructions, so app updates no longer replace them.");
  });
});

describe("instructions words", () => {
  test("usage counts the trimmed text in characters, as the server does", () => {
    expect(instructionsUsage("  abc \n", 50000)).toEqual({ text: "3 of 50,000 characters", over: false });
    expect(instructionsUsage("😀😀", 2).over).toBe(false);  // two characters, four UTF-16 units
    expect(instructionsUsage("x".repeat(1201), 1200)).toEqual({ text: "1,201 of 1,200 characters. Shorten it to save.", over: true });
  });
  test("notes: none for an expert you made; the old Settings box's text is named when it is shown", () => {
    expect(instructionsNotes({ shipped: "", follows_shipped: false, legacy: false })).toEqual([]);
    expect(instructionsNotes({ shipped: "", legacy: true })).toEqual(["The last section, “From the user (Settings)”, is what was typed in the old “What it should do” box."]);
    expect(instructionsNotes({ shipped: "k", follows_shipped: true, legacy: true })).toHaveLength(2);
  });
});
