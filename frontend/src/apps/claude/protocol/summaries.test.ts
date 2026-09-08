// The per-tool wording, pinned string by string against the inventory's §C/§D
// tables (`.claude-design/inventory/04-core-chat.md`, itself read off
// `templates/claude/template.html`). These are the strings the pixel diff and
// the legacy suites both measure, so an "improvement" here is a regression.
import { describe, expect, test } from "bun:test";

import {
  formatEditDiff,
  leftoverInput,
  permCardLabel,
  permChoices,
  planBody,
  prettyToolName,
  questionModel,
  segLineCount,
  summarizePermission,
  toolChipSummary,
  toolChipSummaryParts,
  toolStatusGlyph,
  chipImageUrl,
  chipOutput,
} from "./summaries";
import type { PermissionRow, ToolSegment } from "./types";

/** A tool segment with just the two fields the summary reads. */
function seg(name: string, input: Record<string, unknown>): ToolSegment {
  return { kind: "tool", id: "t1", name, input, status: "ok", output: null, images: [] };
}

describe("toolChipSummary", () => {
  test("Bash — `$ ` and the first line only, with a visible clip", () => {
    expect(toolChipSummary(seg("Bash", { command: "ls -la" }))).toBe("$ ls -la");
    expect(toolChipSummary(seg("Bash", { command: "cat <<EOF\nbody\nEOF" }))).toBe("$ cat <<EOF …");
  });

  test("Read / NotebookEdit — the path half only", () => {
    expect(toolChipSummaryParts(seg("Read", { file_path: "/a/b.ts" }))).toEqual({
      lead: "",
      path: "/a/b.ts",
    });
    // `path` and `notebook_path` are the fallbacks, in that order.
    expect(toolChipSummary(seg("Read", { path: "/a/c.ts" }))).toBe("/a/c.ts");
    expect(toolChipSummary(seg("NotebookEdit", { notebook_path: "/n.ipynb" }))).toBe("/n.ipynb");
  });

  test("Glob / Grep — pattern, then `  in ` the place", () => {
    expect(toolChipSummary(seg("Grep", { pattern: "TODO", path: "src" }))).toBe("TODO  in src");
    expect(toolChipSummary(seg("Glob", { pattern: "**/*.ts", glob: "src" }))).toBe(
      "**/*.ts  in src",
    );
    // No place at all: no trailing separator.
    expect(toolChipSummary(seg("Grep", { pattern: "TODO" }))).toBe("TODO");
  });

  test("Edit — `+added -removed` first, then the path", () => {
    const s = seg("Edit", { file_path: "/a/b.ts", old_string: "x\ny", new_string: "1\n2\n3" });
    expect(toolChipSummaryParts(s)).toEqual({ lead: "+3 -2", path: "/a/b.ts" });
    expect(toolChipSummary(s)).toBe("+3 -2  /a/b.ts");
    // "" counts as zero lines, not one: an Edit that only adds has no removals.
    expect(toolChipSummary(seg("Edit", { file_path: "/a", old_string: "", new_string: "z" }))).toBe(
      "+1 -0  /a",
    );
    // An Edit with no file_path is a real, if odd, input — and "+0 -0  " would
    // read as a truncation.
    expect(toolChipSummary(seg("Edit", {}))).toBe("+0 -0");
  });

  test("Write — added lines and the path", () => {
    expect(toolChipSummary(seg("Write", { file_path: "/a/b", content: "a\nb" }))).toBe("+2  /a/b");
  });

  test("Task — the description, else the subagent type", () => {
    expect(toolChipSummary(seg("Task", { description: "find it", subagent_type: "explore" }))).toBe(
      "find it",
    );
    expect(toolChipSummary(seg("Task", { subagent_type: "explore" }))).toBe("explore");
  });

  test("TodoWrite — completed over total", () => {
    const todos = [{ status: "completed" }, { status: "pending" }, { status: "in_progress" }];
    expect(toolChipSummary(seg("TodoWrite", { todos }))).toBe("1/3 done");
    expect(toolChipSummary(seg("TodoWrite", {}))).toBe("0/0 done");
  });

  test("WebFetch / WebSearch — the url, else the query", () => {
    expect(toolChipSummary(seg("WebFetch", { url: "https://x/y" }))).toBe("https://x/y");
    expect(toolChipSummary(seg("WebSearch", { query: "tokens" }))).toBe("tokens");
  });

  test("ExitPlanMode — that a plan HAPPENED, never its first line", () => {
    expect(toolChipSummary(seg("ExitPlanMode", { plan: "## Step one\n- do it" }))).toBe(
      "proposed a plan",
    );
  });

  test("AskUserQuestion — the first usable question, verbatim", () => {
    const questions = [{ question: "" }, { question: "Which one?\nmore" }];
    expect(toolChipSummary(seg("AskUserQuestion", { questions }))).toBe("Which one? …");
    expect(toolChipSummary(seg("AskUserQuestion", { questions: [] }))).toBe("");
  });

  test("unknown / MCP — nothing honest to add; the name is already in the row", () => {
    expect(toolChipSummary(seg("mcp__server__do_thing", { a: 1 }))).toBe("");
    expect(toolChipSummary(null)).toBe("");
  });

  test("a non-string value is JSON-encoded, never coerced", () => {
    expect(toolChipSummary(seg("Bash", { command: { a: 1 } }))).toBe('$ {"a":1}');
  });
});

