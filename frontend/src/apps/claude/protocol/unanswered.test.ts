import { describe, expect, test } from "bun:test";

import type { Segment, ToolSegment } from "./types";
import { unansweredNotices, unansweredOf } from "./unanswered";

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
      { questions: [{ question: "Ship it?" }, { question: "Which branch?" }] },
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
    expect(n.detail.join(" ")).toContain("rm -rf build");
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
  test("keeps order and skips everything else", () => {
    const segs: Segment[] = [
      { kind: "text", text: "hi" },
      tool("AskUserQuestion", { questions: [{ question: "A?" }] }, "x" + MARK),
      tool("Bash", { command: "ls" }, "fine"),
    ];
    expect(unansweredNotices(segs).map((n) => n.detail[0])).toEqual(["A?"]);
  });
});
