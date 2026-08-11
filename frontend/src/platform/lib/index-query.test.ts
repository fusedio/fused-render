import { describe, expect, test } from "bun:test";
import { cellText, outcomeFrom } from "./index-query";

describe("cellText", () => {
  test("null is spelled out, because an empty cell is a different fact", () => {
    expect(cellText(null)).toBe("NULL");
    expect(cellText(undefined)).toBe("NULL");
    expect(cellText("")).toBe("");
  });

  test("numbers and booleans render, 0 and false included", () => {
    expect(cellText(0)).toBe("0");
    expect(cellText(false)).toBe("false");
    expect(cellText(1.5)).toBe("1.5");
  });

  test("a structured value is shown as JSON rather than [object Object]", () => {
    expect(cellText({ a: 1 })).toBe('{"a":1}');
    expect(cellText([1, 2])).toBe("[1,2]");
  });
});

describe("outcomeFrom", () => {
  test("a successful query becomes a table", () => {
    const out = outcomeFrom(200, {
      ok: true,
      columns: ["ext", "n"],
      rows: [
        ["ts", 12],
        ["md", 3],
      ],
      truncated: false,
    });
    expect(out).toEqual({
      ok: true,
      sql: null,
      table: {
        columns: ["ext", "n"],
        rows: [
          ["ts", "12"],
          ["md", "3"],
        ],
        truncated: false,
      },
    });
  });

  test("the compiled SQL travels with an ask result", () => {
    const out = outcomeFrom(200, {
      ok: true,
      sql: "SELECT 1",
      columns: ["a"],
      rows: [[1]],
      truncated: true,
    });
    expect(out.ok && out.sql).toBe("SELECT 1");
    expect(out.ok && out.table.truncated).toBe(true);
  });

  test("a short row is padded to the column count", () => {
    // The table renders one <td> per column; a ragged row would shift the rest
    // of its cells one column left.
    const out = outcomeFrom(200, { ok: true, columns: ["a", "b", "c"], rows: [[1]] });
    expect(out.ok && out.table.rows).toEqual([["1", "NULL", "NULL"]]);
  });

  test("a plain server error carries its message verbatim", () => {
    // The server sends duckdb's own text ("no such column: nope"), which is the
    // whole point of not rewording it.
    const out = outcomeFrom(400, { error: "Binder Error: no such column: nope" });
    expect(out).toEqual({
      ok: false,
      sql: null,
      error: "Binder Error: no such column: nope",
    });
  });

  test("a refused ask still reports the SQL that was refused", () => {
    const out = outcomeFrom(400, {
      error: "only read-only statements are allowed here",
      sql: "DELETE FROM files",
    });
    expect(out.ok).toBe(false);
    expect(out.sql).toBe("DELETE FROM files");
  });

  test("the AI relay's typed envelope is unwrapped to its message", () => {
    // /api/index/ask passes a relay failure straight through, so the error is
    // an OBJECT here, not a string — read as-is it rendered "[object Object]".
    const out = outcomeFrom(502, {
      ok: false,
      error: { type: "ai_unavailable", message: "claude binary not found on PATH" },
    });
    expect(out.ok).toBe(false);
    expect(!out.ok && out.error).toBe("claude binary not found on PATH");
  });

  test("an unreadable response is still an error and never a crash", () => {
    for (const data of [null, undefined, "nope", 5, []]) {
      const out = outcomeFrom(500, data);
      expect(out.ok).toBe(false);
      expect(!out.ok && out.error.length).toBeGreaterThan(0);
    }
  });

  test("a 200 that is not ok:true is an error, not an empty table", () => {
    const out = outcomeFrom(200, { ok: false, error: "nope" });
    expect(out.ok).toBe(false);
  });

  test("a result with no rows is a table, not an error", () => {
    const out = outcomeFrom(200, { ok: true, columns: ["a"], rows: [] });
    expect(out.ok).toBe(true);
    expect(out.ok && out.table.rows).toEqual([]);
  });
});
