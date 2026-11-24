import { describe, expect, test } from "bun:test";

import type { Segment, ToolSegment } from "./types";
import {
  stripUnansweredMarker,
  unansweredCardText,
  unansweredNotices,
  unansweredOf,
  unansweredReasonOf,
} from "./unanswered";

const MARK = " [fused:unanswered reason=timeout minutes=60]";
const tool = (name: string, input: Record<string, unknown>, output: string | null): ToolSegment => ({
  kind: "tool",
  id: "t1",
  name,
  input,
  status: "error",
  output,
  images: [],
});

describe("unansweredOf", () => {
  test("an AskUserQuestion denied by the timeout names the question and the wait", () => {
    const seg = tool(
      "AskUserQuestion",
      { questions: [{ question: "Ship it?", options: [{ label: "Yes" }] }, { question: "Which branch?", options: [{ label: "Yes" }] }] },
      "No answer from the Fused Render chat window after 60 minutes — treating this as denied." + MARK,
    );
    expect(unansweredOf(seg)).toEqual({
      reason: "timeout",
      title: "Unanswered question — timed out after 60 min",
      detail: ["Ship it?", "Which branch?"],
    });
  });

  test("a permission-gated tool reads as an approval, with the tool's own summary", () => {
    const seg = tool("Bash", { command: "rm -rf build" }, "The run moved on before this was answered. [fused:unanswered reason=abandoned]");
    const n = unansweredOf(seg)!;
    expect(n.reason).toBe("abandoned");
    expect(n.title).toBe("Unanswered approval — the run moved on first");
    expect(n.detail).toEqual(["Bash: $ rm -rf build"]);
  });

  test("an approval's detail is one capped line, not the whole script", () => {
    const cmd = Array.from({ length: 200 }, (_, i) => "echo line" + i).join("\n");
    const n = unansweredOf(tool("Bash", { command: cmd }, "x" + MARK))!;
    expect(n.detail).toHaveLength(1);
    expect(n.detail[0]).not.toContain("\n");
    expect(n.detail[0].length).toBeLessThanOrEqual("Bash: ".length + 80);
  });

  test("the marker must END the output and the call must have errored", () => {
    // a Bash that merely prints the marker (grep, git diff, cat of the test file)
    expect(unansweredOf(tool("Bash", {}, "tests/x.py:3: [fused:unanswered reason=timeout]\nmore output"))).toBeNull();
    expect(unansweredOf({ ...tool("Bash", {}, "x" + MARK), status: "ok" })).toBeNull();
  });

  test("expired says the reply ended", () => {
    const seg = tool("Write", { file_path: "/a" }, "The reply ended before this was answered. [fused:unanswered reason=expired]");
    expect(unansweredOf(seg)!.title).toBe("Unanswered approval — the reply ended first");
  });

  test("a human's own denial, a plain result and a running call are not notices", () => {
    expect(unansweredOf(tool("Bash", {}, "The user denied this request."))).toBeNull();
    expect(unansweredOf(tool("Bash", {}, "ok"))).toBeNull();
    expect(unansweredOf(tool("Bash", {}, null))).toBeNull();
    expect(unansweredOf({ kind: "text", text: MARK } as Segment)).toBeNull();
  });
});

describe("unansweredNotices", () => {
  test("looks inside a subagent's children", () => {
    const parent = {
      ...tool("Agent", {}, "done"),
      status: "ok" as const,
      children: [tool("AskUserQuestion", { questions: [{ question: "Deep?", options: [{ label: "Yes" }] }] }, "x" + MARK)],
    };
    expect(unansweredNotices([parent]).map((n) => n.detail[0])).toEqual(["Deep?"]);
  });

  test("keeps order and skips everything else", () => {
    const segs: Segment[] = [
      { kind: "text", text: "hi" },
      tool("AskUserQuestion", { questions: [{ question: "A?", options: [{ label: "Yes" }] }] }, "x" + MARK),
      tool("Bash", { command: "ls" }, "fine"),
    ];
    expect(unansweredNotices(segs).map((n) => n.detail[0])).toEqual(["A?"]);
  });
});

describe("approval and question lines", () => {
  test("no summary means no dangling colon", () => {
    expect(unansweredOf(tool("Mystery", {}, "x" + MARK))!.detail).toEqual(["Mystery"]);
  });

  test("a plan reads as a plan, with no raw tool name", () => {
    const n = unansweredOf(tool("ExitPlanMode", { plan: "Do it" }, "x" + MARK))!;
    expect(n.title).toBe("Unanswered plan — timed out after 60 min");
    expect(n.detail).toEqual([]);
  });

  test("an MCP tool is named readably", () => {
    const n = unansweredOf(tool("mcp__my_srv__do_thing", { a: 1 }, "x" + MARK))!;
    expect(n.detail[0].startsWith("my srv: do thing")).toBe(true);
    expect(n.detail[0]).not.toContain("mcp__");
  });

  test("question texts are one capped line each; unusable questions are dropped", () => {
    const long = "word ".repeat(60);
    const n = unansweredOf(
      tool("AskUserQuestion", { questions: [{ question: "A\nB?", options: [{ label: "Yes" }] }, { question: long, options: [{ label: "Yes" }] }, { nope: 1 }] }, "x" + MARK),
    )!;
    expect(n.detail[0]).toBe("A B?");
    expect(n.detail[1].length).toBeLessThanOrEqual(80);
    expect(n.detail).toHaveLength(2);
  });
});

describe("shared helpers", () => {
  test("stripUnansweredMarker drops only the trailing marker", () => {
    expect(stripUnansweredMarker("No answer. [fused:unanswered reason=timeout minutes=60]")).toBe("No answer.");
    expect(stripUnansweredMarker("a [fused:unanswered reason=timeout] b")).toBe("a [fused:unanswered reason=timeout] b");
    expect(stripUnansweredMarker(null)).toBeNull();
  });

  test("stripUnansweredMarker leaves an unmarked output byte-for-byte, trailing newline included", () => {
    expect(stripUnansweredMarker("line 1\nline 2\n")).toBe("line 1\nline 2\n");
    expect(stripUnansweredMarker("padded  \n\n")).toBe("padded  \n\n");
  });

  test("a card's unanswered reason and its status line", () => {
    expect(unansweredReasonOf({ decision: "expired" })).toBe("expired");
    expect(unansweredReasonOf({ decision: "deny", reason: "timeout" })).toBe("timeout");
    expect(unansweredReasonOf({ decision: "deny", reason: "abandoned" })).toBe("abandoned");
    expect(unansweredReasonOf({ decision: "deny" })).toBeNull();
    expect(unansweredCardText("expired", "answered")).toBe("◦ Unanswered — the reply ended before you answered");
    expect(unansweredCardText("abandoned", "decided")).toBe("◦ Unanswered — the run moved on before you decided");
  });
});
