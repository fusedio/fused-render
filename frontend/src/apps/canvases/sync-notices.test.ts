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

  test("one: the node's name leads the title", () => {
    expect(removalNotice([{ name: "chunks_parquet_6", file: "chunks_parquet_6.py" }])).toEqual({
      title: "Removed chunks_parquet_6",
      detail: "Its file was deleted on disk.",
    });
  });

  test("several", () => {
    expect(
      removalNotice([
        { name: "a", file: "a.py" },
        { name: "b", file: null },
      ]),
    ).toEqual({ title: "Removed 2 nodes from the canvas", detail: "Their files were deleted on disk." });
  });
});

describe("keptEditNotice", () => {
  test("names the file, stays in the panel, and opens the kept text", () => {
    let opened = 0;
    const n = keptEditNotice("a.py", () => opened++);
    expect(n.title).toBe("Kept your unsaved edits");
    expect(n.detail).toBe("a.py was deleted on disk.");
    expect(n.tier).toBe("attention");
    expect(n.action?.label).toBe("Open");
    n.action?.onClick();
    expect(opened).toBe(1);
  });
});