test("prettyToolName splits an MCP id and leaves everything else alone", () => {
  expect(prettyToolName("mcp__my_server__do_the_thing")).toBe("my server: do the thing");
  expect(prettyToolName("Bash")).toBe("Bash");
  expect(prettyToolName(undefined)).toBe("");
});

test("permCardLabel truncates past 28 characters, and not before", () => {
  expect(permCardLabel("Edit")).toBe("Edit");
  expect(permCardLabel("x".repeat(28))).toBe("x".repeat(28));
  expect(permCardLabel("x".repeat(29))).toBe("x".repeat(27) + "…");
});

test("segLineCount counts \"\" as zero", () => {
  expect(segLineCount("")).toBe(0);
  expect(segLineCount("a")).toBe(1);
  expect(segLineCount("a\nb")).toBe(2);
  expect(segLineCount(undefined)).toBe(0);
});

test("formatEditDiff marks every line and is shared by card and chip", () => {
  expect(formatEditDiff({ old_string: "a\nb", new_string: "c" })).toBe("- a\n- b\n+ c");
  // Non-strings are JSON-encoded rather than coerced.
  expect(formatEditDiff({ old_string: 1, new_string: null })).toBe("- 1\n+ ");
  expect(formatEditDiff(null)).toBe("- \n+ ");
});

describe("summarizePermission", () => {
  const row = (tool: string, input: Record<string, unknown>) => ({ tool, input });

  test("Bash — description over the command", () => {
    expect(summarizePermission(row("Bash", { description: "list", command: "ls" }))).toEqual({
      sub: "list",
      body: "ls",
      covered: ["description", "command"],
    });
  });

  test("Edit — path plus the every-occurrence annotation, diff as the body", () => {
    const out = summarizePermission(
      row("Edit", { file_path: "/a", replace_all: true, old_string: "x", new_string: "y" }),
    );
    expect(out.sub).toBe("/a  (every occurrence)");
    expect(out.body).toBe("- x\n+ y");
    expect(out.covered).toEqual(["file_path", "replace_all", "old_string", "new_string"]);
  });

  test("Read — file_path, else path; no body", () => {
    expect(summarizePermission(row("Read", { path: "/a" }))).toEqual({
      sub: "/a",
      body: "",
      covered: ["path"],
    });
  });

  test("Glob / Grep — \"path  in glob\" over the pattern", () => {
    expect(summarizePermission(row("Grep", { path: "src", glob: "*.ts", pattern: "TODO" }))).toEqual(
      { sub: "src  in *.ts", body: "TODO", covered: ["path", "glob", "pattern"] },
    );
  });

  test("an unknown tool dumps the whole object, so every key is covered", () => {
    const out = summarizePermission(row("Whatever", { a: 1, b: 2 }));
    expect(out.sub).toBe("");
    expect(out.body).toBe('{\n  "a": 1,\n  "b": 2\n}');
    expect(out.covered).toEqual(["a", "b"]);
    expect(leftoverInput({ a: 1, b: 2 }, out.covered)).toBeNull();
  });

  test("nothing truncates — the payload goes in verbatim", () => {
    const content = "x".repeat(5000);
    expect(summarizePermission(row("Write", { file_path: "/a", content })).body).toBe(content);
  });
});

test("leftoverInput keeps what was not rendered, __proto__ included", () => {
  expect(leftoverInput({ a: 1, b: 2 }, ["a"])).toEqual({ b: 2 });
  expect(leftoverInput({ a: 1 }, ["a"])).toBeNull();
  const hostile = JSON.parse('{"a":1,"__proto__":{"x":1}}') as Record<string, unknown>;
  const rest = leftoverInput(hostile, ["a"]);
  expect(Object.keys(rest ?? {})).toEqual(["__proto__"]);
});

