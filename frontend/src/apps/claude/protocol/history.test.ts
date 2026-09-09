import { describe, expect, test } from "bun:test";

import { ago, historyToTurns, paneSlashes, rowPane, sessionTitle } from "./history";
import type { HistoryResponse } from "./types";
import { composeOutgoing, formatAnnotations, MARKER_VIEW, paneShotBlock } from "./wire";

const stat = { path: "/t.jsonl", mtime: 1, size: 2 };

describe("historyToTurns", () => {
  test("a user turn shows what was TYPED and keeps the raw wire", () => {
    const raw = composeOutgoing("fix it", [paneShotBlock([{ kind: "pane", view: "/p.png" }], "app")]);
    const turns = historyToTurns({ turns: [{ role: "user", text: raw, uuid: "u-1" }], transcript: stat });
    expect(turns[0]).toEqual({ role: "user", key: "u-1", text: "fix it", raw, uuid: "u-1" });
  });

  test("a payload with no uuid still renders, it just cannot be anchored to", () => {
    const turns = historyToTurns({
      turns: [{ role: "user", text: "hi" } as HistoryResponse["turns"][number]],
      transcript: stat,
    });
    expect(turns[0].key).toBe("h:0");
    expect((turns[0] as { uuid?: string }).uuid).toBeUndefined();
  });

  test("segments ride along only when the turn had any (agent.py:5041)", () => {
    const turns = historyToTurns({
      turns: [
        { role: "assistant", text: "plain" },
        { role: "assistant", text: "tail", segments: [{ kind: "text", text: "tail" }] },
      ],
      transcript: stat,
    });
    expect("segments" in turns[0]).toBe(false);
    expect((turns[1] as { segments?: unknown[] }).segments?.length).toBe(1);
  });

  // R2-3/R2-14: the assistant fallback is unconditional, so a role this mapper
  // did not know became an assistant turn — a failed turn rendered as the
  // model's own prose after a reload, in normal type, while the live run had
  // shown the same failure in red.
  test("a failed turn restores as a red error turn, not as prose", () => {
    const turns = historyToTurns({
      turns: [
        { role: "user", text: "go", uuid: "u1" },
        { role: "error", text: "API Error: Can't reach the API server (ENOTFOUND)" },
      ],
      transcript: stat,
    });
    expect(turns[1].role).toBe("error");
    expect((turns[1] as { text: string }).text).toContain("ENOTFOUND");
    // `kind` is required on an ErrorTurn and comes from the same classifier the
    // live path runs the poll's `error` through.
    expect(typeof (turns[1] as { kind: string }).kind).toBe("string");
  });

  test("an error turn does not swallow the reply before it", () => {
    const turns = historyToTurns({
      turns: [
        { role: "assistant", text: "here is half an answer" },
        { role: "error", text: "You've hit your session limit" },
      ],
      transcript: stat,
    });
    expect(turns.map((t) => t.role)).toEqual(["assistant", "error"]);
    expect((turns[0] as { text: string }).text).toBe("here is half an answer");
  });

  test("`stopped` lands on the LAST turn only (agent.py:5049)", () => {
    const turns = historyToTurns({
      turns: [
        { role: "user", text: "go", uuid: "u1" },
        { role: "assistant", text: "half a thought", stopped: true },
      ],
      transcript: stat,
    });
    expect((turns[1] as { stopped?: boolean }).stopped).toBe(true);
  });

  test("a `stopped` flag on a NON-last turn is ignored", () => {
    const turns = historyToTurns({
      turns: [
        { role: "assistant", text: "one", stopped: true },
        { role: "assistant", text: "two" },
      ],
      transcript: stat,
    });
    expect((turns[0] as { stopped?: boolean }).stopped).toBeUndefined();
    expect((turns[1] as { stopped?: boolean }).stopped).toBeUndefined();
  });

  test("an empty or malformed payload is an empty transcript, never a throw", () => {
    expect(historyToTurns({ turns: [], transcript: stat })).toEqual([]);
    expect(historyToTurns({} as HistoryResponse)).toEqual([]);
  });
});

describe("sessionTitle (T:18088)", () => {
  test("plain words are the title", () => {
    expect(sessionTitle({ id: "s1", preview: "make the header sticky" })).toBe("make the header sticky");
  });

  test("a TRUNCATED block opener is cut even with no closing tag", () => {
    // What the store actually holds: the head of the wire, 80 chars, so the
    // `</pane-shot>` the strip matches on is not in the string at all.
    const preview = "<pane-shot>\nThe user attached a pi";
    expect(sessionTitle({ id: "s1", preview })).toBe(MARKER_VIEW);
  });

  test("the annotation preamble is tag-less by construction and still cut", () => {
    expect(sessionTitle({ id: "s1", preview: "The user annotated 1 element in the l" })).toBe(
      "annotations",
    );
  });

  test("words before a block win over the marker", () => {
    const preview = composeOutgoing("centre this", [
      formatAnnotations([{ label: "A", tag: "p", content: "x" }], "file"),
    ]);
    expect(sessionTitle({ id: "s1", preview })).toBe("centre this");
  });

  test("never blank: the id is the last resort", () => {
    expect(sessionTitle({ id: "sess-9", preview: "" })).toBe("sess-9");
    expect(sessionTitle(null)).toBe("");
  });
});

describe("ago (T:17945)", () => {
  const at = (secondsAgo: number) => ago(1_000_000 - secondsAgo, () => 1_000_000 * 1000);
  test("the five buckets", () => {
    expect(at(0)).toBe("now");
    expect(at(59)).toBe("now");
    expect(at(60)).toBe("1m ago");
    expect(at(3599)).toBe("59m ago");
    expect(at(3600)).toBe("1h ago");
    expect(at(86_400)).toBe("yesterday");
    expect(at(172_800)).toBe("2d ago");
  });
  test("a future timestamp clamps to \"now\" rather than going negative", () => {
    expect(ago(2_000_000, () => 1_000_000 * 1000)).toBe("now");
  });
});

describe("rowPane (T:18113)", () => {
  test("a row opened on THIS target names no file", () => {
    expect(rowPane({ pane: "/a/b.py" }, "/a/b.py")).toBe("");
    expect(rowPane({ pane: "" }, "/a/b.py")).toBe("");
  });
  test("another file is named", () => {
    expect(rowPane({ pane: "/a/c.py" }, "/a/b.py")).toBe("/a/c.py");
  });
  test("only a drive-letter path has its backslashes rewritten", () => {
    expect(paneSlashes("C:\\a\\b.py")).toBe("C:/a/b.py");
    expect(paneSlashes("/a/we\\ird.py")).toBe("/a/we\\ird.py");
    expect(rowPane({ pane: "C:\\a\\b.py" }, "C:/a/b.py")).toBe("");
  });
});
