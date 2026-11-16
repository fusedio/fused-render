import { describe, expect, test } from "bun:test";
import { fileDeletedRemovals, keptEditNotice, removalNotice } from "./sync-notices";

describe("fileDeletedRemovals", () => {
  test("only deletes sync made for a deleted file", () => {
    expect(
      fileDeletedRemovals([
        { op: "delete", name: "a", reason: "file_deleted", file: "a.py" },
        { op: "delete", name: "b" }, // a node deleted with its row: not ours to announce
        { op: "upsert", name: "c", reason: "file_deleted" },
        { op: "delete", name: "d", reason: "file_deleted" },
        { op: "delete", name: "", reason: "file_deleted" },
      ]),
    ).toEqual([
      { name: "a", file: "a.py" },
      { name: "d", file: null },
    ]);
  });
});

describe("removalNotice", () => {
  test("none", () => {
    expect(removalNotice([])).toBeNull();
  });

  test("one, with and without its file", () => {
    expect(removalNotice([{ name: "chunks_parquet_6", file: "chunks_parquet_6.py" }])).toEqual({
      title: 'Removed "chunks_parquet_6" from the canvas',
      detail: "Its file chunks_parquet_6.py was deleted on disk.",
    });
    expect(removalNotice([{ name: "x", file: null }])?.detail).toBe("Its file was deleted on disk.");
  });

  test("several, listed with a cap", () => {
    expect(
      removalNotice([
        { name: "a", file: "a.py" },
        { name: "b", file: null },
      ]),
    ).toEqual({ title: "Removed 2 nodes from the canvas", detail: "Their files were deleted on disk: a.py and b." });
    const many = Array.from({ length: 7 }, (_, i) => ({ name: `n${i}`, file: `n${i}.py` }));
    expect(removalNotice(many)?.detail).toBe(
      "Their files were deleted on disk: n0.py, n1.py, n2.py, n3.py, n4.py and 2 more.",
    );
  });
});

describe("keptEditNotice", () => {
  test("names the file and where the text went, and stays in the panel", () => {
    expect(keptEditNotice("a.py", "/trash/1/a.py")).toEqual({
      title: "Kept your unsaved edits to a.py",
      detail: "The file was deleted on disk while you were editing it. Your text was saved to /trash/1/a.py",
      tier: "attention",
    });
  });
});