describe("permChoices", () => {
  test("a file tool gets the whole-tool grant; Bash does not", () => {
    expect(permChoices({ tool: "Edit" }, "prompt").map((c) => c.text)).toEqual([
      "Allow",
      "Allow all Edit in this reply",
      "Allow, and let Claude decide from here",
      "Deny",
    ]);
    expect(permChoices({ tool: "Bash" }, "prompt").map((c) => c.text)).toEqual([
      "Allow",
      "Allow, and let Claude decide from here",
      "Deny",
    ]);
  });

  test("no escalation when the run is already auto, or still planning", () => {
    expect(permChoices({ tool: "Bash" }, "auto").map((c) => c.text)).toEqual(["Allow", "Deny"]);
    expect(permChoices({ tool: "Bash" }, "plan").map((c) => c.text)).toEqual(["Allow", "Deny"]);
  });

  test("exactly one primary, and it is Allow/once", () => {
    const choices = permChoices({ tool: "Read" });
    expect(choices.filter((c) => c.primary)).toHaveLength(1);
    expect(choices[0]).toMatchObject({ decision: "allow", scope: "once", mode: "", primary: true });
    expect(choices[choices.length - 1]).toMatchObject({ decision: "deny", scope: "once" });
  });
});

describe("questionModel", () => {
  const q = (question: string, extra: Record<string, unknown> = {}) => ({
    question,
    options: [{ label: "a" }, { label: "b" }],
    ...extra,
  });

  test("one single-select question is the oneShot card", () => {
    const m = questionModel({ questions: [q("Which?")] });
    expect(m.answerable).toBe(true);
    expect(m.oneShot).toBe(true);
    expect(m.extra).toBeNull();
  });

  test("multiSelect is never oneShot, and neither are two questions", () => {
    expect(questionModel({ questions: [q("Which?", { multiSelect: true })] }).oneShot).toBe(false);
    expect(questionModel({ questions: [q("A?"), q("B?")] }).oneShot).toBe(false);
  });

  test("all of them or none: a dropped question makes the card unanswerable", () => {
    expect(questionModel({ questions: [q("A?"), { question: "B?", options: [] }] })).toMatchObject({
      answerable: false,
    });
    // Duplicate texts collapse in the answer record, so they are unanswerable too.
    expect(questionModel({ questions: [q("A?"), q("A?")] }).answerable).toBe(false);
    expect(questionModel({}).answerable).toBe(false);
  });

  test("anything beyond `questions` is kept for the leftover dump", () => {
    expect(questionModel({ questions: [q("A?")], metadata: { x: 1 } }).extra).toEqual({
      metadata: { x: 1 },
    });
  });
});

test("planBody takes a usable plan and leaves anything else to the dump", () => {
  expect(planBody({ plan: "## do it" })).toBe("## do it");
  expect(planBody({ plan: "" })).toBe("");
  expect(planBody({ plan: 3 })).toBe("");
});

test("toolStatusGlyph: ok draws nothing, an unknown status is running", () => {
  expect(toolStatusGlyph("ok")).toBe("");
  expect(toolStatusGlyph("running")).toBe("");
  expect(toolStatusGlyph("error")).toBe("✗");
  expect(toolStatusGlyph("toString")).toBe("");
  expect(toolStatusGlyph("weird")).toBe("");
});

test("chipOutput tells an empty result from no result yet", () => {
  expect(chipOutput(null)).toBeNull();
  expect(chipOutput("")).toBeNull();
  expect(chipOutput("done")).toBe("done");
  expect(chipOutput("x".repeat(5000))).toHaveLength(4000);
});

test("chipImageUrl validates rather than trusts the media type", () => {
  expect(chipImageUrl("image/png", "AAA=")).toBe("data:image/png;base64,AAA=");
  expect(chipImageUrl("image/png", "AA A\n=")).toBe("data:image/png;base64,AAA=");
  expect(chipImageUrl("text/html", "AAA=")).toBeNull();
  expect(chipImageUrl("image/png", "<script>")).toBeNull();
});

test("a resolved question row's answers are what the poll landed", () => {
  const row: PermissionRow = {
    id: "r1",
    tool: "AskUserQuestion",
    input: {},
    created_at: 0,
    decision: "allow",
    scope: "once",
    mode: "",
    answers: { "Which?": "a, b" },
  };
  expect(Object.values(row.answers)).toEqual(["a, b"]);
});
